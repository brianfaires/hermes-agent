# Features not included in the v0.21.0 fork

This is the durable omission log. A dropped feature remains recoverable through its source branch and backup ref unless explicitly noted otherwise.

| Feature | Source | Decision | Why it does not make the cut | Recovery |
|---|---|---|---|---|
| Incident-evidence closure lifecycle inside Hermes Agent | `wt/t_0c4486a1` / `0fb8e78acf` | `DROP_OUT_OF_SCOPE` | Hermes does not create or own incident-evidence directories. The utility is operational tooling and belongs with Ops, not in upstream core or the fork. Its focused tests pass, but usefulness does not justify repository coupling. | Remote branch pending credential restoration; local backup refs and checksummed patch are preserved. |
| Empty task worktrees | `wt/t_0086da14`, `wt/t_16cc80ba` | `DROP_LOW_VALUE` | They contain no commit or working-tree delta from `main`; there is no feature to migrate. | Original `main` snapshot is preserved in backup refs. |
| Redundant Tornado task worktree | `fix/tornado-6.5.8` | `DROP_LOW_VALUE` | Its tip equals `staging`; it contains no independent feature beyond the preserved staging ref. Dependency state will be regenerated from v0.21. | `staging` and `origin/staging` backup refs preserve the exact tree. |

Future omissions must be appended with upstream evidence or an explicit value/ownership rationale; absence from the reconstruction branch is not sufficient documentation.
