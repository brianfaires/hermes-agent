"""Real shared restart preflight in dry-run mode; no restart is scheduled."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class RestartPreflightQualification(unittest.TestCase):
    def test_active_work_all_profiles_is_reported_without_reservation(self):
        from gateway.run import GatewayRunner
        from gateway.session_context import set_session_vars, clear_session_vars
        path = Path(__file__).resolve().parents[2] / 'plugins/gateway-restart-tool/__init__.py'
        spec = importlib.util.spec_from_file_location('v021_preflight_plugin', path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'HOME': tmp, 'HERMES_HOME': tmp + '/home', 'HERMES_BUNDLES_DIR': tmp + '/bundles',
        }):
            home = Path(tmp) / 'home'
            home.mkdir()
            (home / 'config.yaml').write_text('plugins:\n  enabled: [gateway-restart-tool]\n')
            runner = object.__new__(GatewayRunner)
            runner._running_agents = {'caller': SimpleNamespace(), 'secondary': SimpleNamespace()}
            runner._active_cron_job_count = lambda: 1
            runner._active_api_run_count = lambda: 1
            runner.request_restart = Mock(side_effect=AssertionError('dry-run scheduled restart'))
            tokens = set_session_vars(platform='telegram', session_key='caller')
            try:
                with patch('gateway.run._gateway_runner_ref', lambda: runner), patch.object(plugin, '_restart_modes', return_value=(False, True)):
                    result = json.loads(plugin._handle_request_gateway_restart({
                        'reason': 'isolated preflight', 'confirm': 'restart gateway', 'dry_run': True,
                    }))
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['restart_scope'], 'all_profiles')
                self.assertEqual(result['active_agents'], 2)
                self.assertEqual(result['active_work'], 4)
                self.assertTrue(result['dry_run'])
                runner.request_restart.assert_not_called()
                self.assertFalse(plugin._state_path().exists())
                audit = [json.loads(line) for line in plugin._audit_path().read_text().splitlines()]
                self.assertEqual([r['decision'] for r in audit], ['dry_run'])
                self.assertEqual(audit[0]['restart_scope'], 'all_profiles')
            finally:
                clear_session_vars(tokens)


if __name__ == '__main__':
    unittest.main()
