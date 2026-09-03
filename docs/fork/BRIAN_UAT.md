# Brian-only functional verification log

Only behaviors that cannot be credibly verified through automated tests, delegated UAT, or Ang's own live smoke tests belong here. Inclusion does not bypass risk or release approval.

| Candidate behavior | Why Brian is genuinely required | State |
|---|---|---|
| Private `/log` capture and nightly journal | The final interaction model, privacy expectations, and usefulness are subjective and belong to Brian. Automated tests can prove isolation and retention but cannot prove the workflow is desirable. | Conditional: request only if the feature survives the value and placement gates. |
| Inactive release-switch controller | If retained, Brian must walk the explicit approval/recovery UX because it mediates future human-authorized production transitions. Automated tests can prove state-machine invariants, not operational clarity. | Conditional: v0.21 gateway-control capabilities may eliminate or substantially reduce it. |

All other candidates default to automated verification plus delegated platform UAT. Add an item only when Brian's judgment or physical interaction is irreducible.
