# Brian's Hermes fork reconstruction

This branch reconstructs the local fork on upstream Hermes `v2026.8.31` (`0.21.0`) rather than replaying the divergent historical stack.

## Immutable baseline

- Upstream release tag: `v2026.8.31`
- Peeled release commit: `29112bef099274229cadff79cdff7bf7b99c4b77`
- Reconstruction branch: `brian/reconstruct-v0.21.0`
- Base checkpoint: `brian-rebuild-v0.21.0-base`
- Protected source refs: `refs/backup/hermes-v0.21.0-precleanup-20260903/*`

## Migration policy

Each feature must pass an ongoing-value gate and an upstream-equivalence gate. Retained behavior should use configuration, skills, standalone or bundled plugins, and existing tool/adapter extension points before modifying upstream core. Every accepted slice receives focused verification and a pushed checkpoint tag. Omitted features and any functionality requiring Brian's manual verification are recorded beside this file.
