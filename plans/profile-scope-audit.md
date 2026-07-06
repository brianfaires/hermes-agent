# Profile-scope audit — HERMES_HOME resolution (2026-07-07)

Audit of call sites that bypass profile scoping (`_profile_runtime_scope`,
`set_hermes_home_override`, call-time `get_hermes_home()`). Motivated by the
move to 4+ named profiles: every bypass is a place where a named-profile
gateway silently reads or writes the default profile's state.

## Semantics cheat-sheet

Two failure modes, with opposite fixes:

- **Module-level snapshot** (`X = get_hermes_home()` at import): pinned to the
  gateway's startup profile. Correct for single-profile gateways (immune to
  ambient env drift — this is what the `fix(profiles)` commits enforce), but
  blind to per-turn profile overrides in a multiplexing gateway
  (`SessionSource.profile`).
- **Hand-built path** (`Path.home() / ".hermes"`, `expanduser("~/.hermes")`):
  always wrong when used for *state* — ignores both `HERMES_HOME` and the
  override contextvar. Only legitimate use is to name the *default* home
  explicitly (e.g. deriving a profile label by comparison).

## Fixed in this fork

- `gateway/kanban_mirror/config.py` — `DEFAULT_TOKEN_ENV_PATH` was a
  module-level `Path.home()/".hermes"/".env"`; a named-profile gateway read the
  default profile's Discord token. Now resolved at `load_mirror_config()` time
  via `get_hermes_home()`.

## False positives (deliberate default-home references)

- `gateway/temp_audio.py` `_profile_temp_segment()` — compares the active home
  against `Path.home()/".hermes"` to *label* the default profile. Correct.
- `tools/tts_tool.py` `_active_hermes_profile_label()` — same pattern. Correct.

## Upstream-owned issues (PR candidates, not fixed here to keep the fork small)

Module-level snapshots in gateway state files (all pinned at import; break
per-turn profile multiplexing, and profiles do not get separate state):

- `gateway/platforms/base.py:838` — `_HERMES_HOME` feeds
  `MEDIA_DELIVERY_SAFE_ROOTS` (image/audio/video/document caches). Media
  generated under a named profile's home fails safe-root validation and is
  not delivered.
- `gateway/mirror.py` `_SESSIONS_DIR`, `gateway/sticker_cache.py` `CACHE_PATH`,
  `gateway/channel_directory.py` `DIRECTORY_PATH`/`CHANNEL_ALIASES_PATH`,
  `gateway/hooks.py` `HOOKS_DIR`,
  `gateway/platforms/feishu_comment_rules.py` `RULES_FILE`/`PAIRING_FILE`.
- `gateway/run.py:1174` `_hermes_home` — startup-only usage (.env load,
  restart markers); pinned-at-startup is arguably correct here.

Hand-built `~/.hermes` paths in upstream code (ignore the override contextvar):

- `gateway/rich_sent_store.py:28` — env-or-`~/.hermes` fallback.
- `gateway/platforms/telegram.py:4341` — gmail-triage script path hardcodes
  `Path.home()/".hermes"/scripts/...`.
- `plugins/platforms/google_chat/adapter.py:528,698`, `.../oauth.py:85`.
- `plugins/platforms/photon/auth.py:94`.
- `tools/mcp_oauth.py:122`, `agent/secret_sources/bitwarden.py:95`.
- `plugins/memory/openviking/__init__.py:1054`.

Module-level snapshots in tools (loaded per-process; pinned to whatever
HERMES_HOME was at import — usually fine for CLI, wrong for a long-lived
multiplexing gateway):

- `tools/skills_sync.py`, `tools/skills_tool.py`, `tools/skill_manager_tool.py`
  (`HERMES_HOME = get_hermes_home()`), `tools/process_registry.py`
  (`CHECKPOINT_PATH`), `tools/environments/singularity.py` (`_SNAPSHOT_STORE`),
  `cron/jobs.py` (`HERMES_DIR`).

## Follow-up

The fork's profile-scoping set (`fix(profiles)`-style commits: model switches,
cron pinning, TTS runtime, plus the kanban token fix above) is the natural
upstream PR. The snapshot conversions above need a per-file decision —
pin-at-startup vs per-turn override — and belong in that PR discussion, not in
this fork.
