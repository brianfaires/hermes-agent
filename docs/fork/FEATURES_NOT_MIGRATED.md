# Features not included in the v0.21.0 fork

INVENTORY MILESTONE index into [`FEATURE_DISPOSITION.md`](FEATURE_DISPOSITION.md). Decisions below are accepted for reconstruction accounting only. Old follow-up cards under `t_6d9a0f05` remain incident-held; execution continues only via the recovery writer lane. See `NEXT_SLICE.md`.

This is a compact omission index. The authoritative behavior, source commits/refs, value evidence, and placement decision live exactly once in the master ledger. A historical feature is never considered dropped merely because it is absent from the rebuilt branch.

## Actionable follow-up

| Decision | Cluster IDs | Meaning |
|---|---|---|
| `KEEP` (pending migration) | FC-22 | Value and placement passed, but implementation is intentionally deferred (Critical state/lifecycle). |
| `REWRITE` | FC-36, FC-37 | Current value is proved, but the legacy implementation must be rebuilt at a current boundary. |
| `DEFER_REPRODUCTION` | FC-16, FC-28B | Run the bounded v0.21 reproduction card before deciding whether to migrate. |
| `DEFER_HUMAN_VALUE` | FC-11, FC-41, FC-42, FC-43, FC-44 | Do not implement until Brian resolves the product/privacy/lifecycle decision recorded in `BRIAN_UAT.md`. |

## Explicit drops

| Decision | Cluster IDs | Recovery rule |
|---|---|---|
| `DROP_UPSTREAM` | FC-04, FC-12, FC-14, FC-15, FC-17, FC-18, FC-19, FC-20, FC-21, FC-24, FC-26, FC-27, FC-28, FC-29, FC-30, FC-32, FC-33, FC-34, FC-48, FC-49 | Use the current v0.21 behavior named in the master ledger; do not replay legacy patches. |
| `DROP_LOW_VALUE` | FC-01, FC-06, FC-09, FC-10, FC-23, FC-31, FC-35 | Recover only after new value evidence and a fresh placement review. |
| `DROP_OUT_OF_SCOPE` | FC-38, FC-39, FC-46 | Route to the named external/Ops owner; do not add it to this repository. |

## Retained elsewhere

Already reconstructed/verified on this branch: FC-08, FC-08B, FC-02, FC-03, FC-05, FC-07, FC-13, FC-25, FC-40, FC-45, and FC-47.

Coverage check: remaining omitted/deferred/pending cluster IDs + retained cluster IDs = `51` total authoritative clusters.
