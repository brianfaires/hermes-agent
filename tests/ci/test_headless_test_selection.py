"""Fork CI keeps server and Home Assistant/Apple contracts while omitting unused suites."""
from argparse import Namespace
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
import hermes_yaml as yaml

from scripts.run_tests_parallel import _select_files


POLICY = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "headless-test-exclusions.txt"


def _select(root, paths, exclusions=None):
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    # Read the checked-in data that the workflow passes to the canonical runner.
    if exclusions is None:
        exclusions = [line for line in POLICY.read_text().splitlines() if line.strip() and not line.startswith("#")]
    args = Namespace(files=None, files_from=None, paths="tests", paths_positional=[], include_integration=False)
    selected, _roots = _select_files(args, exclusions, root)
    return {str(path.relative_to(root)) for path in selected}


def test_server_shared_plugin_homeassistant_and_apple_tests_are_selected(tmp_path):
    kept = {
        "tests/agent/test_future_server_feature.py",
        "tests/gateway/test_native_admission.py",
        "tests/gateway/test_lineage.py",
        "tests/gateway/test_bluebubbles_plugin.py",
        "tests/gateway/test_email.py",
        "tests/gateway/test_signal_format.py",
        "tests/gateway/test_slack_send_retry.py",
        "tests/gateway/relay/test_relay_slack_dm_streaming.py",
        "tests/gateway/test_sms.py",
        "tests/gateway/test_whatsapp_identity.py",
        "tests/gateway/test_whatsapp_cloud.py",
        "tests/plugins/platforms/whatsapp/test_inbound.py",
        "tests/plugins/platforms/whatsapp_cloud/test_inbound.py",
        "tests/gateway/test_canonical_tui_desktop_routes.py",
        "tests/plugins/platforms/photon/test_inbound.py",
        "tests/plugins/platforms/slack/test_slash_command_channel_prompt.py",
        "tests/plugins/apple/test_shortcuts.py",
        "tests/plugins/homeassistant/test_future_behavior.py",
        "tests/plugins/test_homeassistant_plugin.py",
        "tests/plugins/test_scoped_secret_readers_fail_closed.py",
        "tests/plugins/browser/test_browser_provider_plugins.py",
        "tests/plugins/web/test_web_search_provider_plugins.py",
        "tests/plugins/image_gen/test_openai_codex_provider.py",
        "tests/plugins/model_providers/test_future_provider.py",
        "tests/plugins/test_google_meet_audio.py",
        "tests/plugins/video_gen/test_deepinfra_provider.py",
        "tests/plugins/video_gen/test_fal_plugin.py",
        "tests/plugins/video_gen/test_openrouter.py",
        "tests/plugins/video_gen/test_xai_plugin.py",
        "tests/skills/apple/test_reminders.py",
        "tests/tui_gateway/test_headless_auth.py",
        "tests/hermes_cli/test_desktop_cron_ticker_profiles.py",
        "tests/scripts/install/test_core_install.py",
        "tests/pm/test_generation_lifetime.py",
    }
    omitted = {
        "tests/gateway/test_feishu.py",
        "tests/plugins/platforms/matrix/test_event_size.py",
        "tests/plugins/memory/test_retaindb_provider.py",
        "tests/scripts/desktop_update/test_posix_handoff.py",
        "tests/hermes_cli/test_desktop_source_build.py",
    }
    assert _select(tmp_path, kept | omitted) == kept


@pytest.mark.parametrize("suite", ["e2e", "integration", "docker"])
def test_native_expensive_suite_opt_ins_remain_effective(tmp_path, suite):
    kept = {"tests/cron/test_schedule.py"}
    assert _select(tmp_path, kept | {f"tests/{suite}/test_qualification.py"}) == kept


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("full", [False, True])
def test_workflow_full_qualification_restores_excluded_plugin_suites(tmp_path, full):
    root = POLICY.parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text())
    if full:
        workflow = yaml.safe_load((root / workflow["jobs"]["tests"]["uses"]).read_text())
        job, step_name = workflow["jobs"]["test"], "Run tests"
    else:
        job, step_name = workflow["jobs"]["headless-tests"], "Run the headless Ubuntu suite"
    command = next(step["run"] for step in job["steps"] if step.get("name") == step_name)
    policy = tmp_path / "scripts/ci/headless-test-exclusions.txt"
    policy.parent.mkdir(parents=True)
    policy.write_text(POLICY.read_text())
    capture = tmp_path / "args.json"
    runner = tmp_path / "scripts/run_tests.sh"
    runner.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                      f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n")
    runner.chmod(0o700)
    subprocess.run(["bash", "-c", command], cwd=tmp_path, check=True,
                   env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    kept = {"tests/cron/test_schedule.py"}
    plugins = {"tests/gateway/test_feishu.py", "tests/plugins/memory/test_retaindb_provider.py",
               "tests/plugins/test_chronos_cron.py", "tests/plugins/test_teams_pipeline_plugin.py"}
    assert _select(tmp_path, kept | plugins, json.loads(capture.read_text())) == (kept | plugins if full else kept)


@pytest.mark.platforms("linux")
def test_full_qualification_forces_python_and_plugin_suites_for_reusable_calls(tmp_path):
    workflow = yaml.safe_load((POLICY.parents[2] / ".github/workflows/ci.yaml").read_text())
    gate = next(step for step in workflow["jobs"]["detect"]["steps"] if step.get("id") == "gate-lanes")
    output = tmp_path / "outputs.txt"
    lanes = {"python": "false", "frontend": "false", "e2e": "false", "e2e_upgrade": "false"}
    inputs = {"${{ inputs.release }}": "false", "${{ inputs.full_qualification }}": "true",
              "${{ github.event_name }}": "workflow_call", "${{ toJSON(steps.classify.outputs) }}": json.dumps(lanes)}
    env = {key: inputs[value] for key, value in gate["env"].items()}
    env.update(PATH=os.environ["PATH"], GITHUB_OUTPUT=str(output))
    subprocess.run(["bash", "-c", gate["run"]], check=True, capture_output=True, text=True,
                   env=env)
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert all(selected[lane] == "true" for lane in lanes)
