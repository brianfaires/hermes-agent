# Google Workspace Broker Migration Runbook

This migration is intentionally split into separate approval gates. Do not run
any step against live state without explicit operator approval for that step.

1. Pre-migration gate: confirm the target system user/service account, shared
   Unix group, local Unix socket path, broker state path, and rollback owner.
   Example identities only: broker uid `24001`, shared group gid `24002`,
   Hermes uid `1000`.
2. Backup gate: run the deterministic backup helper against selected regular
   source files only after `approved=True` has been explicitly granted. It
   creates a new `0700` backup directory containing `manifest.json` mode `0600`
   and opaque files under `files/` mode `0600`. The manifest records original
   uid/gid/mode/size/mtime plus source and copy SHA-256; it does not contain
   file contents. Restore by selecting the manifest entry, verifying
   `copy_sha256`, then copying the corresponding opaque backup file back to the
   original `path` with the recorded owner/mode after a separate restore
   approval gate.
3. System user/service gate: create or update the local broker service user,
   shared group, runtime directory, state directory, and unit files.
4. OAuth migration gate: provision Google OAuth credentials for the broker.
5. Plugin enablement gate: enable the bundled plugin and socket config.
6. Cron/config gate: apply scheduled-job or config changes.
7. Gateway restart gate: restart Hermes gateway processes.
8. Live verification gate: perform Google Calendar/Gmail read checks, then
   broker-owned write checks. Sending, forwarding, trashing, deleting mail, and
   Calendar deletion are not broker capabilities.

Minimum combined scopes:

- Calendar readonly: `https://www.googleapis.com/auth/calendar.readonly`
- Calendar write: `https://www.googleapis.com/auth/calendar`
- Gmail readonly: `https://www.googleapis.com/auth/gmail.readonly`
- Gmail labels/modify: `https://www.googleapis.com/auth/gmail.modify`
- Gmail drafts: `https://www.googleapis.com/auth/gmail.compose`

`gmail.modify` is broader than label-only access: Google uses it for message
label mutation and it can also permit other mailbox changes. The broker boundary
therefore rejects send/reply/forward/trash/delete paths, rejects system-label
CRUD or impersonation, and exposes only the fixed allowlisted operations.

## Socket Identity Model

Production uses separate identities: the broker process owns the socket, and
Hermes connects through a dedicated shared Unix group. The socket must have zero
world bits. The production plugin accepts only a `0660` socket owned by the
configured broker uid and shared group gid, and it rejects any broker uid equal
to the Hermes process uid. The broker process also requires `--client-uid` and
checks Unix peer credentials before reading each request; a missing or
mismatched Hermes uid is rejected before broker dispatch. `0600` sockets are
only for low-level broker/server unit tests that bypass plugin config discovery.

Non-live example setup commands:

```bash
# Example only. Do not run without the system-user/service approval gate.
sudo groupadd --gid 24002 hermes-gws
sudo useradd --uid 24001 --gid 24002 --system --home /var/lib/hermes-gws --shell /usr/sbin/nologin hermes-gws-broker
sudo usermod --append --groups hermes-gws brian
sudo install -d -o 24001 -g 24002 -m 0750 /run/hermes-gws
sudo install -d -o 24001 -g 24001 -m 0700 /var/lib/hermes-gws
sudo install -d -o 24001 -g 24001 -m 0700 /var/lib/hermes-gws/credentials
```

Broker CLI example:

```bash
# Example only. Numeric --socket-gid produces a 0660 socket; --client-uid is the Hermes uid.
sudo -u '#24001' /path/to/python -m plugins.google_workspace_broker.server \
  --socket /run/hermes-gws/broker.sock \
  --socket-gid 24002 \
  --client-uid 1000 \
  --state /var/lib/hermes-gws/calendar-state.json \
  --credentials /var/lib/hermes-gws/oauth-authorized-user.json
```

Plugin config is non-secret but must still be an absolute, regular,
non-symlink file owned by the current Hermes uid and mode `0600` or stricter.
It contains exactly these keys:

```json
{
  "socket_path": "/run/hermes-gws/broker.sock",
  "expected_socket_uid": 24001,
  "expected_socket_gid": 24002
}
```

Set `GOOGLE_WORKSPACE_BROKER_CONFIG` to that config path for the Hermes process.
The plugin opens that config once with no symlink following where supported,
verifies the open file owner/mode/key shape, then verifies the actual socket
parent uid/gid/permissions and socket uid/gid/mode before connecting. The
socket parent must be a real non-symlink directory owned by the configured
broker uid and shared group gid, and it must not be group/world writable.

## Credential Boundary

The broker refuses to load Google credentials unless the credential path is
absolute, regular, non-symlink, owned by the broker euid, mode exactly `0600`,
and its parent directory is a broker-owned non-symlink directory that is not
group/world writable. These checks happen on the open file descriptor before
Google libraries are imported or credential contents are loaded.

## Approval Gates

Use separate approvals for:

- backup creation
- system user/group/service changes
- OAuth credential provisioning
- plugin config enablement
- cron/config changes
- gateway restart
- live read verification
- broker-owned write verification
- restore from backup

The broker does not implement Gmail send/reply/forward/trash/delete or Calendar
delete rollback. Gmail message label assignment permits only user `Label_...`
IDs plus `INBOX`, `UNREAD`, `STARRED`, and `IMPORTANT`; it rejects `TRASH`,
`SPAM`, `SENT`, `DRAFT`, category labels, and unknown system-looking IDs.
