import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.display import build_tool_preview, prepare_tool_preview


class CompactDisplayTests(unittest.TestCase):
    def test_strict_setup_is_hidden_only_in_display(self):
        args = {'command': 'set -euo pipefail\nprintf hello'}
        self.assertEqual(build_tool_preview('terminal', args, max_len=0), '...printf hello')
        self.assertEqual(args['command'], 'set -euo pipefail\nprintf hello')
        from agent.display import shorten_tool_display_value
        self.assertEqual(shorten_tool_display_value('terminal', 'command', 'set -- -o pipefail; echo hi'), 'set -- -o pipefail; echo hi')

    def test_profile_and_home_paths_compact_before_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                args = {'path': str(home / 'profiles' / 'worker' / 'notes.txt')}
                self.assertEqual(build_tool_preview('read_file', args, max_len=0), 'notes.txt')
                from agent.display import shorten_tool_display_args
                self.assertEqual(shorten_tool_display_args('read_file', args)['path'], 'worker/notes.txt')
                self.assertEqual(prepare_tool_preview('read_file', args, fallback='', max_len=8).text, 'notes...')
                self.assertTrue(args['path'].startswith(tmp))

    def test_gateway_callback_selects_meaningful_shell_line(self):
        import queue
        from types import SimpleNamespace
        from gateway.run import TurnRunner
        from gateway.turn_context import TurnContext
        ctx = TurnContext(_run_still_current=lambda: True, progress_mode='all', tool_progress_enabled=True, progress_queue=queue.Queue())
        runner = SimpleNamespace(_adapter_for_source=lambda source: SimpleNamespace(supports_code_blocks=True))
        args = {'command': 'set -euo pipefail\nprintf hello'}
        TurnRunner(runner, ctx).progress_callback('tool.started', 'terminal', args['command'], args)
        result = ctx.progress_queue.get_nowait()
        self.assertIn('...printf hello', result)
        self.assertNotIn('set -euo', result)
        self.assertEqual(args['command'], 'set -euo pipefail\nprintf hello')
