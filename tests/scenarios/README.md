# Organization workflow scenarios

Tests here exercise organization-specific business workflows across Hermes components.
Named profiles, board conventions, role assignments and handoff policy are part of
these scenarios. General component behavior belongs in the source-mirroring test
directories, such as `tests/hermes_cli/` for Kanban unit tests.

Keep each scenario self-contained: create disposable profile homes and boards,
use real Hermes APIs, and script model decisions or worker launches where needed.
The shared fixtures in `tests/conftest.py` apply; fixtures in sibling test
directories do not.

Run these scenarios with:

```bash
scripts/run_tests.sh tests/scenarios/
```

The default test runner and Python CI discover this directory automatically.
Scenarios that require external services use the existing `integration` marker.

`test_kanban_orchestrator_flow.py` covers the Sunny → Rook → Ang/Ops → Rook
request-to-close-out workflow from shared ADR 0001 (#173). It uses a real board
and dispatcher with scripted workers, so it does not require external services
or verify model decisions and live gateway delivery.
