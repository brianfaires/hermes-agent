"""Authentication fallback and /new notices agree with real requests (#48).

Uses the existing hermetic CLI home and loopback vendor servers. No resolver,
agent, reset or display method is replaced.
"""

import json
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tests.e2e.core.providers._openai_helpers import Home, chat_messages
from tests.fakes.providers.chat_variants import CError, CText, FakeChatVariantServer
from tests.gateway.test_normal_runtime_boot import control
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux")

_DRIVER = """
import contextlib
import io
import json
from cli import HermesCLI
from hermes_cli.runtime_provider import resolve_runtime_provider

# The vendor harness distinguishes primary turns by their offered tool schema.
# Responses contain text only; no tool is invoked.
console = HermesCLI(toolsets=['terminal'], compact=True, ignore_rules=True)
console._single_query_mode = True
primary_provider = resolve_runtime_provider(
    requested=console.requested_provider, target_model=console.model)['provider']
try:
    for prompt in ('FIRST-AUTH-FAILURE', 'SECOND-AFTER-RESET'):
        if prompt.startswith('SECOND'):
            console.new_session()
        answer = console.chat(prompt)
        status = io.StringIO()
        with contextlib.redirect_stdout(status):
            console.process_command('/status')
        print('ROUTE-RECEIPT ' + json.dumps({
            'prompt': prompt, 'answer': answer, 'session_id': console.session_id,
            'status': status.getvalue(),
            'owner_info': console.agent.connection.state(console.session_id).info,
            'resolved_primary_provider': primary_provider,
            'reasoning': console.reasoning_config,
            'agent_model': console.agent.model,
            'agent_provider': console.agent.provider,
        }), flush=True)
finally:
    if console.agent:
        console.agent.close()
"""


@pytest.mark.parametrize('unsupported', [False, True], ids=['default', 'unsupported'])
def test_auth_failure_fallback_then_reset_matches_requests_and_display(tmp_path, unsupported):
    def primary_response(record):
        texts = str(chat_messages(record['body'], 'user'))
        return (CError(401, 'synthetic expired credential', code='invalid_api_key')
                if 'FIRST-AUTH-FAILURE' in texts else CText('PRIMARY-AFTER-RESET'))

    with FakeChatVariantServer(primary_response) as primary, FakeChatVariantServer(
        [CText('FALLBACK-FIRST')]
    ) as backup:
        config = {
            'gateway': {'multiplex_profiles': False},
            'model': {'provider': 'openai', 'default': 'gpt-4.1-mini',
                      'base_url': primary.base_url, 'context_length': 128000},
            'custom_providers': [{'name': 'backup', 'base_url': backup.base_url,
                                  'api_key': 'synthetic', 'model': 'gpt-4.1-mini'}],
            'fallback_providers': [{'provider': 'custom:backup', 'model': 'gpt-4.1-mini'}],
            'auxiliary': {'title_generation': {'enabled': False}},
            'models_dev': {'url': 'http://127.0.0.1:9/api.json'},
        }
        if unsupported:
            config['model_overrides'] = {
                provider: {'gpt-4.1-mini': {'supports_reasoning': False}}
                for provider in ('openai', 'custom', 'custom:backup')}
        home = Home(tmp_path).write(config, {'OPENAI_API_KEY': 'synthetic',
                                            'OPENAI_BASE_URL': primary.base_url})
        root = Path(__file__).resolve().parents[2]
        with (tmp_path / 'gateway.log').open('w+') as log:
            # Own the fixture daemon directly. An auto-spawned daemon detaches
            # from the test subtree and cannot be signalled by the live guard.
            daemon = subprocess.Popen(
                [sys.executable, '-m', 'gateway.run'], cwd=root, env=home.env(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

            def ready():
                assert daemon.poll() is None, 'fixture daemon exited during startup'
                try:
                    return control(home.hermes_home, 'identify').get('state') == 'ready'
                except OSError:
                    return False

            try:
                wait_until(ready, timeout=40, what='owned fixture gateway readiness')
                run = subprocess.run(
                    [sys.executable, '-c', _DRIVER], cwd=home.project, env=home.env(),
                    capture_output=True, text=True, timeout=120, check=False)
            finally:
                if daemon.poll() is None:
                    daemon.send_signal(signal.SIGINT)
                    try:
                        daemon.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=5)
        primary_requests, backup_requests = primary.main_requests(), backup.main_requests()

    assert run.returncode == 0, run.stdout[-5000:] + run.stderr[-3000:]
    receipts = [json.loads(line.removeprefix('ROUTE-RECEIPT ')) for line in run.stdout.splitlines()
                if line.startswith('ROUTE-RECEIPT ')]
    assert len(receipts) == 2, run.stdout[-6000:]
    first, second = receipts
    assert backup_requests and primary_requests, (
        receipts, run.stdout[-5000:], run.stderr[-3000:])
    assert first['answer'] == 'FALLBACK-FIRST', first
    assert (first['agent_model'], first['agent_provider']) == ('gpt-4.1-mini', 'custom:backup'), first
    assert 'Model: gpt-4.1-mini (custom:backup)' in first['status'], first
    assert first['owner_info']['model'] == first['agent_model']
    assert first['owner_info']['provider'] == first['agent_provider']
    assert second['answer'] == 'PRIMARY-AFTER-RESET', second
    # An explicit loopback OpenAI endpoint resolves as the compatible custom
    # transport. Compare the actual native resolution, not the requested alias.
    expected_primary = second['resolved_primary_provider']
    assert (second['agent_model'], second['agent_provider']) == ('gpt-4.1-mini', expected_primary), second
    assert f'Model: gpt-4.1-mini ({expected_primary})' in second['status'], second
    assert second['owner_info']['model'] == second['agent_model']
    assert second['owner_info']['provider'] == second['agent_provider']
    assert first['session_id'] != second['session_id']
    assert all(body['model'] == 'gpt-4.1-mini' for body in primary_requests + backup_requests)
    assert any('SECOND-AFTER-RESET' in str(chat_messages(body, 'user')) for body in primary_requests)
    assert not any('SECOND-AFTER-RESET' in str(chat_messages(body, 'user')) for body in backup_requests)
    for body in primary_requests + backup_requests:
        if unsupported:
            assert 'reasoning_effort' not in body, body.get('reasoning_effort')
        else:
            # The existing compatible-provider default is medium, decided
            # at request time; no explicit effort is saved in the session.
            assert body.get('reasoning_effort') == 'medium'
    assert all(row['reasoning'] is None for row in receipts)
    assert all('Reasoning: high' not in row['status'] for row in receipts)
