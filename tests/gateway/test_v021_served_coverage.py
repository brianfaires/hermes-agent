"""Configured routing eligibility and successful adapter coverage stay distinct."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, SecondaryPortBindingConfigError
from gateway.status import read_runtime_status, write_runtime_status
from hermes_cli.profiles import get_multiplex_gateway_coverage
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override


class ServedCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.other = self.home / 'profiles' / 'worker'
        self.other.mkdir(parents=True)
        p = patch.dict(os.environ, {'HERMES_HOME': str(self.home), 'HOME': str(self.home)})
        p.start(); self.addCleanup(p.stop)
        p = patch('hermes_cli.profiles._get_default_hermes_home', return_value=self.home)
        p.start(); self.addCleanup(p.stop)
        self.runner = object.__new__(GatewayRunner)
        self.runner.config = GatewayConfig(multiplex_profiles=True)
        self.runner._launch_profile_home = self.home
        self.runner._launch_profile_name = 'default'
        self.runner.adapters = {}
        self.runner._profile_adapters = {}
        self.runner.pairing_store = object()
        self.runner.pairing_stores = {}

    def record(self):
        return read_runtime_status(self.home / 'gateway_state.json')

    async def test_startup_failed_and_adapterless_remain_eligible_not_connected(self):
        homes = [('default', self.home)] + [(n, self.other) for n in ('worker', 'empty', 'failed', 'port')]
        async def start(name, home, claimed):
            if name == 'failed':
                raise RuntimeError('isolated failure')
            if name == 'port':
                raise SecondaryPortBindingConfigError('invalid listener')
            self.runner._profile_adapters[name] = {Platform.DISCORD: object()} if name == 'worker' else {}
            return int(name == 'worker')
        with patch('gateway.run._multiplex_profile_homes', return_value=homes), patch.object(self.runner, '_start_one_profile_adapters', side_effect=start), patch('gateway.pairing.PairingStore'):
            count = await self.runner._start_secondary_profile_adapters()
        self.assertEqual(count, 1)
        self.assertEqual(set(self.record()['served_profiles']), {n for n, _ in homes})
        self.assertEqual(self.record()['connected_profiles'], ['worker'])

    def test_publication_pins_owner_and_restores_caller(self):
        self.runner._profile_adapters['worker'] = {Platform.DISCORD: object()}
        token = set_hermes_home_override(self.other)
        try:
            self.runner._publish_profile_coverage()
            self.assertEqual(get_hermes_home(), self.other)
        finally:
            reset_hermes_home_override(token)
        self.assertFalse((self.other / 'gateway_state.json').exists())
        self.assertEqual(self.record()['connected_profiles'], ['worker'])
        self.runner._publish_profile_coverage(clear=True)
        self.assertEqual(self.record()['connected_profiles'], [])

    async def test_single_profile_clears_previous_membership(self):
        write_runtime_status(connected_profiles=['worker'])
        self.runner.config = GatewayConfig(multiplex_profiles=False)
        self.assertEqual(await self.runner._start_secondary_profile_adapters(), 0)
        self.assertEqual(self.record()['connected_profiles'], [])

    async def test_fatal_removal_publishes_before_disconnect(self):
        adapter = SimpleNamespace(fatal_error_code='failed')
        self.runner._profile_adapters['worker'] = {Platform.DISCORD: adapter}
        self.runner._running = False
        async def disconnect(*args):
            self.assertEqual(self.record()['connected_profiles'], [])
        with patch.object(self.runner, '_safe_adapter_disconnect', side_effect=disconnect):
            await self.runner._handle_profile_adapter_fatal_error('worker', Platform.DISCORD, adapter)
        self.assertEqual(self.runner._profile_adapters['worker'], {})

    def test_reader_requires_explicit_membership_and_validated_owner(self):
        write_runtime_status(gateway_state='running', served_profiles=['worker'], connected_profiles=['worker'])
        with patch('gateway.status.get_runtime_status_running_pid', return_value=123) as live:
            coverage = get_multiplex_gateway_coverage('worker')
            self.assertEqual(coverage.pid, 123)
            self.assertEqual(live.call_args.kwargs['expected_home'], self.home.resolve())
            self.assertIsNone(get_multiplex_gateway_coverage('missing'))
        with patch('gateway.status.get_runtime_status_running_pid', return_value=None):
            self.assertIsNone(get_multiplex_gateway_coverage('worker'))
        for value in ([], 'worker', [123]):
            data = self.record(); data['connected_profiles'] = value
            (self.home / 'gateway_state.json').write_text(json.dumps(data))
            with patch('gateway.status.get_runtime_status_running_pid', return_value=123):
                self.assertIsNone(get_multiplex_gateway_coverage('worker'))

    def test_reader_uses_real_stopped_and_pid_reuse_fences(self):
        write_runtime_status(gateway_state='stopped', served_profiles=['worker'], connected_profiles=['worker'])
        self.assertIsNone(get_multiplex_gateway_coverage('worker'))
        write_runtime_status(gateway_state='running')
        with patch('gateway.status._pid_exists', return_value=True), patch('gateway.status._get_process_start_time', return_value='different-start'):
            self.assertIsNone(get_multiplex_gateway_coverage('worker'))

    def test_failed_publication_restores_scope(self):
        token = set_hermes_home_override(self.other)
        try:
            with patch('gateway.status.write_runtime_status', side_effect=OSError('unwritable')):
                self.runner._publish_profile_coverage()
            self.assertEqual(get_hermes_home(), self.other)
        finally:
            reset_hermes_home_override(token)

    def test_cli_renders_connected_coverage_without_false_service_failure(self):
        from hermes_cli.status import _show_gateway_service_status
        write_runtime_status(gateway_state='running', served_profiles=['worker'], connected_profiles=['worker'])
        snapshot = SimpleNamespace(running=False, manager='manual', gateway_pids=[], has_process_service_mismatch=False, service_installed=True, service_running=False)
        for connected in (True, False):
            write_runtime_status(connected_profiles=['worker'] if connected else [])
            output = io.StringIO()
            with patch('hermes_cli.gateway.get_gateway_runtime_snapshot', return_value=snapshot), patch('hermes_cli.profiles.get_active_profile_name', return_value='worker'), patch('gateway.status.get_runtime_status_running_pid', return_value=123), contextlib.redirect_stdout(output):
                _show_gateway_service_status()
            if connected:
                self.assertIn('served by multiplexer', output.getvalue())
                self.assertIn('connected through PID 123', output.getvalue())
                self.assertNotIn('installed but stopped', output.getvalue())
            else:
                self.assertNotIn('served by multiplexer', output.getvalue())
                self.assertIn('installed but stopped', output.getvalue())

    async def test_real_profile_start_only_successful_connect_claims_slot(self):
        from gateway import run as gateway_run
        cfg = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token='fixture')})
        adapter = SimpleNamespace()
        for succeeds in (False, True):
            self.runner._profile_adapters.clear()
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch('gateway.config.load_gateway_config', return_value=cfg))
                stack.enter_context(patch('gateway.run._load_gateway_runtime_config', return_value={}))
                stack.enter_context(patch('hermes_cli.plugins.discover_plugins'))
                stack.enter_context(patch.object(self.runner, '_snapshot_profile_busy_modes'))
                stack.enter_context(patch.object(self.runner, '_create_adapter', return_value=adapter))
                stack.enter_context(patch.object(self.runner, '_adapter_credential_claim', return_value=('discord', 'fixture')))
                stack.enter_context(patch.object(self.runner, '_adapter_listener_claim', return_value=None))
                stack.enter_context(patch.object(self.runner, '_configure_profile_adapter'))
                stack.enter_context(patch.object(self.runner, '_connect_initial_adapter_with_timeout', new=AsyncMock(return_value=succeeds)))
                disconnected = stack.enter_context(patch.object(self.runner, '_safe_adapter_disconnect', new=AsyncMock()))
                stack.enter_context(patch.object(self.runner, '_schedule_secondary_profile_startup_reconnect'))
                claimed = {}
                count = await self.runner._start_one_profile_adapters('worker', self.other, claimed)
            self.runner._publish_profile_coverage()
            self.assertEqual(count, int(succeeds))
            self.assertEqual(self.record()['connected_profiles'], ['worker'] if succeeds else [])
            self.assertEqual(bool(claimed), succeeds)
            self.assertEqual(disconnected.await_count, int(not succeeds))

    async def test_reconnect_publishes_only_while_runner_still_owns_slot(self):
        cfg = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token='fixture')})
        adapter = SimpleNamespace()
        for shutdown in (False, True):
            self.runner._running = True
            self.runner._profile_adapters = {}
            self.runner._profile_failed_platforms = {}
            self.runner._publish_profile_coverage(clear=True)
            async def connect(*args, **kwargs):
                if shutdown:
                    self.runner._running = False
                return True
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch('hermes_cli.profiles.get_profile_dir', return_value=self.other))
                stack.enter_context(patch('gateway.config.load_gateway_config', return_value=cfg))
                stack.enter_context(patch.object(self.runner, '_create_adapter', return_value=adapter))
                stack.enter_context(patch.object(self.runner, '_configure_profile_adapter'))
                stack.enter_context(patch.object(self.runner, '_connect_adapter_with_timeout', side_effect=connect))
                stack.enter_context(patch.object(self.runner, '_sync_voice_mode_state_to_adapter'))
                stack.enter_context(patch.object(self.runner, '_redeliver_failed_obligations_for_platform', new=AsyncMock()))
                disconnected = stack.enter_context(patch.object(self.runner, '_safe_adapter_disconnect', new=AsyncMock()))
                await self.runner._run_secondary_profile_reconnect('worker', Platform.DISCORD)
            self.assertEqual(self.record()['connected_profiles'], [] if shutdown else ['worker'])
            self.assertEqual(disconnected.await_count, int(shutdown))
            self.assertEqual(bool(self.runner._profile_adapters), not shutdown)
