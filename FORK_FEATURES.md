# Fork features after `3ef6bbd20`

This file documents the fork behavior present in the tree after base commit
`3ef6bbd201263d354fd83ec55b3c306ded2eb72a`. It is derived from the current
implementation, tests, and rewritten history. Commit IDs below are unique
12-character abbreviations from that history.

No live profile values are recorded here. Examples use inert names and IDs.

## Status vocabulary

- **Implemented-and-wired** — the current executable path calls the feature.
- **Implemented-but-unwired** — code exists, but no current executable path calls it.
- **Deployment-gated** — wired code exists, but a plugin, config gate, credential,
  platform, or external service must be enabled before it does work.
- **Deprecated** — compatibility surface remains but should not be configured for new use.
- **Removed** — the old surface is absent from the current tree.

One implemented-but-unwired surface was found: the Discord `voice_fx`
continuous mixer and profile acknowledgement catalog. Optional plugins and
disabled-but-executable runtime paths are labeled deployment-gated instead.

## Quick feature index

| Area | Feature | Status |
|---|---|---|
| Configuration | Atomic JSON Pointer config mutations | Implemented-and-wired |
| Installation | Worktree-safe installer and doctor repair | Implemented-and-wired |
| Browser | Dual-stack-aware local CDP port selection | Implemented-and-wired |
| TTS | ElevenLabs voice settings and text-preserving fallback | Deployment-gated |
| Memory | Provider-specific Hindsight toolset and history command | Deployment-gated |
| Skills | External-skill prompt-cache invalidation | Implemented-and-wired |
| Tooling | Platform-scoped pinned toolsets | Implemented-and-wired |
| Diagnostics | Request-context estimate and provider-boundary capture | Deployment-gated |
| Observability | Langfuse path-like payload neutralization | Deployment-gated |
| Cleanup | Recursive wildcard disk cleanup | Deployment-gated |
| Webhook | Google Pub/Sub OIDC authentication | Deployment-gated |
| Webhook | Guarded local script triggers | Deployment-gated |
| Cron | Lifecycle hooks and file-backed prompts | Implemented-and-wired |
| Cron | Delivery diagnostics and output-hook parity | Implemented-and-wired |
| Calendar | Profile-local cron-to-Google-Calendar plugin | Deployment-gated |
| Discord | Markdown/media/display correctness | Implemented-and-wired |
| Discord | Channel-policy enforcement and thread behavior | Deployment-gated |
| Profiles | Profile-pinned gateway, cron, model, and adapter state | Implemented-and-wired |
| Operations | Audited multi-profile gateway restart tool | Deployment-gated |
| Voice | Profile-scoped Discord voice orchestration | Deployment-gated |
| Voice | `/stop`, filler normalization, and profile-scoped runtime | Deployment-gated |
| Voice | Continuous `voice_fx` mixer and acknowledgement catalog | Implemented-but-unwired |
| Kanban | Board inventory, branch metadata, and notification policy | Implemented-and-wired |
| Kanban | Durable Discord mirror and conversation router | Deployment-gated |
| Compression | Voice, summary continuity, and atomic manual rotation | Implemented-and-wired |
| Prompting | Bounded guidance for selected model families | Implemented-and-wired |
| CI/release | Fork test isolation, scanning, and attribution policy | Implemented-and-wired |
| Desktop security | Patched DOMPurify dependency | Implemented-and-wired |
| Retired surfaces | `discord.stt_aliases`; permissive adapter resolver | Removed |

## 1. Atomic structural config mutations

**Status:** implemented-and-wired.
**Owners:** `e024744b1ada`.

- **Purpose and behavior:** `hermes config patch` performs concurrency-safe
  `add`, `replace`, and `remove` operations against `config.yaml`. Paths use
  JSON Pointer escaping (`~1` for `/`, `~0` for `~`); `/-` appends to a list.
- **Architecture:** `hermes_cli/subcommands/config.py` exposes the CLI and
  `hermes_cli/config.py` serializes read/modify/write operations before atomic
  replacement. It does not write `.env`.
- **Config/env/defaults:** no feature flag or new environment variable. The
  target is the active profile's `config.yaml`.
- **Usage:**

  ```bash
  hermes config patch add /model/aliases/fast --json '"openai/gpt-5"'
  hermes config patch add /fallback_providers/- --json '{"provider":"openai","model":"gpt-5"}'
  hermes config patch remove /model/aliases/fast
  ```

- **Restart/deployment:** the write itself is immediate. Long-running gateways
  must be restarted when the changed setting is startup-cached.
- **Tests/caveats:** `tests/hermes_cli/test_config_patch.py` covers operation
  semantics, invalid paths, and concurrent structural writes. `replace`
  requires an existing target; `add` and `replace` require valid JSON.

## 2. Worktree-safe installation and browser startup

### Worktree-safe launcher handling

**Status:** implemented-and-wired.
**Owners:** `d63dd88d258e`.

- `setup-hermes.sh` and `hermes doctor --fix` detect a linked Git worktree by a
  root `.git` **file**. Worktree-local setup may proceed, but global launcher
  symlinks and shell PATH files are not modified.
- There is no config key or environment gate. The guard is automatic.
- Use the worktree-local executable explicitly, for example
  `./venv/bin/hermes` or `./.venv/bin/hermes` according to that checkout.
- No gateway restart is required. The guard prevents a deployment-side effect;
  it does not deploy the worktree.
- Tests: `tests/test_setup_hermes_worktree_safety.py` and
  `tests/hermes_cli/test_doctor_command_install.py`.

### Loopback-family-aware CDP port selection

**Status:** implemented-and-wired.
**Owners:** `445c814bac3f`.

- Browser startup probes the loopback address families available on the host
  instead of treating a missing IPv6 bind as a collision for every candidate.
  IPv4-only hosts therefore select a genuinely free Chrome DevTools port.
- No new config or environment variable. Existing browser/CDP configuration
  still applies; the default local endpoint remains `127.0.0.1:9222`.
- No restart beyond starting a new browser-debug process. Test:
  `tests/hermes_cli/test_browser_connect_dual_stack.py`.

## 3. TTS and Discord voice

### ElevenLabs voice settings and fallback

**Status:** deployment-gated.
**Owners:** `d07ed4f8f269`, `70adc9bec2d3`.

- **Behavior:** ElevenLabs sync generation and streaming playback accept the
  same voice settings. If the premium SDK is unavailable, the visible text
  stream remains intact rather than disappearing with TTS.
- **Wiring:** `tools/tts_tool.py` builds `elevenlabs.types.VoiceSettings` only
  inside the ElevenLabs provider path. Per-utterance acknowledgement overrides
  reuse the same settings path.
- **Config:** under `tts.elevenlabs`:
  - `voice_id` — default `pNInz6obpgDQGcFmaJgB`.
  - `model_id` — default `eleven_multilingual_v2`; streaming default remains
    `eleven_flash_v2_5` where the streaming path selects it.
  - `speed` — default `tts.speed`, otherwise `1.0`; clamped to `0.7..1.2` when
    sent in `VoiceSettings`.
  - `stability`, `similarity_boost`, `style` — omitted by default; configured
    values are clamped to `0.0..1.0`.
  - `use_speaker_boost` — omitted by default; accepts a boolean or common true
    spellings.
- **Prerequisites:** `tts.provider: elevenlabs`, `ELEVENLABS_API_KEY`, and the
  optional `hermes-agent[tts-premium]` dependency.
- **Example:**

  ```yaml
  tts:
    provider: elevenlabs
    elevenlabs:
      voice_id: demo-voice-id
      speed: 1.05
      stability: 0.45
      similarity_boost: 0.75
      style: 0.15
      use_speaker_boost: true
  ```

- **Restart:** ordinary TTS calls resolve configuration per invocation/stream
  setup, so no restart is inherently required. Restart a long-running gateway
  only when its surrounding profile/adapter configuration is startup-cached.
- **Tests:** `tests/tools/test_tts_dotenv_fallback.py` and
  `tests/gateway/test_voice_command.py`.

### Profile-scoped voice orchestration

**Status:** deployment-gated.
**Owners:** `c5665e35e00f`, `5feace5be2c1`, `38a2d27bd702`, `b488245ddbd0`.

- Voice media, TTS, STT, adapter state, model resolution, acknowledgements, and
  cleanup resolve through the inbound `SessionSource.profile`. Missing profile
  adapters fail closed; they do not fall back to the primary bot.
- Runtime orchestration now lives in
  `plugins/platforms/discord/voice_runtime.py`; `gateway/run.py` keeps the thin
  lifecycle seam. Durable Discord state is rooted in the adapter's profile
  home, not ambient process state.
- **Gates/prerequisites:** a configured Discord platform with voice support,
  `discord.py[voice]`, FFmpeg/codec support, Discord voice permissions, and any
  selected STT/TTS provider credentials. `discord.auto_voice_channel_id`
  defaults to `null` and must be a positive channel ID when set;
  `discord.auto_voice_user_ids` defaults to `[]` and accepts a sequence or
  comma-separated IDs. Multiplexed identities additionally require
  `gateway.multiplex_profiles: true` (or the recognized operator override
  `GATEWAY_MULTIPLEX_PROFILES`).
- **Restart:** required after source, profile, credential, or startup-cached
  voice configuration changes.
- **Tests:** the profile-routing and lifecycle matrix spans
  `tests/gateway/test_discord_tts_profile_scope.py`,
  `test_discord_voice_receive_secret_refresh.py`,
  `test_voice_ack_profile_routing.py`, and
  `tests/integration/test_voice_channel_flow.py`.

### Voice command flows, filler normalization, and `/stop`

**Status:** deployment-gated.
**Owners:** `e9b448b2c7f5`, `ffce6eed74a4`, `50c0f59b86de`.

- `/stop` interrupts in-progress Discord TTS, including sibling threads.
  Join/rejoin, cancellation, busy, model-switch, and session-resume states have
  acknowledgement hooks. Filler-only/filler-prefixed STT is normalized before
  command dispatch. Audible acknowledgement playback currently depends on the
  unwired mixer described below.
- Existing Discord voice gates and provider prerequisites apply. Adapter config
  changes require restart. Tests include `test_voice_command.py` and
  `test_stop_thread_sibling.py`.

### Continuous `voice_fx` mixer and profile acknowledgement catalog

**Status:** implemented-but-unwired.
**Owners:** `c5665e35e00f`, `d71f3509479b`.

- The implementation can sum ambient PCM, acknowledgements, and TTS while
  ducking ambient audio under speech, but production join code never calls
  `_install_voice_mixer()`. `discord.voice_fx.enabled` has no executable runtime
  read, so `voice_mixer_active()` remains false and first-tool/catalog
  acknowledgements do not fire in a normal gateway.
- `discord.voice_fx` defaults:
  - `enabled: false`
  - `ambient_enabled: true`, `ambient_path: ""`
  - `ambient_gain: 0.18`, `duck_gain: 0.06`, `speech_gain: 1.0`
  - `ack_enabled: true`
  - built-in `ack_phrases`; event-specific phrase lists are available for
    cancellation, model switch, join, busy, restart join, and session resume.
  - `session_resume_user_turn_threshold: 2`.
- A profile-local `voice/acknowledgements.yaml` can replace phrase selection.
  Schema version is `2`; file/group/phrase settings support `enabled` (default
  `true`), `weight` (`1`), model include/exclude (`["*"]`/`[]`), and voice
  `style`, `stability`, and `speed` (unset by default).
- Example:

  ```yaml
  discord:
    voice_fx:
      enabled: true
      ambient_enabled: true
      ambient_gain: 0.18
      duck_gain: 0.06
      speech_gain: 1.0
      ack_enabled: true
  ```

- The isolated mixer and parser tests pass by manually installing/constructing
  the relevant objects; they do not prove an executable join-to-mixer path.
  Restarting or setting `enabled: true` does not repair the missing wiring.
  Tests: `test_discord_voice_mixer.py` and `test_voice_acknowledgements.py`.

### Profile-local STT alias catalog

**Status:** implemented-and-wired when Discord voice is active; deployment-gated
by the Discord/STT prerequisites above.
**Owners:** `6551554f91f2`, `e1530115af56`.

- Exact, lightly normalized spoken phrases are rewritten before voice-command
  dispatch. The current source is `<HERMES_HOME>/voice/commands.toml`:

  ```toml
  [stt_aliases]
  "/new" = ["reset session", "new session", "start over"]
  "/queue continue" = ["keep going"]
  ```

- Missing, invalid TOML, and invalid UTF-8 fail soft to no aliases. The catalog
  is cached when the Discord adapter is constructed, so a gateway restart is
  required after edits.
- The former `discord.stt_aliases` config key is **removed** and no longer
  creates/configures a Discord platform entry. Tests:
  `tests/gateway/test_voice_command.py` and `test_config.py`.

## 4. Memory, skills, and model prompting

### Provider-specific Hindsight toolset

**Status:** deployment-gated.
**Owners:** `7cf53fe45983`.

- The file-backed `memory` toolset and provider-backed Hindsight tools are
  independent. `memory.provider: hindsight` auto-enables the `hindsight`
  capability token; runtime tools are injected by `MemoryManager` while the
  static toolset remains intentionally empty.
- Exact gates:
  - `memory.provider` — default `""`; set to `hindsight`.
  - `agent.disabled_toolsets` — default `[]`; `memory` may be disabled without
    suppressing Hindsight, while `hindsight` explicitly disables provider tools.
  - `platform_toolsets` may explicitly include `hindsight`.
  - Hindsight provider prerequisites and the optional
    `hermes-agent[hindsight]` dependency still apply.
- Provider defaults remain `HINDSIGHT_MODE=cloud`,
  `HINDSIGHT_BANK_ID=hermes`, `HINDSIGHT_BUDGET=mid`,
  `HINDSIGHT_API_URL=https://api.hindsight.vectorize.io`,
  `HINDSIGHT_TIMEOUT=120`, and `HINDSIGHT_IDLE_TIMEOUT=300`. Cloud mode
  requires `HINDSIGHT_API_KEY`. Local embedded mode may additionally use
  `HINDSIGHT_LLM_API_KEY` plus its profile-scoped
  `<HERMES_HOME>/hindsight/config.json` model/provider settings.
- Example:

  ```yaml
  memory:
    provider: hindsight
    memory_enabled: false
  agent:
    disabled_toolsets: [memory]
  ```

- Restart/new agent construction is required after changing provider/toolsets.
  Tests: `tests/agent/test_memory_provider.py` and
  `tests/hermes_cli/test_tools_config.py`.

### Hindsight history reconstruction command

**Status:** deployment-gated.
**Owners:** `d0b90d532f34`.

- Enabling the standalone `hindsight-history` plugin adds
  `/hindsight-history [--retain-only|--recall-only] [--turns N]`. It reads the
  newest active transcript from profile `state.db`, reports recorded explicit
  calls, and reconstructs automatic recall against the **current** memory store.
- Prerequisites: plugin enabled, `hindsight-client>=0.6.1`, usable Hindsight
  provider configuration, and a persisted session. `HERMES_HOME` selects the
  profile database; there is no additional feature config.
- Setup: `hermes plugins enable hindsight-history`, then restart the gateway so
  plugin commands are registered. Tests: `tests/plugins/test_hindsight_history_plugin.py`.
- Caveat: automatic retain results were asynchronous and are reported as
  reconstructed status, not historical payloads; automatic recall is re-run,
  not replayed.

### External-skill cache invalidation

**Status:** implemented-and-wired.
**Owners:** `b935eceaf8b5`.

- Prompt-cache keys include a manifest signature for each directory in
  `skills.external_dirs`, so adding, removing, or editing an external skill
  invalidates the assembled skills prompt.
- `skills.external_dirs` defaults to `[]`. Add shared directories explicitly:

  ```yaml
  skills:
    external_dirs:
      - /srv/hermes/shared-skills
  ```

- No gateway restart is required for later file changes; invalidation occurs on
  prompt assembly. Existing conversations still obey normal conversation prompt
  stability rules. Test: `tests/agent/test_external_skills.py`.

### Bounded execution guidance for selected models

**Status:** implemented-and-wired.
**Owners:** `e4344218855f`.

- Exact model-family routing injects bounded tool-persistence guidance for
  `gpt-5.6-sol` and `gpt-5.6-terra`; broader GPT/Codex/Grok models receive the
  existing OpenAI-family execution guidance. Claude models do not.
- No config or environment gate. Selection is based on the active model name at
  agent construction, so a model switch/new agent applies the corresponding
  prompt. Tests: `tests/agent/test_prompt_builder.py` and
  `tests/run_agent/test_run_agent.py`.

## 5. Tool search, displays, and request diagnostics

### Platform-scoped pinned toolsets

**Status:** implemented-and-wired.
**Owners:** `0241eb894aca`.

- Tool search can keep selected toolsets directly visible while deferring other
  non-core schemas. `default` pins merge with the active platform key; unknown
  toolsets are ignored. Pins do not widen the session's authorized toolsets.
- `tools.tool_search` defaults: `enabled: auto`, `threshold_pct: 10`,
  `search_default_limit: 5`, `max_search_limit: 20`, and absent
  `pinned_toolsets` behaves as `{}`.
- Example:

  ```yaml
  tools:
    tool_search:
      enabled: auto
      pinned_toolsets:
        default: [github]
        discord: [discord]
        telegram: [telegram]
  ```

- `HERMES_SESSION_PLATFORM` or gateway session context selects the platform
  entry; it is runtime routing context, not a user-facing feature toggle.
  Reconstruct the agent/session after edits. Tests: `tests/tools/test_tool_search.py`.

### Gateway progress display abbreviation

**Status:** implemented-and-wired.
**Owners:** `f11ca377e79c`.

- Gateway progress output shortens known path roots, strict-shell prologues, and
  tool-call output; Discord progress messages suppress URL embeds. Path labels
  are computed from active environment/profile paths rather than hard-coded.
- No config or restart requirement beyond running the updated code. Tests:
  `tests/agent/test_display.py`, `tests/gateway/test_stream_events.py`, and
  `test_run_progress_topics.py`.

### Request-context estimate and provider-boundary capture

**Status:** deployment-gated.
**Owners:** `c1cce48f703a`.

Two related diagnostics share this owner but have different fidelity:

1. Enabling the standalone `request-dump` plugin registers
   `/dump-system-prompt`. It combines the newest persisted session prompt with
   **current** tool configuration and writes a mode-`0600` text estimate under
   `<HERMES_HOME>/dump-system-prompt/`. It is not a historical request capture.
2. Top-level `request_capture.enabled` (default `false`) captures the first
   Hermes-visible request for each newly constructed agent at the provider
   boundary. `request_capture.retention` defaults to `20` and is clamped to
   `1..1000`. Each atomic pair is stored under
   `<HERMES_HOME>/sessions/request-captures/capture_ID/` as `with_tools.json`
   and `prompt_only.json` after structural secret/URL-query redaction.

Example:

```yaml
request_capture:
  enabled: true
  retention: 20
```

Enable the estimate with `hermes plugins enable request-dump`. Restart the CLI
or gateway after changing plugin or capture configuration. Captures contain
sensitive conversation context even after redaction; protect and delete them
accordingly. Tests: `tests/plugins/test_request_dump_plugin.py` and the request
capture cases in `tests/run_agent/`.

## 6. Optional observability and cleanup plugins

### Langfuse path-like text hardening

**Status:** deployment-gated.
**Owners:** `fce5c418c4bb`.

- Multiline text beginning with a local-looking path is converted to structured
  text before Langfuse sees it, preventing the SDK from treating an entire tool
  payload as a media path and raising path-length errors.
- Enable with `hermes plugins enable observability/langfuse` or the tools UI.
  Required env vars are `HERMES_LANGFUSE_PUBLIC_KEY` and
  `HERMES_LANGFUSE_SECRET_KEY`; legacy `LANGFUSE_PUBLIC_KEY` and
  `LANGFUSE_SECRET_KEY` are accepted by runtime fallback. Optional:
  `HERMES_LANGFUSE_BASE_URL` (default `https://cloud.langfuse.com`),
  `HERMES_LANGFUSE_ENV`, `HERMES_LANGFUSE_RELEASE`,
  `HERMES_LANGFUSE_SAMPLE_RATE` (default `1.0`),
  `HERMES_LANGFUSE_MAX_CHARS` (default `12000`), and
  `HERMES_LANGFUSE_DEBUG`.
- The SDK and credentials are cached on first initialization; restart after
  enabling the plugin or correcting credentials. Test:
  `tests/plugins/test_langfuse_plugin.py`.

### Recursive wildcard disk cleanup

**Status:** deployment-gated.
**Owners:** `7a54c4f9c6c3`.

- Tracked entries ending in `/*` recursively prune aged files and newly empty
  descendants while preserving the wildcard parent. Legacy non-wildcard
  directory entries cannot delete a whole protected directory.
- The `disk-cleanup` plugin runs `post_tool_call` and `on_session_end` hooks.
  Retention rules remain: tests immediately, temp after 7 days, cron output
  after 14 days, and empty directories under its safe roots.
- No new key/env was introduced. Operations remain confined to `HERMES_HOME`
  and `/tmp/hermes-*`. The plugin must be listed in `plugins.enabled`; restart
  the hosting process after changing plugin enablement.
  Test: `tests/plugins/test_disk_cleanup_plugin.py`.

## 7. Webhook authentication and local automation

### Google Pub/Sub OIDC

**Status:** deployment-gated.
**Owners:** `68fd6df29844`.

- A webhook route with `auth: oidc` or `google_oidc` verifies the Authorization
  bearer token against Google JWKS, expected audience, accepted issuer, and an
  authorized caller identity. Audience-only configuration is rejected.
- Exact route keys under `platforms.webhook.extra.routes.ROUTE`:
  `auth`; `oidc.audience` (required); one or both of `oidc.email` and
  `oidc.subject` (at least one required); optional `oidc.issuer`; fixed Google
  `oidc.jwks_url`; and `oidc.leeway_seconds` (default `30`). Default accepted
  issuers are `accounts.google.com` and `https://accounts.google.com`. Custom
  JWKS URLs are rejected. Legacy flat aliases remain accepted for audience,
  issuer, email, subject, and JWKS URL; nested values win.
- Prerequisites: webhook platform enabled, `aiohttp`, PyJWT/crypto, network
  access to Google JWKS, and a Pub/Sub push subscription configured with the
  matching audience/caller.
- Example:

  ```yaml
  platforms:
    webhook:
      enabled: true
      extra:
        routes:
          pubsub-alerts:
            auth: oidc
            oidc:
              audience: https://hooks.example.invalid/webhooks/pubsub-alerts
              email: pubsub-pusher@example-project.iam.gserviceaccount.com
            deliver: log
  ```

- Restart the gateway after static route/auth changes. Dynamic routes are
  validated when loaded. Tests: `tests/gateway/test_webhook_adapter.py` and
  `test_webhook_dynamic_routes.py`.

### Guarded script-trigger routes

**Status:** deployment-gated.
**Owners:** `6108023a95b3`, `b08a62a9cb98`, `b11a4a6d5708`.

- Authenticated routes may run an allowlisted profile-local script without
  forwarding request-body text. Trigger execution is route-local single-flight,
  uses stable delivery IDs, returns retryable `429` backpressure during active
  work/cooldown, and scopes rate/idempotency/session state by routed profile.
- Global keys under `platforms.webhook.extra`:
  - `script_triggers_enabled: false`
  - `script_trigger_allowlist: []`
  - `script_timeout_seconds: 30`
  - existing `rate_limit: 30` requests/minute and
    `max_body_bytes: 1048576` still apply.
- Route keys: `script`, `script_mode: trigger`, optional
  `script_timeout` (may shorten, never extend, the global ceiling), and
  `script_cooldown_seconds` (default `10`).
- Scripts must resolve inside `<HERMES_HOME>/scripts/`, survive symlink
  resolution inside that root, and match the allowlist. They run without a
  shell and receive no request body on stdin. Non-empty redacted stdout may be
  delivered; empty stdout is silent.
- Restart after static config changes. Tests: `test_webhook_adapter.py` covers
  auth ordering, path confinement, dedupe, per-route concurrency, retry, and
  multiplexed profile isolation.

## 8. Cron lifecycle, prompts, and calendar synchronization

### Generic lifecycle hooks and COMPLETE output payload

**Status:** implemented-and-wired.
**Owners:** `be1d00981b3b`, `cf6cf763ba4a`.

- `cron.hooks` exposes `CREATE`, `UPDATE`, `REMOVE`, and `COMPLETE`. Jobs and the
  scheduler emit them on mutations, one-shot cleanup, and completion. Callback
  exceptions are logged and swallowed so extensions cannot break cron.
- COMPLETE includes `output_file`, allowing external consumers to attach saved
  output without calendar-specific code in the scheduler.
- No config/env/defaults. Registration is in-process; plugins register callbacks
  at startup, so enabling/disabling a consumer requires process restart.
- Tests: `tests/cron/test_hooks.py` and `test_hook_wiring.py`.

### File-backed cron prompts

**Status:** implemented-and-wired.
**Owners:** `00bbb91d7a11`.

- Jobs can store `prompt_path`; the scheduler reads the file at each run and
  combines inline prompt text before file contents. Tool, core storage,
  scheduler, and HTTP API create/update paths all carry the field.
- `prompt_path` must be absolute, exist at execution, be a regular UTF-8 file,
  and be at most `1048576` bytes. No config or environment gate.
- Example tool/API semantics: create with `prompt_path` instead of `prompt`, or
  provide both to prepend a short instruction. On update, an empty string clears
  it. File edits take effect at the next tick; no restart is required.
- Caveat: jobs run in fresh sessions; the prompt must be self-contained. Tests:
  `tests/cron/test_jobs.py`, `test_cron_context_from.py`,
  `tests/tools/test_cronjob_tools.py`, and
  `tests/gateway/test_api_server_jobs.py`.

### Delivery diagnostics

**Status:** implemented-and-wired.
**Owners:** `dc9b2a2091a1`.

- Cross-platform cron delivery no longer emits an irrelevant thread warning;
  actual warnings/failures use an attention prefix. No new config. Tests:
  `test_cron_delivery_thread_diagnostics.py` and `test_scheduler.py`.

### Cron-to-Google-Calendar plugin

**Status:** deployment-gated.
**Owners:** `0cf7d3dea6ca`, `ad4b57478df3`, `5b6a3cab14c4`.

- The plugin subscribes to the generic cron lifecycle hooks, creates/updates
  managed events, preserves user-edited titles, represents high-frequency jobs
  as all-day series, archives ended series, adopts/reconciles surviving events,
  retries failed duplicate archives, learns run duration, and attaches sanitized
  final output (maximum `3500` characters).
- `cron.calendar_sync.enabled` defaults to `true` **inside the plugin**, but the
  plugin self-disables unless prerequisites are present. `calendar_id` defaults
  to `Hermes crons`. Sidecar state is
  `<HERMES_HOME>/cron/calendar_sync.json`; initial event duration is 300 seconds,
  minimum learned duration is 60 seconds, and schedules at or below six hours
  use the high-frequency representation.
- Prerequisites: plugin enabled/loaded, the active profile's `google-workspace`
  skill scripts, that profile's Google OAuth token, Calendar API access, and
  write policy allowing create/update. The worker subprocess receives the
  active `HERMES_HOME`; it does not borrow another profile's credentials.
- Example:

  ```yaml
  cron:
    calendar_sync:
      enabled: true
      calendar_id: Hermes crons
  ```

- Restart the gateway/cron host after enabling the plugin or changing its
  startup registration. Calendar failures are best-effort and never fail the
  cron job. Tests are collected under
  `tests/plugins/cron_calendar_sync/`, including Ops parity and mixed-timezone
  matching.

## 9. Discord text, media, channel, and profile behavior

### Markdown and media directive correctness

**Status:** implemented-and-wired.
**Owners:** `636486e1f56d`, `6a5ce8913f8f`.

- Discord outbound prose escapes Markdown markers outside fenced and inline code
  while preserving code content. A `MEDIA:` directive is recognized only on a
  standalone line (optionally indented, blockquoted, or prefixed by the voice
  marker); prose and inline-code mentions remain visible.
- No new config. Updated code takes effect on process restart/deployment. Tests:
  `test_discord_format.py`, `test_discord_reply_mode.py`,
  `test_platform_base.py`, and `test_stream_consumer.py`.

### Free-response thread behavior and slash-command sync retry

**Status:** deployment-gated.
**Owners:** `5767e5dc8774`, `b519aa4389ac`.

- Tracked Discord threads remain eligible for free responses even without a
  parent-channel cache entry. Slash-command fingerprints persist only after
  Discord accepts registration, so transient API failures retry on reconnect.
- Existing Discord gates apply: `discord.free_response_channels` and slash
  registration settings/permissions. Contrary to the current upstream prose,
  executable code/tests show free-response channels still auto-thread when
  `discord.auto_thread` is true unless excluded by
  `discord.no_thread_channels` / `DISCORD_NO_THREAD_CHANNELS`.
- Restart/reconnect is required to resync commands. Tests:
  `test_discord_free_response.py` and `test_discord_connect.py`.

### Outbound channel-policy fence

**Status:** deployment-gated.
**Owners:** `634d96a7ec95`.

- Text, file, image, animation, voice, cron/tool, origin, and standalone REST
  sends enforce the resolved profile's Discord channel policy. Threads inherit
  their parent channel's policy. A denied profile cannot send into a channel it
  would reject inbound.
- Exact policy inputs are `discord.allowed_channels` /
  `DISCORD_ALLOWED_CHANNELS` and `discord.ignored_channels` /
  `DISCORD_IGNORED_CHANNELS`. Empty allow/ignore sets are unrestricted;
  ignored wins; `*` in ignored denies all; `*` in allowed allows all.
- Changes to startup-resolved adapter config require gateway restart. Tests:
  `test_discord_outbound_channel_fence.py` plus the send/file/image suites.

### Profile-pinned gateway state

**Status:** implemented-and-wired.
**Owners:** `c47b4668c6e4`, `5feace5be2c1`.

- Background cron, `/model` persistence, prompt/config/cache paths, Discord
  command fingerprints, recovery ledgers, and non-conversational state remain
  pinned to the gateway's startup profile home.
- Uses normal profile selection (`HERMES_HOME`, `HERMES_PROFILE`, CLI `-p`) and
  `gateway.multiplex_profiles`; no new user-facing key. Restart only when changing
  deployment/profile selection. Tests: `test_profile_scoping.py`,
  `test_model_command_profile_scope.py`, and `test_multiplex_adapter_registry.py`.

## 10. Kanban features

### Board inventory DB paths

**Status:** implemented-and-wired.
**Owners:** `f7c7bdf63073`.

- Board list/show resolves each board's actual on-disk DB rather than applying
  `HERMES_KANBAN_DB` to the inventory. When that task-operation override is
  present, the CLI reports the pin separately.
- No new config. Test: `tests/hermes_cli/test_kanban_boards.py`.

### Card branch metadata

**Status:** implemented-and-wired.
**Owners:** `55dd6e921b05`.

- `branch_name` persists for `dir` and `worktree` workspaces, flows through CLI
  and dashboard create/update APIs, and renders on cards/drawers. Main/master
  branches are labeled `(main)`; worktree branches are labeled `(worktree)`.
- Usage: pass `branch_name` with `workspace_kind: dir|worktree` and an absolute
  `workspace_path`, or use the corresponding Kanban CLI/API field. No restart.
- Tests: `tests/hermes_cli/test_kanban_db.py`, `test_kanban_cli.py`, and
  `tests/plugins/test_kanban_dashboard_plugin.py`.

### Persisted workspace and branch validation

**Status:** implemented-and-wired.
**Owners:** `6b030c108557`.

- This mixed late hardening remains at its dependency-safe position rather than
  being folded into the earlier card-display feature. It centralizes
  `normalize_workspace_metadata` at create/update persistence boundaries.
- Every supplied workspace path must be absolute; a `dir` workspace additionally
  requires a path after board-default resolution. Scratch workspaces cannot
  carry a branch; Git-valid `@` is allowed; pseudo-ref
  `HEAD`, option-like, whitespace, control, and forbidden-ref characters are
  rejected. No config or restart is required.
- Tests: `tests/hermes_cli/test_kanban_db.py` and dashboard update validation.

### Notification routing policy and notifier diagnostics

**Status:** implemented-and-wired.
**Owners:** `5ff4cccf24d4`, `5ce7243d0f30`.

- All subscription entry points and notifier send-time delivery pass through one
  policy. The notifier logs tick/routing outcomes so idle queues and missed
  delivery can be distinguished.
- `kanban.notification_policy` defaults:
  `mode: origin`, `allowed_platforms: []`, `preserve_tui: true`.
  `mode: telegram_home_only` reroutes disallowed targets to the active notifier
  profile's Telegram home channel; `allowed_platforms` exempts named platforms;
  TUI remains local unless `preserve_tui: false`.
- `kanban.auto_subscribe_on_create` remains `true` by default and is an existing
  parent gate for automatic subscriptions.
- Caveat: `DEFAULT_CONFIG` currently contains two top-level `kanban` literals;
  the later dispatcher block overwrites the earlier template block containing
  `auto_subscribe_on_create` and `notification_policy`. Explicit user config and
  runtime fallback defaults above work, but generated/default config does not
  expose them correctly.
- Example:

  ```yaml
  kanban:
    notification_policy:
      mode: telegram_home_only
      allowed_platforms: []
      preserve_tui: true
  ```

- Telegram rerouting requires that profile's Telegram bot/home-channel config.
  Use `hermes kanban notify-audit` to find existing noncompliant rows. Restart is
  not generally required for CLI operations; long-running notifier processes
  should be restarted after policy changes. Tests: `test_kanban_notifications.py`,
  `test_kanban_notifier.py`, and dashboard/tool policy cases.

### Worker-only lifecycle guidance

**Status:** implemented-and-wired.
**Owners:** `f2a84c5936c5`.

- `KANBAN_GUIDANCE` is injected only when `HERMES_KANBAN_TASK` identifies a
  dispatcher-spawned worker, not merely because `kanban_show` is available.
  Ordinary Discord/orchestrator sessions may use Kanban tools without being
  misclassified as single-card workers.
- No new config. Worker dispatch sets the environment. Test:
  `tests/tools/test_kanban_tools.py`.

### Durable Discord mirror and reply/conversation routing

**Status:** deployment-gated.
**Owners:** `8d8db3acb7e4`, `ba6316148dfc`, `36a1dcedd618`.

- Cards mirror to Discord forum threads; replies/reactions route back to card
  comments or owner instructions. Durable state, binding epochs, outbox/inbox,
  recovery, reconciliation, terminal summaries/digests/tags, idle archive, and
  restart recovery live beside the Discord platform plugin. Gateway startup and
  shutdown call the plugin runtime directly.
- `kanban.discord_mirror` defaults:
  - `enabled: false`, `board: default`, `forum_channel_id: ""`, `guild_id: ""`
  - `token_env_path: <HERMES_HOME>/.env`
  - `poll_seconds: 10.0`, `prose_interval_seconds: 60.0`
  - `max_post_chars: 3800`, `note_char_limit: 900`, `digest_title: Board`
  - `done_thread_archive_idle_minutes: 60.0`
  - `binding_transitions_enabled`, `terminal_lifecycle_enabled`,
    `reconciliation_enabled`, `automatic_successor_enabled`: all `false`.
  - Advanced reconciliation/lifecycle/successor gates require
    `binding_transitions_enabled: true` or startup validation fails.
  - `closed_thread_reply_policy` defaults to discard archived/locked/missing
    destinations; supported actions are `discard`, `redirect`, and
    `reopen_thread`, with explicit failure-policy mappings.
- `discord.kanban_reply_inbox` defaults:
  - `enabled: false`, `forum_channel_ids: []`
  - `allow_commands: [comment, block, unblock]`, `default_action: comment`,
    `ack: true`, `board_slug: null`, `allow_thread_level_messages: false`
  - `conversation_log_enabled: false`, `conversation_router_enabled: false`
  - `conversation_router_ingress_bot_id: null`, `profile_bot_user_ids: {}`.
- Discord backfill defaults are `kanban_backfill_page_size: 100`,
  `kanban_backfill_max_pages: 10`, and
  `kanban_backfill_max_age_seconds: 604800`.
- Conversation routing additionally requires `gateway.multiplex_profiles: true`;
  the ingress bot ID must appear in `profile_bot_user_ids`; every mapped profile
  must exist and have the expected connected Discord identity; router board/forum
  values must match the mirror config. Validation fails closed.
- Minimal example:

  ```yaml
  kanban:
    discord_mirror:
      enabled: true
      board: operations
      forum_channel_id: "123456789012345678"
      guild_id: "234567890123456789"
  discord:
    kanban_reply_inbox:
      enabled: true
      forum_channel_ids: ["123456789012345678"]
      board_slug: operations
  ```

- Discord bot credentials and forum permissions are prerequisites. Restart the
  gateway after configuration changes. Tests include
  `tests/e2e/test_discord_kanban_router_restart_acceptance.py`, the
  `test_kanban_mirror_*` suites, and
  `tests/plugins/test_discord_kanban_mirror_plugin.py`.

## 11. Gateway restart operations

### Gateway restart plugin

**Status:** deployment-gated.
**Owners:** `334acf3f93a8`.

- The standalone plugin exposes `request_gateway_restart`, using the live
  gateway's drain-aware restart path for the invoking profile and detached
  `hermes -p PROFILE gateway restart` children for explicitly allowed remote
  profiles. Batch order schedules remote profiles before the invoking profile.
- Requests require a non-empty reason and exact confirmation text
  `restart gateway`; `dry_run: true` validates without restarting. Audit records
  are appended under `<HERMES_HOME>/logs/gateway-restart-tool.jsonl`.
- Config under `plugins.entries.gateway-restart-tool` includes `enabled: true`
  after plugin enablement, plus:
  `allowed_target_profiles: []` (the invoking profile is always included),
  `cooldown_seconds: 300`, and `schedule_delay_seconds: 3.0` (minimum `0.5`).
  Cooldowns are persisted per target and reserved atomically across processes;
  failed scheduling releases only its own reservation.
- Enable the plugin and expose its `gateway_restart` toolset through the normal
  plugin/tool configuration, then restart the gateway. Local real restarts work
  only inside a live `GatewayRunner`; remote targets require valid installed
  profiles and a working profile-specific gateway supervisor/CLI path.
- Example call:

  ```json
  {"reason":"apply reviewed configuration","confirm":"restart gateway","dry_run":true}
  ```

- Tests: `tests/plugins/test_gateway_restart_tool.py`, including policy,
  cross-process reservation, Windows byte-range locking, batch ordering,
  cooldown isolation, and synchronous/asynchronous scheduling failures.

## 12. Compression and session continuity

**Status:** implemented-and-wired.
**Owners:** `83fd737806de`, `ed3b4e81bb40`, `e8488361a291`.

- Compression preserves spoken-session modality context. At the next compaction
  boundary, a stale protected summary is replaced instead of stacked. Manual
  `/compress` treats continuation creation and transcript persistence as one
  logical commit, avoiding duplicate parent flushes and rolling back an
  uncommitted child if either core or gateway persistence fails.
- No new config or environment variable. Existing compression/session storage
  settings apply. Code deployment is sufficient; no data migration is required.
- Tests: `test_context_compressor_voice_preservation.py`,
  `test_context_compressor_summary_continuity.py`, and
  `tests/gateway/test_compress_session_db.py`.

## 13. CI, test, release, and dependency policy

### Fork dependency/CI/release policy

**Status:** implemented-and-wired in repository automation.
**Owners:** `2e516c951320`, `1a32065b6c1f`, `98e9b8005bf2`.

- Dependabot retains the GitHub Actions ecosystem declaration but does not open
  scheduled Actions update PRs in this fork.
- OSV scanning and SARIF artifact generation run in forks; unsupported code-scan
  publication remains restricted to upstream.
- Contributor attribution maps the fork owner's commit email through the current
  contributor-file mechanism without modifying the frozen legacy map.
- No runtime config, environment variable, deployment, or gateway restart.
  Tests: `tests/ci/test_osv_workflow_policy.py` and
  `test_contributor_workflow_base.py`.

### Parallel-test and live-system-guard isolation

**Status:** implemented-and-wired in the test runner.
**Owners:** `5f9c82e30b77`, `903a22741f9a`, `66c2bcf84807`.

- Each file-level pytest subprocess receives a unique `--basetemp`, cleaned on
  every exit path. The live-system guard parses executable positions rather than
  banning harmless argument text. Retained multi-profile fixtures use temporary
  homes and declare the runtime dependencies their integration paths exercise.
- No runtime config. Tests: `tests/test_run_tests_parallel.py`,
  `test_live_system_guard_self_test.py`, and the profile-aware fixture suites.

### Desktop DOMPurify security update

**Status:** implemented-and-wired.
**Owners:** `2aea1d07d373`.

- Desktop dependency metadata and `package-lock.json` select a DOMPurify version
  past `GHSA-c2j3-45gr-mqc4`.
- No runtime config. A desktop dependency install/build is required for packaged
  artifacts to contain the updated dependency; no gateway restart is relevant.

## 14. Removed or superseded implementation surfaces

### `discord.stt_aliases`

**Status:** removed.
**Owners:** introduced by `6551554f91f2`, removed/superseded by `e1530115af56`.

Use profile-local `voice/commands.toml` instead. The old top-level key does not
create a Discord platform entry and has no effect. Restart the gateway after
editing the TOML catalog.

### Permissive shadow adapter resolver

**Status:** removed.
**Owners:** `38a2d27bd702`.

The duplicate resolver that could encourage fallback to the primary adapter was
removed. The retained profile-safe resolver is the only runtime path. There is
no compatibility setting.

## Consolidated configuration reference

Only fork-relevant keys are listed. “Absent” means the current parser treats a
missing key as the shown value.

| Key/path | Default | Gate/effect |
|---|---:|---|
| `memory.provider` | `""` | `hindsight` enables provider-specific memory tools |
| `agent.disabled_toolsets` | `[]` | `memory` and `hindsight` can be disabled independently |
| `platform_toolsets.PLATFORM` | platform defaults | May explicitly include `hindsight` or other granted toolsets |
| `skills.external_dirs` | `[]` | External skill manifests participate in cache invalidation |
| `tools.tool_search.enabled` | `auto` | `auto`, `on`, or `off` |
| `tools.tool_search.threshold_pct` | `10` | Auto-activation threshold, `0..100` |
| `tools.tool_search.search_default_limit` | `5` | Default bridge-search result count |
| `tools.tool_search.max_search_limit` | `20` | Hard request bound, `1..50` |
| `tools.tool_search.pinned_toolsets.default` | absent / `[]` | Pins visible toolsets for every platform |
| `tools.tool_search.pinned_toolsets.PLATFORM` | absent / `[]` | Adds pins for the active platform |
| `tts.elevenlabs.voice_id` | `pNInz6obpgDQGcFmaJgB` | ElevenLabs voice |
| `tts.elevenlabs.model_id` | `eleven_multilingual_v2` | ElevenLabs generation model |
| `tts.elevenlabs.speed` | `tts.speed` or `1.0` | Voice speed, clamped `0.7..1.2` when sent |
| `tts.elevenlabs.stability` | absent | Optional `0..1` |
| `tts.elevenlabs.similarity_boost` | absent | Optional `0..1` |
| `tts.elevenlabs.style` | absent | Optional `0..1` |
| `tts.elevenlabs.use_speaker_boost` | absent | Optional boolean |
| `discord.voice_fx.enabled` | `false` | Intended master gate; currently unwired |
| `discord.voice_fx.ambient_enabled` | `true` | Ambient channel gate |
| `discord.voice_fx.ambient_path` | `""` | Empty uses synthesized audio |
| `discord.voice_fx.ambient_gain` | `0.18` | Idle ambient gain |
| `discord.voice_fx.duck_gain` | `0.06` | Ambient gain during speech |
| `discord.voice_fx.speech_gain` | `1.0` | Speech/ack gain |
| `discord.voice_fx.ack_enabled` | `true` | First-tool acknowledgement gate |
| `discord.voice_fx.ack_phrases` | `Let me look into that.`, `One moment.`, `Checking on that now.`, `Give me a sec.`, `On it.` | Intended first-tool phrase pool; mixer path unwired |
| `discord.voice_fx.cancellation_ack_phrases` | `Sure thing.`, `Ignored.`, `Consider it unsaid.` | Cancellation phrase pool |
| `discord.voice_fx.model_switch_ack_phrases` | `Model switched.` | Model-switch phrase pool |
| `discord.voice_fx.join_ack_phrases` | `[]` | Join phrase pool |
| `discord.voice_fx.busy_ack_phrases` | `[]` | Busy phrase pool |
| `discord.voice_fx.restart_join_ack_phrases` | `Back online.` | Restart/rejoin phrase pool |
| `discord.voice_fx.session_resume_ack_phrases` | `Picking up where we left off.` | Session-resume phrase pool |
| `discord.voice_fx.session_resume_user_turn_threshold` | `2` | Resume acknowledgement threshold |
| `voice/commands.toml:[stt_aliases]` | absent | Profile-local exact STT rewrites |
| `voice/acknowledgements.yaml` | absent | Profile-local schema-v2 ack catalog |
| `discord.auto_voice_channel_id` | `null` | Optional positive auto-join channel ID |
| `discord.auto_voice_user_ids` | `[]` | Authorized auto-voice users |
| `discord.free_response_channels` | `""` | Mention-free channel set |
| `discord.allowed_channels` | `""` | Optional channel allowlist |
| `discord.ignored_channels` | absent / `""` | Channel denylist; takes precedence |
| `discord.auto_thread` | `true` | Auto-thread eligible Discord messages |
| `discord.no_thread_channels` | `""` | Explicit channel exclusions from auto-threading |
| `request_capture.enabled` | `false` | Provider-boundary first-request capture |
| `request_capture.retention` | `20` | Complete capture pairs, clamped `1..1000` |
| `platforms.webhook.extra.script_triggers_enabled` | `false` | Global script-trigger gate |
| `platforms.webhook.extra.script_trigger_allowlist` | `[]` | Resolved script allowlist |
| `platforms.webhook.extra.script_timeout_seconds` | `30` | Global script ceiling |
| `platforms.webhook.extra.rate_limit` | `30` | Requests per route per minute |
| `platforms.webhook.extra.max_body_bytes` | `1048576` | Request-body ceiling |
| `platforms.webhook.extra.routes.ROUTE.auth` | HMAC path | `oidc` enables Google token verification |
| `...oidc.audience` | required for OIDC | Expected audience |
| `...oidc.email` / `...oidc.subject` | at least one required | Authorized caller identity |
| `...oidc.issuer` | Google issuer pair | Accepted issuer(s) |
| `...oidc.jwks_url` | Google JWKS URL | Custom URLs are rejected |
| `...oidc.leeway_seconds` | `30` | JWT clock-skew allowance |
| route aliases `oidc_audience`, `oidc_issuer`, `oidc_email`, `oidc_subject`, `oidc_jwks_url` | absent | Legacy flat aliases; nested OIDC keys win |
| `...script` | absent | Profile-local transform/trigger script |
| `...script_mode` | transform mode | `trigger` selects guarded execution |
| `...script_timeout` | global ceiling (`30`) | May shorten, never extend, global timeout |
| `...script_cooldown_seconds` | `10` | Per-route post-run cooldown |
| cron job `prompt_path` | absent | Absolute UTF-8 prompt file, max 1 MiB |
| `cron.calendar_sync.enabled` | plugin default `true` | Also requires plugin/skill/token availability |
| `cron.calendar_sync.calendar_id` | `Hermes crons` | Calendar name/ID passed to workspace policy |
| `kanban.notification_policy.mode` | `origin` | Supports `telegram_home_only` |
| `kanban.notification_policy.allowed_platforms` | `[]` | Policy exemptions |
| `kanban.notification_policy.preserve_tui` | `true` | Keeps TUI-local delivery |
| `kanban.auto_subscribe_on_create` | runtime fallback `true` | Template entry is currently overwritten by duplicate `kanban` default block |
| `kanban.discord_mirror.enabled` | `false` | Mirror daemon gate |
| `kanban.discord_mirror.board` | `default` | Board slug |
| `kanban.discord_mirror.forum_channel_id` | `""` | Required for a valid enabled mirror |
| `kanban.discord_mirror.guild_id` | `""` | Discord guild scope |
| `kanban.discord_mirror.token_env_path` | `<HERMES_HOME>/.env` | Profile token source |
| `kanban.discord_mirror.poll_seconds` | `10.0` | Mirror polling interval |
| `kanban.discord_mirror.prose_interval_seconds` | `60.0` | Prose update throttle |
| `kanban.discord_mirror.max_post_chars` | `3800` | Discord post bound |
| `kanban.discord_mirror.note_char_limit` | `900` | Note excerpt bound |
| `kanban.discord_mirror.digest_title` | `Board` | Digest title |
| `kanban.discord_mirror.done_thread_archive_idle_minutes` | `60.0` | Idle archive delay |
| `kanban.discord_mirror.binding_transitions_enabled` | `false` | Parent gate for advanced lifecycle modes |
| `kanban.discord_mirror.terminal_lifecycle_enabled` | `false` | Terminal thread lifecycle |
| `kanban.discord_mirror.reconciliation_enabled` | `false` | Discord/local reconciliation |
| `kanban.discord_mirror.automatic_successor_enabled` | `false` | Successor automation |
| `kanban.discord_mirror.closed_thread_reply_policy` | discard closed/missing | Rules may discard, redirect, or reopen |
| `discord.kanban_reply_inbox.enabled` | `false` | Reply ingestion gate |
| `discord.kanban_reply_inbox.forum_channel_ids` | `[]` | Allowed forum IDs |
| `discord.kanban_reply_inbox.allow_commands` | comment/block/unblock | Accepted owner commands |
| `discord.kanban_reply_inbox.default_action` | `comment` | Bare-reply action |
| `discord.kanban_reply_inbox.ack` | `true` | Reply acknowledgement |
| `discord.kanban_reply_inbox.board_slug` | `null` / `default` at use | Routed board |
| `discord.kanban_reply_inbox.allow_thread_level_messages` | `false` | Permit non-reply thread messages |
| `discord.kanban_reply_inbox.conversation_log_enabled` | `false` | Conversation-log gate |
| `discord.kanban_reply_inbox.conversation_router_enabled` | `false` | Multi-profile router gate |
| `discord.kanban_reply_inbox.conversation_router_ingress_bot_id` | `null` | Required router ingress identity |
| `discord.kanban_reply_inbox.profile_bot_user_ids` | `{}` | Bot-ID to profile routing map |
| `discord.kanban_backfill_page_size` | `100` | Discord mirror backfill page size |
| `discord.kanban_backfill_max_pages` | `10` | Backfill page bound |
| `discord.kanban_backfill_max_age_seconds` | `604800` | Backfill age bound |
| `gateway.multiplex_profiles` | `false` | Required by conversation router/multi-bot voice |
| `plugins.entries.gateway-restart-tool.enabled` | plugin disabled until enabled | Loads tool registration |
| `plugins.entries.gateway-restart-tool.allowed_target_profiles` | `[]` plus invoking profile | Cross-profile restart allowlist |
| `plugins.entries.gateway-restart-tool.cooldown_seconds` | `300` | Per-target restart cooldown |
| `plugins.entries.gateway-restart-tool.schedule_delay_seconds` | `3.0` | Local graceful-restart delay, minimum `0.5` |

Fork-relevant environment variables and profile files:

| Name/path | Purpose/default |
|---|---|
| `HERMES_HOME` | Active profile root and all profile-local state |
| `HERMES_PROFILE` | Active profile identity where used by plugin/runtime selection |
| `GATEWAY_MULTIPLEX_PROFILES` | Recognized operator override for multiplexing; invalid/blank falls back to config |
| `HERMES_SESSION_PLATFORM` | Tool-search platform context when gateway context is unavailable |
| `ELEVENLABS_API_KEY` | ElevenLabs credential |
| `HINDSIGHT_API_KEY` | Hindsight Cloud credential |
| `HINDSIGHT_MODE` | `cloud`; provider mode |
| `HINDSIGHT_API_URL` | Default Hindsight cloud endpoint |
| `HINDSIGHT_BANK_ID` / `HINDSIGHT_BUDGET` | Defaults `hermes` / `mid` |
| `HINDSIGHT_TIMEOUT` / `HINDSIGHT_IDLE_TIMEOUT` | Defaults `120` / `300` seconds |
| `HINDSIGHT_LLM_API_KEY` | Optional local-embedded LLM credential |
| `DISCORD_ALLOWED_CHANNELS` / `DISCORD_IGNORED_CHANNELS` | Fallback channel-policy sources |
| `DISCORD_FREE_RESPONSE_CHANNELS` | Mention-free Discord channel set |
| `DISCORD_AUTO_THREAD` / `DISCORD_NO_THREAD_CHANNELS` | Auto-thread gate and exclusions |
| `HERMES_LANGFUSE_PUBLIC_KEY` / `HERMES_LANGFUSE_SECRET_KEY` | Langfuse credentials |
| `HERMES_LANGFUSE_BASE_URL` | Langfuse endpoint; default cloud service |
| `HERMES_LANGFUSE_ENV`, `HERMES_LANGFUSE_RELEASE` | Optional trace tags |
| `HERMES_LANGFUSE_SAMPLE_RATE` | Default `1.0` |
| `HERMES_LANGFUSE_MAX_CHARS` | Default `12000` |
| `HERMES_LANGFUSE_DEBUG` | Optional verbose plugin logging |
| `<HERMES_HOME>/voice/commands.toml` | STT alias catalog, cached at adapter startup |
| `<HERMES_HOME>/voice/acknowledgements.yaml` | Voice acknowledgement catalog, cached at adapter startup |
| `<HERMES_HOME>/cron/calendar_sync.json` | Calendar plugin sidecar state |
| `<HERMES_HOME>/sessions/request-captures/` | Sensitive provider-boundary diagnostic pairs |

## Commit coverage ledger

Every rewritten commit after the base is assigned above:

| Commit | Feature/role |
|---|---|
| `e024744b1ada` | Atomic structural config mutations |
| `d63dd88d258e` | Worktree-safe launcher handling |
| `445c814bac3f` | Dual-stack browser port selection |
| `d07ed4f8f269` | ElevenLabs settings |
| `7cf53fe45983` | Hindsight toolset decoupling |
| `0241eb894aca` | Platform-scoped pinned toolsets |
| `fce5c418c4bb` | Langfuse path-text hardening |
| `7a54c4f9c6c3` | Recursive wildcard cleanup |
| `68fd6df29844` | Webhook OIDC |
| `6108023a95b3` | Webhook script triggers |
| `be1d00981b3b` | Cron lifecycle hooks |
| `dc9b2a2091a1` | Cron delivery diagnostics |
| `00bbb91d7a11` | File-backed cron prompts |
| `cf6cf763ba4a` | COMPLETE output hooks |
| `636486e1f56d` | Discord Markdown escaping |
| `5767e5dc8774` | Free-response thread behavior |
| `b519aa4389ac` | Slash-command sync retry |
| `6551554f91f2` | Initial configurable STT aliases; superseded by profile catalog |
| `634d96a7ec95` | Outbound channel-policy fence |
| `6a5ce8913f8f` | Standalone MEDIA directives |
| `f11ca377e79c` | Gateway display abbreviation |
| `c47b4668c6e4` | Profile-pinned cron/model state |
| `f7c7bdf63073` | Real per-board DB inventory |
| `55dd6e921b05` | Kanban card branch metadata |
| `8d8db3acb7e4` | Initial Discord Kanban mirror/reply routing |
| `5ff4cccf24d4` | Kanban notification policy |
| `c5665e35e00f` | Profile-scoped voice I/O/orchestration |
| `5feace5be2c1` | Profile-pinned Discord durable state |
| `83fd737806de` | Spoken context through compression |
| `ed3b4e81bb40` | Protected-summary replacement |
| `e8488361a291` | Atomic manual compression rotation |
| `b935eceaf8b5` | External-skill cache invalidation |
| `5ce7243d0f30` | Kanban notifier diagnostics |
| `b08a62a9cb98` | Route-local webhook serialization |
| `b11a4a6d5708` | Profile-scoped webhook delivery state |
| `e4344218855f` | Model-specific bounded execution guidance |
| `2e516c951320` | Dependabot fork policy |
| `1a32065b6c1f` | Fork OSV scanning |
| `98e9b8005bf2` | Fork contributor attribution |
| `5f9c82e30b77` | Parallel pytest basetemp isolation |
| `903a22741f9a` | Process-killer guard parsing |
| `70adc9bec2d3` | Text-preserving TTS SDK fallback |
| `66c2bcf84807` | Profile-aware CI fixtures/dependencies |
| `c1cce48f703a` | Request estimate and provider-boundary capture |
| `f2a84c5936c5` | Worker-only Kanban guidance |
| `0cf7d3dea6ca` | Calendar plugin introduction |
| `334acf3f93a8` | Multi-profile gateway restart plugin |
| `e9b448b2c7f5` | Voice command acknowledgements |
| `ffce6eed74a4` | `/stop` interrupts TTS |
| `50c0f59b86de` | Voice filler normalization |
| `d71f3509479b` | Profile acknowledgement catalogs |
| `38a2d27bd702` | Removed shadow adapter resolver |
| `e1530115af56` | Profile-local STT catalog migration |
| `d0b90d532f34` | Hindsight history command |
| `ba6316148dfc` | Durable Kanban mirror lifecycle/recovery |
| `36a1dcedd618` | Kanban mirror platform-plugin extraction |
| `ad4b57478df3` | Calendar Ops parity |
| `5b6a3cab14c4` | Calendar CI discovery/timezone fix |
| `b488245ddbd0` | Discord voice platform-plugin extraction |
| `6b030c108557` | Persisted Kanban workspace/branch validation |
| `2aea1d07d373` | DOMPurify security update |
