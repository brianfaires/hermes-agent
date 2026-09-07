# Feature omission and retained-candidate index

This is an index into [FEATURE_DISPOSITION.md](FEATURE_DISPOSITION.md), the sole authoritative behavior and historical item inventory. Absence alone never means dropped. All five accepted candidates are now integrated as source; whole-rebase completion, hosted-CI qualification, deployment and feature activation are not claimed.

| Decision | Cluster IDs | Recovery rule |
|---|---|---|
| `DROP_UPSTREAM` | FC-04, FC-12, FC-14, FC-15, FC-17, FC-18, FC-19, FC-20, FC-21, FC-24, FC-26, FC-27, FC-28, FC-29, FC-30, FC-32, FC-33, FC-34, FC-48 | Use current behavior; do not replay legacy patches. |
| `DROP_LOW_VALUE` | FC-01, FC-06, FC-09, FC-10, FC-23, FC-31, FC-35 | Reopen only with new value evidence and placement review. |
| `DROP_OUT_OF_SCOPE` | FC-38, FC-39, FC-46 | External/Ops ownership; unchanged by this integration. |
| `SUPERSEDED` | FC-43 | Brian’s intentional finalized journal decision supersedes the historical blanket block; no accidental chat ingestion or mass backfill/deletion. |

Retained source (`KEEP` or implemented `REWRITE`): FC-02, FC-03, FC-05, FC-07, FC-08, FC-08B, FC-11, FC-13, FC-16, FC-22, FC-25, FC-28B, FC-36, FC-37, FC-40, FC-41, FC-42, FC-44, FC-45, FC-47, FC-49. FC-11/37/41/42/44 are integrated candidates; operational limitations are indexed in [BRIAN_UAT.md](BRIAN_UAT.md). FC-37 is an OS-owner CLI, NOT a chat slash command; its current-memory reconstruction accepts either a valid injective `{profile}` bank template or an explicit static `bank_id` / legacy `banks.hermes.bankId` when the template is absent or empty. FC-44 is standalone and sandbox-qualified, not installed/production-qualified. SD-01/02 remain supplemental historical branch deltas.

Programmatically computed accounting: 21 retained + 29 explicit drops + 1 superseded = 51 clusters; 0 deferred. The 157 individual historical items remain unchanged, once each in the master appendix.

Release owner `t_ddd2e9dc`; Ops prerequisite `t_907703d2`. Old incident/reconstruction graphs remain held. Other features unchanged. See [NEXT_SLICE.md](NEXT_SLICE.md) for the fixed original release window and remaining parent-owned gates.
