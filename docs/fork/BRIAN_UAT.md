# Brian-only functional verification and product-decision log

INVENTORY MILESTONE product gate. This is not a current approval request and does not authorize implementation or retirement; see `NEXT_SLICE.md`.

Only irreducible product, privacy, or operational decisions belong here. The authoritative source/evidence/disposition remains the single row for each ID in [`FEATURE_DISPOSITION.md`](FEATURE_DISPOSITION.md). None of these decisions is needed for the inventory checkpoint; they gate only future migration of the named clusters.

| Cluster | Decision Brian must make | Why delegation cannot settle it | Gate state |
|---|---|---|---|
| FC-11 | Retire Calendar synchronization, or commission a fresh cron-provider/standalone-plugin rewrite? | The old plugin id remains enabled while the feature flag is false; code evidence cannot infer desired future workflow. | Deferred; no migration card until Brian opts in. |
| FC-41 | Is `/new (<prompt>)` still a desired interaction? | Only Brian can establish current workflow value for the shorthand and its destructive-confirmation semantics. | Deferred; approval required before gateway-core design. |
| FC-42 | Should private `/log` capture and nightly journal exist, and what are its retention/privacy rules? | Usefulness and acceptable personal-data retention are subjective. | Privacy gate; no implementation or data migration authorized. |
| FC-43 | Should a Personal History category be blocked from Hindsight, and how is the boundary identified? | A name-based block is policy, not a universal memory invariant. | Privacy gate; isolate from FC-42 and require explicit approval. |
| FC-44 | Does Brian want a separate release-switch controller beyond current update/drain/restart procedures? | A human must judge the production approval/recovery UX and whether the extra controller reduces or adds risk. | Critical lifecycle/release gate; no implementation authorized. |

All other non-KEEP clusters are settled by automated evidence, an independent engineering review, or a future bounded reproduction. Brian is **not needed now** for this documentation-only checkpoint; a decision is needed only before reviving one of the five rows above.
