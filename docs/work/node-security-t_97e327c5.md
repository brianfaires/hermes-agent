# Node 26.13.1 security pin qualification

## Ask
Update managed Node to exactly 26.13.1 on every supported target (task `t_97e327c5`). Preserve unrelated dependencies, old installed runtimes, and deployment gates.

## Plan
- [x] Resolve Termux's exact upstream version and optional numeric packaging revision from its existing distribution index; reject mismatched versions, noncanonical URLs and malformed hashes.
- [x] Run the native Node-only lock command and bootstrap generator.
- [x] Download all nine artifacts and compare complete-byte SHA256 to their existing providers' HTTPS indexes.
- [x] Prove resolver regressions fail against the original implementation and pass with the correction; exercise nearby PM/bootstrap coverage.
- [ ] Independent review, staging integration and exact-head hosted CI.
- [ ] Supported deployment followed by Ops installation and consumer acceptance.

## State
2026-11-16: based on `e55db73863a4a3c866ed5ebdd17b0ca97dca0617`. The original resolver fails all 13 new parameterized cases. Broader verification caught the existing bionic contract test's fixed `-1` assumption; it now accepts optional numeric revisions with a seeded index, without network access. The corrected candidate passes 62 tests across ten files through `scripts/run_tests.sh` (one Windows-only test skipped on Linux) and all 14 `scripts/check` gates. The isolated, verified Linux x64 archive's binary reports `v26.13.1`; the installed prior runtime still reports `v26.12.1`. The native `pm.cli.cmd_lock` entry point for `--bump node 26.13.1` completed in isolated state. Generated installer fragments are byte-identical because they embed uv/Windows Git, not Node.

All previous targets remain: Darwin arm64/x64, Linux arm64/x64, Linux arm64/x64 musl, Linux arm64 bionic, Windows arm64/x64. Sources remain `nodejs.org`, `unofficial-builds.nodejs.org` (musl), and `packages.termux.dev` (bionic). Termux's package control fields report `nodejs`, `26.13.1`, `aarch64`; its complete-byte SHA256 matches the package index. The prior bionic artifact was 26.4.0 despite the global 26.12.1 lock version; it now matches 26.13.1. Other lock entries and Python/npm dependency files are unchanged.

Source preparation is not a patched-live-runtime claim. No runtime installation, dependency synchronization, garbage collection, launcher change or gateway restart was performed. Full cross-platform execution remains the hosted CI gate; artifact availability/hash qualification alone is not native execution on every target.

## Next
Ang owns independent review, staging and the source deployment receipt. Ops card `t_1adf089f` owns the subsequent supported Node-only installation and real consumer startup verification. Normal canonical source deployment remains gated; do not install from an unpromoted worktree or bypass the no-restart restriction. Keep old runtime entries. Before deployment, rollback is dropping this unlanded candidate; after landing, revert the coherent source commit and use the supported prior pin/entry selection rather than hand-editing facts or replacing binaries.
