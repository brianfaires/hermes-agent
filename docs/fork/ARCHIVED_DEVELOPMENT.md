# Loose development ends — pre-v0.21.0 reconstruction

Captured: 2026-09-03T07:23:31Z

Policy: preserve every non-empty candidate as a local backup ref and remote branch before removing its worktree. Never imply that preservation equals approval for migration. Generated caches and `test_durations.json` are disposable.

| Branch | Tip / state | Candidate | Current disposition |
|---|---|---|---|
| `fix/kanban-ready-integrity` | `25df328f8a27`, clean | Keep operator holds out of ready queue | Preserve for v0.21 comparison; do not migrate without current reproduction. |
| `wt/t_0086da14` | equals `main`, clean | No branch delta | Remove worktree; no push needed. |
| `wt/t_0c4486a1` | recovered as `0fb8e78acf`, 15 focused tests pass | Incident-evidence closure utility | Preserve branch as unreviewed recovery; exclude from Hermes fork because repository does not own incident evidence lifecycle. |
| `wt/t_132d2ba8` | `366a66d266`, clean | Disk-cleanup plugin preserves durable profile scripts | Preserve; re-evaluate against v0.21 plugin behavior. |
| `wt/t_16cc80ba` | equals `main`, clean | No branch delta | Remove worktree; no push needed. |
| `wt/t_277d82ac` | `8bedb6fea8`, clean | Tavily quota fallback | Preserve; require v0.21 reproduction and provider-extension analysis. |
| `wt/t_3f347ffe` | `e6fab7b8e1`, clean | Personal History Log Hindsight deny gate | Preserve; privacy-sensitive and not a low-risk first slice. |
| `wt/t_5211e5ce` | `4d93606e52`, clean | Calendar stale-output recovery | Preserve; plugin-only candidate, verify v0.21 compatibility. |
| `wt/t_757e3e93` | `1008d1fdbc`, clean | Historical npm lockfile remediation | Preserve for traceability; regenerate security assessment from v0.21 rather than replaying pins. |
| `wt/t_817455a6` | recovered as `2581b9cd8c`, 158 focused tests pass | Backup scan-scope display correction | Preserve as recovered candidate; reproduce against v0.21 before inclusion. |
| `fix/kanban-worker-reclaim-safety` | `738e4a093a`, clean | Prevent replacement writer while timed-out worker survives | Preserve; core safety patch requiring v0.21 reproduction and review. |
| `wt/t_96ba6ce8` | `d509b7fb67`, clean | Stream full backup archives | Preserve; core CLI change, compare with v0.21 backup implementation. |
| `wt/t_af7f24ea` | `6c120b4a44`, clean, already on origin | Inactive release-switch controller | Preserve remote branch; likely replace/reduce using v0.21 gateway control socket. Human walkthrough only if retained. |
| `wt/t_bd621e31` | `28b065d32e`, clean | Cron lifecycle relay to Ops | Preserve; plugin-first candidate with external Ops consumer dependency. |
| `wt/t_c8b3ecc9` | `a485633e0b`, clean, already on origin | Private `/log` and nightly journal | Preserve remote branch; high privacy/UX risk, not a low-risk first slice. |
| `staging` worktree | `07677f36f5`, clean, matches origin | Integration checkout | Remove redundant worktree; preserve branch/ref. |
| `wt/t_df3b3320` | `cd560d7dab`, clean | Profile-scoped reminder mirror | Preserve; compare with v0.21 selective multiplex before deciding. |
| `wt/t_01f1e8fa` | `8c2b17a0dc`, clean | Telegram `/new` inline prompt delivery | Preserve; reproduce against v0.21 gateway before deciding. |
| `wt/t_2d5377a6` | `4c40304a96`, clean | Isolate Google Workspace behind local broker | Preserve; provenance/task mismatch requires review, and integration should remain outside core. |
| `fix/security-managed-venv-2026-09` | `75a40f9503`, clean | Historical managed-venv CVE remediation | Preserve for traceability; regenerate from v0.21 dependencies instead of replaying. |
| `fix/tornado-6.5.8` | equals `staging`, clean | Historical Tornado remediation already represented by staging | Remove redundant worktree; no separate feature delta. |

## Safety evidence

- Pre-cleanup source refs are under `refs/backup/hermes-v0.21.0-precleanup-20260903/`.
- Manifest: `precleanup-ref-manifest.json`; SHA-256: `0832ed66ea7ee0c9d5da0ee7ce9f380fd23729075f56e79c07a54ad4c3c9ebd6`.
- Recovered dirty patches were checksummed before committing.
- No non-primary worktree had a live process during the audit.
- Worktree removal is paused until remote preservation can use the required `ang-ineering` GitHub identity.
