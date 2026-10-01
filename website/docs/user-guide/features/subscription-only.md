# Subscription-only scheduled work

`subscription_only` is an opt-in restriction on one cron job, Kanban task,
or CLI execution. Existing jobs and tasks default to `false`; profile
configuration and the global fallback chain are unchanged.

The initial supported subscription route is **openai-codex** with an explicit
model. The pilot uses **gpt-6.1-sol**. An unsupported provider, missing model,
non-boolean policy, or custom endpoint is rejected. The resolved official
Codex endpoint is allowed. Authentication, quota, network, and context failures
cannot activate another inference provider.

```sh
hermes chat --subscription-only --provider openai-codex -m gpt-6.1-sol -q 'Your task'
hermes cron create '30m' 'Your task' --subscription-only --provider openai-codex --model gpt-6.1-sol
hermes kanban create 'Your task' --subscription-only --provider openai-codex --model gpt-6.1-sol
```

The Python `create_job` and `create_task` APIs accept `subscription_only=True`.
Cron uses `provider` and `model`; Kanban uses `provider_override` and
`model_override`. Dashboard create requests accept the same boolean and their
existing route fields. Cron edit/update supports setting the policy, validating
the resulting complete record. JSON readback exposes `subscription_only`.
Existing cron records without the field mean `false`; existing Kanban databases
gain a column defaulting to false.

Delegated agents inherit the restriction, including reviews. Conflicting
delegation configuration fails before credential resolution. Tasks created by
restricted execution inherit its policy and route; Kanban dependency children,
worker CLI children, and triage decomposition also retain the persisted policy.
The existing cron model tool cannot set provider/model pins: use the user-owned
CLI/API fields, or create children within restricted execution to inherit them.

Cron creation through a Kanban worker's CLI subprocess also recovers the
restriction and route from the existing persisted task identity. Unavailable
worker state and invalid restricted route overrides fail closed.

This deliberately narrow mode disables auxiliary inference (including all
speech synthesis and streaming speech), external memory
inference, and background reviews. Hermes summarization raises a policy error
instead of trying auxiliary providers or silently dropping context. Native
Codex compaction stays on the subscription route. These limitations can cause
jobs requiring those features to fail; failures must be handled by the caller.

The restriction uses execution context and agent state, not a mutable process
flag. It governs Hermes-managed inference; it is not an operating-system
sandbox for arbitrary scripts, shell commands, or third-party plugins.
