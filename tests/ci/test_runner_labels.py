"""Workflows name only runners this fork has.

The fork has GitHub-hosted runners and nothing else. A job that names a
Blacksmith or larger-runner label is not rejected: it queues until the 24-hour
timeout with no failing check, so a future upstream merge that reintroduces one
is invisible unless something refuses it here. Labels reach a job through
`runs-on`, matrix values, `workflow_call` inputs and `${{ }}` expressions, so
every string value in every workflow and action is checked, not only `runs-on`.
"""
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
GITHUB = ROOT / ".github"

UNAVAILABLE_RUNNER = re.compile(
    r"blacksmith-"  # Blacksmith runners
    r"|-\d+-(?:arm-)?cores?\b"  # GitHub larger runners, e.g. windows-latest-32-core
    r"|\b\d+vcpu\b"  # sized runner labels
)


def _config_files():
    return sorted(
        path
        for pattern in ("workflows/*.y*ml", "actions/**/*.y*ml", "actionlint.yaml")
        for path in GITHUB.glob(pattern)
    )


def _values(node):
    """Every scalar value (mapping keys excluded), so comments never count."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _values(value)
    elif isinstance(node, str):
        yield node


@pytest.mark.parametrize("path", _config_files(), ids=lambda path: str(path.relative_to(GITHUB)))
def test_workflows_name_only_runners_the_fork_has(path):
    document = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    offenders = sorted({value for value in _values(document) if UNAVAILABLE_RUNNER.search(value)})
    assert not offenders, f"{path.name} names runners this fork cannot schedule: {offenders}"


@pytest.mark.parametrize(
    "label",
    ["blacksmith-4vcpu-ubuntu-2404", "windows-latest-32-core", "windows-latest-32-arm-core", "ubuntu-latest-8-cores"],
)
def test_policy_rejects_unschedulable_labels(label):
    assert UNAVAILABLE_RUNNER.search(label)


@pytest.mark.parametrize(
    "label", ["ubuntu-24.04", "ubuntu-24.04-arm", "windows-2025", "windows-11-arm", "macos-15", "macos-latest"]
)
def test_policy_accepts_github_hosted_labels(label):
    assert not UNAVAILABLE_RUNNER.search(label)
