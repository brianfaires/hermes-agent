"""Durable cron answers and native outcomes beyond bounded operational history.

Only terminal facts, job labels/schedule and the final response are retained. Prompts,
session transcripts and intermediate tool output are never copied. Receipts remain until
the operator removes the profile's executions database; mirror outages cannot expire them.
"""
from __future__ import annotations

import json


def initialize_results(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_results (
        execution_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL DEFAULT '{}',
        terminal_record TEXT
    )""")


def capture_execution_job(execution_id, job):
    """Capture an owned running attempt's labels before a one-shot can remove itself."""
    import os
    from cron import executions

    schedule = job.get('schedule') or {}
    snapshot = dict(job_name=job.get('name'), scheduled_at=job.get('_scheduled_instant')
                    or job.get('next_run_at'), schedule={key: schedule[key]
                    for key in ('kind', 'expr', 'minutes', 'run_at', 'display') if key in schedule})
    with executions._transaction() as conn:
        row = executions._fetch(conn, execution_id)
        if (not row or row['status'] != 'running' or row['process_id'] != executions._PROCESS_ID
                or row['pid'] != os.getpid()):
            raise RuntimeError('Cron execution no longer owned before result snapshot')
        conn.execute('INSERT OR IGNORE INTO execution_results (execution_id, snapshot) VALUES (?, ?)',
                     (execution_id, json.dumps(snapshot)))


def retain_terminal_result(conn, record, *, final_response=None):
    """Called inside the existing status transaction, after its owner-fenced CAS."""
    if record is None:
        return
    # Databases created before ``snapshot`` gained its default reject a row without one, and
    # OR IGNORE would swallow that NOT NULL failure too: supply it and ignore only the key conflict.
    conn.execute("INSERT INTO execution_results (execution_id, snapshot) VALUES (?, '{}') "
                 'ON CONFLICT(execution_id) DO NOTHING', (record['id'],))
    prior = conn.execute('SELECT terminal_record FROM execution_results WHERE execution_id=?',
                         (record['id'],)).fetchone()['terminal_record']
    # Native verified receipt recovery may settle an earlier uncertain audit outcome.
    if prior is not None and (json.loads(prior)['status'] != 'unknown'
                              or record['status'] not in {'completed', 'failed'}):
        return
    conn.execute('UPDATE execution_results SET terminal_record=? WHERE execution_id=?',
                 (json.dumps(dict(record, final_response=final_response)), record['id']))


def retained_execution(conn, execution_id):
    """Native execution facts after bounded audit pruning; never expose the answer here."""
    row = conn.execute('SELECT terminal_record FROM execution_results WHERE execution_id=?',
                       (execution_id,)).fetchone()
    if row is None or row['terminal_record'] is None:
        return None
    record = json.loads(row['terminal_record'])
    record.pop('final_response', None)
    return record


def settle_retained_delivery(conn, execution_id, outcome):
    """Apply the native queued-delivery CAS without changing the execution answer."""
    row = conn.execute('SELECT terminal_record FROM execution_results WHERE execution_id=?',
                       (execution_id,)).fetchone()
    if row is None or row['terminal_record'] is None:
        return False
    record = json.loads(row['terminal_record'])
    if record.get('delivery_outcome') != 'queued':
        return False
    record['delivery_outcome'] = outcome
    conn.execute('UPDATE execution_results SET terminal_record=? WHERE execution_id=?',
                 (json.dumps(record), execution_id))
    return True


def retain_terminal_results(conn):
    """Recovery/cancellation also commit receipts before bounded history pruning."""
    rows = conn.execute("""SELECT e.* FROM executions e LEFT JOIN execution_results r
        ON e.id=r.execution_id WHERE e.status IN ('completed','failed','unknown')
        AND r.terminal_record IS NULL""").fetchall()
    for row in rows:
        retain_terminal_result(conn, dict(row))


def iter_execution_results(*, page_size=200):
    """Read all retained receipts in bounded pages; release each DB transaction before I/O."""
    from cron import executions

    cursor = 0
    while True:
        with executions._transaction() as conn:
            rows = conn.execute('SELECT rowid, snapshot, terminal_record FROM execution_results '
                'WHERE rowid>? AND terminal_record IS NOT NULL ORDER BY rowid LIMIT ?',
                (cursor, max(1, min(int(page_size), 500)))).fetchall()
        if not rows:
            return
        for row in rows:
            yield dict(json.loads(row['terminal_record']), **json.loads(row['snapshot']))
        cursor = rows[-1]['rowid']
