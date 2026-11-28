"""Locked-skill approval authority. No model-facing approval API.

Enrollment is a separately attended native provisioning ceremony, not evidence of
human presence from an OS operator ticket. Only authenticated Telegram callbacks
can capture/confirm the candidate and approve frozen content. Local provisioning
must independently verify Brian's confirmation after release.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from hermes_cli.locked_skill_proposals import _unique_object, canonical, digest, qualify
from hermes_state_runtime import RuntimeStoreError

_PREFIX = 'skills.locked_proposals.v1:'


def _read(conn, key):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (_PREFIX + key,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0], object_pairs_hook=_unique_object)
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError('invalid_state') from exc
    if not isinstance(value, dict):
        raise RuntimeStoreError('invalid_state')
    return value


def _save(conn, key, value):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (_PREFIX + key, canonical(value)))


def _require(value, reason='permission_denied'):
    if not value:
        raise RuntimeStoreError(reason)


class Proposals:
    """Private server-side operations for one reserved native profile authority."""

    def __init__(self, authority):
        self.authority = authority
        self.db = authority.db
        self.home = Path(authority.profile_id)
        _require(self.home.is_absolute() and str(self.home.resolve()) == str(self.home), 'profile_mismatch')

    def enrollment_start(self, skill):
        target = qualify(self.home, skill)
        enrollment = {'id': uuid.uuid4().hex, 'target': target, 'expires': time.time() + 900}
        def write(conn):
            _require(_read(conn, 'principal') is None, 'already_registered')
            _save(conn, 'enrollment:' + enrollment['id'], enrollment)
            return enrollment
        return self.db._execute_write(write)

    def _identity(self, runner, event):
        from gateway.config import Platform
        from gateway.session_ingress_context import capture_provenance
        provenance = capture_provenance(runner, event)
        source, raw = event.source, event.raw_message
        _require(provenance is not None)
        _require(source.platform == Platform.TELEGRAM and source.is_bot is False
                 and not source.delivered_via_upstream_relay and not source.thread_id
                 and source.chat_type == 'dm')
        # Compare the real SDK sender/message with the source, not a model/wire supplied identity.
        from telegram import Message
        _require(isinstance(raw, Message) and raw.from_user is not None and raw.from_user.is_bot is False
                 and raw.chat.type == 'private' and raw.forward_origin is None
                 and raw.sender_chat is None and raw.edit_date is None)
        _require(str(raw.from_user.id) == source.user_id == source.chat_id == str(raw.chat.id)
                 and str(raw.message_id) == event.message_id and raw.text == event.text)
        _require(provenance['runtime_home'] == provenance['transport_home'] == str(self.home), 'profile_mismatch')
        return {'user': source.user_id, 'chat': source.chat_id, 'profile': source.profile,
                'provenance': provenance}, str(raw.message_id)

    def check_route(self, identity):
        from gateway.config import Platform
        from gateway.session import SessionSource
        from gateway.session_ingress_context import _binding
        runner = self.authority.runner
        source = SessionSource(platform=Platform.TELEGRAM, user_id=identity['user'], chat_id=identity['chat'],
                               chat_type='dm', profile=identity['profile'])
        provenance = identity['provenance']
        _, _, _, current = _binding(runner, source, provenance.get('transport_profile'))
        _require(current == provenance, 'unsupported_route')

    def native(self, runner, event, args):
        identity, message = self._identity(runner, event)
        _require(event.get_command_args().split() == ['proposal', *args], 'invalid_params')
        handlers = {'register': self._capture, 'confirm': self._confirm, 'approve': self._approve}
        _require(len(args) in {2, 3} and args[0] in handlers, 'invalid_params')
        return self.db._execute_write(lambda conn: handlers[args[0]](conn, args[1:], identity, message))

    def _enrollment(self, conn, identifier):
        record = _read(conn, 'enrollment:' + identifier)
        _require(record is not None and record['expires'] > time.time(), 'stale_enrollment')
        _require(_read(conn, 'principal') is None, 'already_registered')
        return record

    def _capture(self, conn, args, identity, message):
        _require(len(args) == 1, 'invalid_params')
        record = self._enrollment(conn, args[0])
        candidate = {'identity': identity, 'target': record['target']}
        fingerprint = digest(canonical(candidate).encode())
        _require('candidate_digest' not in record or record['candidate_digest'] == fingerprint, 'candidate_changed')
        record.update(candidate=candidate, candidate_digest=fingerprint, capture_message=message)
        _save(conn, 'enrollment:' + args[0], record)
        return {'candidate_digest': fingerprint, 'captured': True}

    def _confirm(self, conn, args, identity, message):
        _require(len(args) == 2, 'invalid_params')
        record = self._enrollment(conn, args[0])
        _require(record.get('candidate_digest') == args[1]
                 and record['candidate']['identity'] == identity
                 and record['capture_message'] != message, 'candidate_changed')
        record['confirmation_message'] = message
        _save(conn, 'enrollment:' + args[0], record)
        return {'candidate_digest': args[1], 'confirmed': True}

    def enrollment_finish(self, identifier, fingerprint):
        def write(conn):
            record = self._enrollment(conn, identifier)
            _require(record.get('candidate_digest') == fingerprint and record.get('confirmation_message'), 'unconfirmed')
            candidate = record['candidate']
            _require(qualify(self.home, candidate['target']['skill']) == candidate['target'], 'target_changed')
            _save(conn, 'principal', {**candidate, 'candidate_digest': fingerprint,
                                    'capture_message': record['capture_message'],
                                    'confirmation_message': record['confirmation_message'],
                                    'registered_at': time.time()})
            return {'registered': True, 'candidate_digest': fingerprint}
        return self.db._execute_write(write)

    def freeze(self, prepared, *, pr_url=None):
        from hermes_cli.locked_skill_proposals import ProposalError, freeze, manifest_digest
        from hermes_cli.locked_skill_publication import bind
        if pr_url is None:
            raise ProposalError('exact owning-repository PR URL required')
        _require(prepared['home'] == str(self.home), 'profile_mismatch')
        manifest = bind(freeze(prepared), pr_url)
        manifest['manifest_sha256'] = manifest_digest(manifest)
        def write(conn):
            key = 'manifest:' + manifest['manifest_sha256']
            previous = _read(conn, key)
            _require(previous is None or previous == manifest, 'manifest_changed')
            if previous is None:
                _save(conn, key, manifest)
            return manifest
        return self.db._execute_write(write)

    def _manifest(self, conn, fingerprint):
        from hermes_cli.locked_skill_proposals import manifest_digest, verify_revision
        record = _read(conn, 'manifest:' + fingerprint)
        _require(record is not None and record['home'] == record['profile_id'] == str(self.home)
                 and manifest_digest(record) == fingerprint, 'manifest_changed')
        reconciliation = _read(conn, 'reconciliation:' + fingerprint)
        if reconciliation is not None:
            _require(reconciliation['head'] == record['head']
                     and reconciliation['manifest_sha256'] == fingerprint, 'invalid_state')
            _require(_read(conn, 'application:' + fingerprint) is not None
                     and type(reconciliation.get('completed')) is bool, 'invalid_state')
        verify_revision(record, reconciling=reconciliation is not None,
                        reconciled=reconciliation is not None and reconciliation['completed'])
        from hermes_cli.locked_skill_publication import verify
        verify(record)
        return record

    def _approve(self, conn, args, identity, message):
        _require(len(args) == 1, 'invalid_params')
        principal = _read(conn, 'principal')
        _require(principal is not None and principal['identity'] == identity)
        manifest = self._manifest(conn, args[0])
        # Enrollment is scoped to one owner/skill/path, not a broad principal grant.
        target = principal['target']
        _require(all(manifest.get(key) == target[key] for key in
                     ('home', 'repository', 'owner', 'common_dir', 'skill', 'path')), 'target_changed')
        current = qualify(self.home, manifest['skill'])
        _require(all(current[key] == manifest[key] for key in current), 'target_changed')
        key = 'approval:' + args[0]
        receipt = _read(conn, key)
        if receipt is None:
            receipt = {'manifest_sha256': args[0], 'identity': identity, 'message': message,
                       'candidate_digest': principal['candidate_digest'], 'approved_at': time.time()}
            _save(conn, key, receipt)
        return {'approved': True, 'manifest_sha256': args[0]}

    def reconcile(self, fingerprint):
        from gateway.locked_skill_reconcile import reconcile
        return reconcile(self, fingerprint)

    def apply(self, fingerprint):
        from gateway.locked_skill_apply import apply
        return apply(self, fingerprint)
