# Passive Discord ingress evidence

The adapter writes `INFO` records with message prefix `discord_ingress ` and a
single JSON object through its existing logger. This uses Hermes' existing
asynchronous queue, redacting formatter, gateway component filter, and rotating
files; it adds no daemon, network call, schedule, configuration, or recovery
behavior. The existing WebSocket liveness loop supplies `progress` samples.
The post-authorization `gateway_platform_event` hook is not used: it cannot
observe pre-admission rejects and is not a transport coverage signal.

## Version 1 record

- `v`: 1.
- `profile`: explicit adapter owner, otherwise the profile captured at adapter
  construction (which can be `custom` for a nonstandard home).
- `home`: construction-time transport home. Never the ambient routed turn home.
- `bot_id`: connected client user ID; null before identity is available.
- `pid`, `generation`: process ID and random per-adapter observation generation.
  Generation identifies an adapter lifetime, not a Discord session/resume ID.
- `seq`: increasing observation-attempt number within the generation.
- `received_count`: cumulative live `on_message` callbacks, including bot/self
  events and rejected messages; not a count of authorized human requests.
- `timestamp`: observation Unix time in seconds; `monotonic`: process monotonic
  clock in seconds. Neither is the message creation time.
- `message_id`, `channel_id`, `thread_id`, `parent_id`: native incoming IDs as
  decimal strings, or null. Snowflakes are accepted only as positive uint64
  SDK integers. A thread uses its own ID as both channel and thread ID.
  IDs describe the incoming location, not a subsequently auto-created thread.
- `stage`, `reason`: fixed code-owned values below.

No text, username, user ID, attachment URL, SDK object representation, exception
text, or content-derived string is added to these records. Existing unrelated
logs have their existing contents: consumers must extract only these records,
not copy or retain the shared log. This is local operational evidence, not
cryptographically authenticated audit data.

## Stages and limits

| Stage | Reason | Meaning |
| --- | --- | --- |
| received | on_message | SDK callback entered before waiting for adapter readiness |
| admitted | adapter_policy | Preliminary dedup/type/user/bot/mention gate passed; channel policy may still reject |
| rejected | duplicate, self_message, message_type, bots_disabled, bot_mention_required, bot_inline_mention_required, user_not_allowed, other_bot_mentioned, mention_required | Admission gate rejected |
| rejected | channel_not_allowed, channel_ignored, mention_required, auto_thread_failed, mention_only | Later handler gate rejected |
| dispatched | handoff | About to enter base `handle_message`; includes private-command path |
| dispatched | batched | Successfully queued in existing text batching |
| error | cancelled, dispatch_error | Live handler interrupted/raised; original exception semantics unchanged |
| progress | healthy, not_ready, client_closed, socket_state_unavailable, socket_closed, ack_unavailable, ack_stale, latency_non_finite, latency_exceeded, health_check_error | Existing WebSocket health sample |

`dispatched` is NOT gateway authorization, agent execution, delivery success,
or completion. A base-handler rejection or a later batching failure is outside
this observation boundary. Recovery/backfill calls do not emit live-ingress
records. Slash interactions, edits, deletes, voice and reactions are not covered.

Observation failures do not change delivery. Sequence increments precede logging;
missing queue writes, rotation loss, disabled logging, or observer exceptions
may therefore leave holes. No record is a durable-write acknowledgment. If
logging is completely lost, only a stale/missing tail remains.

## Reader contract

1. Bind the exact deployed logger plus `(profile, home, bot_id)` and current
   process identity. Never count a shared-channel event from a different bot.
   Null/unknown identity is insufficient. Verify runtime SHA separately.
2. Parse the real log envelope (`timestamp INFO [optional session] logger:
   discord_ingress JSON`), not substring matches in user previews. Validate
   JSON fields and version. Never retain other shared-log lines.
3. Read current and retained rotated files as one bounded window. Group by
   owner, PID and generation, order by `seq`, deduplicate identical records;
   contradictory duplicates or holes make the affected interval UNKNOWN.
   Detect read/rotation races and cap exhaustion; retry read-only or report
   UNKNOWN, never infer absence from an incomplete window.
4. A quiet interval requires progress records bracketing that interval in the
   same generation, fresh endpoint samples, expected sample spacing, healthy
   transport samples, contiguous observation sequence, and unchanged
   `received_count`. This means *observed quiet*, not proven delivery health.
   A stale timestamp, clock anomaly, missing bracket, generation change, dropped
   history, disabled liveness probe, or missing records means UNKNOWN coverage.
   Use monotonic deltas within one process; wall time alone can jump.
5. The default progress interval is 15 seconds. Consumers must use the deployed
   liveness configuration and their own bounded grace period. This feature
   does not enable a disabled probe or change the existing reconnect policy.
6. Even healthy ACKs and contiguous local logs cannot prove Discord delivered
   every event, that channel permissions cover all expected channels, or that
   an SDK callback was never lost before entry. Missing expected messages need
   separately authorized independent observations; silence alone is not a drop.

## Binding and validation

In a shared gateway, logs normally reside in the launching process home's
`logs/gateway.log` and `.1` through `.3` (5 MiB per file), not each transport
profile's home. Logger names can be plugin-loader scoped; record the deployed
name rather than hardcoding the module import name used in tests. Records also
follow the existing catch-all `agent.log` sink; retention is unchanged.

Use offline fixtures, not synthetic traffic against Discord:

    HERMES_PYTHON=/absolute/verified/venv/bin/python bash scripts/run_tests.sh -j 2 tests/gateway/test_discord_ingress_observation.py

The fixtures exercise real admission/handler decisions and the existing queued
rotating logger with temporary homes. A one-shot deployment check should read
only local files with explicit owner/logger/PID binding, print scalar summaries
only, and perform no sends, REST calls, restarts, replay, or configuration edits.
Record source qualification separately from installation and live evidence.
The feature is not live until the corresponding source release is deployed.
