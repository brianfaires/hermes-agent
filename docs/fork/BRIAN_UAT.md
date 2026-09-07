# Brian-only functional verification and product-decision log

The 2026-09-06 release approval covers FC-16, FC-22, FC-28B and FC-36, now implemented. The rows below remain deferred; no question or live approval is requested during preparation. FC-37 is also explicitly deferred because Hindsight history privacy was not among the four approved features.

Only irreducible product, privacy, or operational decisions belong here. The authoritative source/evidence/disposition remains the single row for each ID in [`FEATURE_DISPOSITION.md`](FEATURE_DISPOSITION.md). These decisions gate only future migration of the named clusters.

| Cluster | Decision Brian must make | Why delegation cannot settle it | Gate state |
|---|---|---|---|
| FC-11 | Retire Calendar synchronization, or commission a fresh cron-provider/standalone-plugin rewrite? | The old plugin id remains enabled while the feature flag is false; code evidence cannot infer desired future workflow. | Deferred; no migration card until Brian opts in. |
| FC-41 | Is `/new (<prompt>)` still a desired interaction? | Only Brian can establish current workflow value for the shorthand and its destructive-confirmation semantics. | **Resolved — wanted.** Settled contract: `/new (<prompt>)` resets and delivers the enclosed text as the new session's first user message exactly once; `/new <name>` keeps title semantics; bare `/new` is unchanged; confirmation is never automatic. Implemented candidate recorded under FC-41 in [`FEATURE_DISPOSITION.md`](FEATURE_DISPOSITION.md) — **NOT deployed**. Independent blocker review is closed; final focused pytest: 111 passed. Duplicate inbound suppression uses the existing bounded process-local cache (300 seconds, 2,000 entries, transport ID required), not durable exactly-once delivery. Awaiting parent-owned integration/deploy decision. |
| FC-42 | Should private `/log` capture and nightly journal exist, and what are its retention/privacy rules? | Usefulness and acceptable personal-data retention are subjective. | Privacy gate; no implementation or data migration authorized. |
| FC-43 | Should a Personal History category be blocked from Hindsight, and how is the boundary identified? | A name-based block is policy, not a universal memory invariant. | Privacy gate; isolate from FC-42 and require explicit approval. |
| FC-44 | Does Brian want a separate release-switch controller beyond current update/drain/restart procedures? | A human must judge the production approval/recovery UX and whether the extra controller reduces or adds risk. | Critical lifecycle/release gate; no implementation authorized. |

FC-37 also remains deferred with no history import, retention change, or stored-data migration. Brian is not needed for the approved local release preparation. Hosted exact-SHA CI and the parent-owned live cutover remain separate operational gates.
