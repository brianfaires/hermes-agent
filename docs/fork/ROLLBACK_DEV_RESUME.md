# Parked rollback development

Brian requested preservation on a development branch for later resumption. This is a checkpoint, not a release candidate or an instruction to activate the machinery.

- Resume branch: `dev/rollback`.
- Preserved source tip: `13fc25cb79affa85bb2e8ed53fb969a6344897ef`, formerly `ang/v021-production-bindings` (original ref retained).
- Six development commits absent from current main: bootstrap/runtime bindings, pre-stop recovery safety, timing bounds, Feishu readiness and websocket-open handshake, and health-refusal diagnostics.
- Main at preservation: `56fd41d2f05fdfa4c90658c44fc76c8e4a36c306`. No merge, rollback, runtime/config change, or restart was performed to create this checkpoint.
- Work was already committed and clean; this checkpoint changes documentation only. No new tests or qualification are claimed.

## Resume boundary

Inspect the six commits and their tests before restarting implementation. Reconcile with current main in a separate development worktree; do not blindly replay the old bootstrap path. The approved operational default is now direct independent Claude release execution, not this parked machinery. No automatic install, scheduler, recovery daemon, or deployment is authorized by this branch.

Historical local evidence remains outside Git under `/home/brian/.hermes/profiles/ang/state/release-switches/v021-20260908/`. The later release acceptance is in the sibling `v021-main-20260909/ACCEPTANCE.md`. These are local references, not portable artifacts; private logs, snapshots, credentials, and runtime configuration must not be copied into this branch. Consult current release runbooks and establish fresh review/test/backup/approval evidence before any future activation. Live rollback was not exercised by the successful v0.21 release.
