"""Refresh and reader liveness contracts for versioned configuration."""
from contextlib import contextmanager
import threading

import pytest

from hermes_cli import config, config_snapshots
from hermes_cli.config_transactions import config_mutation_lock


def test_setup_in_place_refresh_adopts_new_revision(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    state = config.load_config()
    config.save_config(state)
    nested = config.load_config()
    nested['model'] = {'default': 'fixture-model', 'provider': 'fixture'}
    config.save_config(nested)
    state.clear()
    state.update(config.load_config())
    state['custom'] = {'later': True}
    config.save_config(state)
    assert config.read_user_config_raw()['model']['default'] == 'fixture-model'
    assert config.read_user_config_raw()['custom']['later'] is True


def test_plain_reader_survives_write_churn_but_cannot_save_uncertain_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('custom: 1\n')
    revisions = iter(range(10))
    monkeypatch.setattr(config_snapshots, '_revision', lambda path: next(revisions))
    state = config_snapshots.read_config_snapshot(path, lambda: {'custom': 1})
    assert state['custom'] == 1
    with pytest.raises(config_snapshots.ConfigConflictError):
        config.atomic_config_replace(path, state)
    assert path.read_text() == 'custom: 1\n'


def test_waiting_file_writer_does_not_hold_reader_lock(tmp_path, monkeypatch):
    from hermes_cli import auth
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('custom: 1\n')
    waiting, release, read_done = (threading.Event() for _ in range(3))
    failures = []

    @contextmanager
    def blocked_file_lock(*args):
        waiting.set()
        assert release.wait(10)
        yield

    def writer():
        try:
            with config_mutation_lock(tmp_path / 'config.yaml'):
                pass
        except BaseException as exc:
            failures.append(exc)

    def reader():
        try:
            assert config.read_raw_config()['custom'] == 1
        finally:
            read_done.set()

    monkeypatch.setattr(auth, '_file_lock', blocked_file_lock)
    write_thread = threading.Thread(target=writer)
    read_thread = threading.Thread(target=reader)
    write_thread.start()
    try:
        assert waiting.wait(10)
        read_thread.start()
        assert read_done.wait(2), 'cache-miss reader blocked behind waiting writer'
    finally:
        release.set()
        write_thread.join(10)
        if read_thread.ident is not None:
            read_thread.join(10)
    assert not failures


def test_effective_fail_closed_refuses_nonmapping_even_after_tolerant_read(tmp_path, monkeypatch):
    from hermes_cli.config_effective import load_user_config_effective
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = tmp_path / 'config.yaml'
    path.write_text('[]\n')
    load_user_config_effective(path)
    with pytest.raises(ValueError, match='mapping'):
        load_user_config_effective(path, fail_closed=True)
