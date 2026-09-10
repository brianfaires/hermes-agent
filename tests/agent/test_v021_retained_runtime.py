"""Executable qualification of retained migration features; no live services."""
import asyncio
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class RetainedRuntimeTests(unittest.TestCase):
    def test_browser_skips_occupied_successor_with_unavailable_ipv6(self):
        from hermes_cli.browser_connect import find_free_debug_port
        real_socket = socket.socket
        def ipv4_only(family, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError('fixture: family unavailable')
            return real_socket(family, *args, **kwargs)
        with real_socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with patch.object(socket, 'socket', side_effect=ipv4_only):
                selected = find_free_debug_port(preferred=port - 1)
            self.assertGreater(selected, port)
            with real_socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(('127.0.0.1', selected))

    def test_browser_fallback_when_neither_family_is_available(self):
        from hermes_cli.browser_connect import find_free_debug_port
        with patch.object(socket, 'socket', side_effect=OSError('fixture: no families')):
            self.assertEqual(find_free_debug_port(preferred=9200), 9201)

    def test_external_memory_schemas_independent_of_file_memory(self):
        from agent.memory_manager import MemoryManager, inject_memory_provider_tools, memory_provider_tools_exposed
        manager = MemoryManager()
        schema = {'name': 'hindsight_retain', 'description': 'fixture', 'parameters': {'type': 'object', 'properties': {}}}
        manager.add_provider(SimpleNamespace(name='hindsight', get_tool_schemas=lambda: [schema]))
        for disabled, expected in [(['memory'], True), (['memory', 'hindsight'], False)]:
            with self.subTest(disabled=disabled):
                agent = SimpleNamespace(_memory_manager=manager, tools=[], valid_tool_names=set(),
                                        enabled_toolsets=['hindsight'], disabled_toolsets=disabled)
                self.assertEqual(memory_provider_tools_exposed(agent), expected)
                self.assertEqual(inject_memory_provider_tools(agent), int(expected))
                self.assertEqual('hindsight_retain' in agent.valid_tool_names, expected)
                self.assertEqual(inject_memory_provider_tools(agent), 0, 'must not duplicate schemas')

    def test_baseline_worker_guidance_init_fallback_and_cache(self):
        from agent.system_prompt import build_system_prompt_parts
        from run_agent import AIAgent
        for task in ('', 'fixture-task'):
            for tool_names in ((), ('kanban_show',)):
                expected = bool(task and tool_names)
                with self.subTest(task=task, tools=tool_names), \
                     tempfile.TemporaryDirectory() as root, \
                     patch.dict(os.environ, {'HERMES_HOME': root, 'HERMES_KANBAN_TASK': task}), \
                     patch('run_agent.get_tool_definitions', return_value=[
                         {'type': 'function', 'function': {'name': name, 'description': 'fixture',
                          'parameters': {'type': 'object', 'properties': {}}}} for name in tool_names]), \
                     patch('run_agent.check_toolset_requirements', return_value={}), \
                     patch('run_agent.OpenAI'), \
                     patch('run_agent.load_soul_md', return_value=''), \
                     patch('run_agent.build_environment_hints', return_value=''), \
                     patch('run_agent.build_context_files_prompt', return_value=''):
                    agent = AIAgent(api_key='test-key', base_url='https://example.invalid/v1',
                                    quiet_mode=True, skip_context_files=True, skip_memory=True)
                    self.assertEqual(bool(agent._kanban_worker_guidance), expected)
                    before = build_system_prompt_parts(agent)['stable']
                    with patch.dict(os.environ, {'HERMES_KANBAN_TASK': ''}):
                        self.assertEqual(build_system_prompt_parts(agent)['stable'], before)
                    agent._kanban_worker_guidance = None
                    fallback = build_system_prompt_parts(agent)['stable']
                    self.assertEqual('Kanban task execution protocol' in fallback, expected)

    def test_langfuse_post_tool_neutralizes_multiline_paths(self):
        from plugins.observability import langfuse as plugin
        for value in ['/tmp/output.txt\n' + 'x' * 300, '~/output.txt\r\nlog', 'C:\\Temp\\output.txt\nlog']:
            with self.subTest(value=value[:20]):
                observation = Mock()
                state = plugin.TraceState(trace_id='fixture', root_ctx=None, root_span=None,
                                          tools={'call': observation})
                with patch.dict(plugin._TRACE_STATE, {'fixture': state}, clear=True), \
                     patch.dict(os.environ, {'HERMES_LANGFUSE_CAPTURE': 'sanitized', 'HERMES_LANGFUSE_MAX_CHARS': '80'}):
                    plugin.on_post_tool_call(task_id='fixture', tool_call_id='call', tool_name='terminal', result=value)
                result = observation.update.call_args.kwargs['output']
                self.assertEqual(result['type'], 'path_like_text')
                self.assertTrue(result['content'].startswith('local-path-like text: '))
                self.assertEqual(result['length'], len(value))
                self.assertLess(len(result['content']), 120)
                observation.end.assert_called_once()

    def test_discord_denial_precedes_text_media_and_edit_delivery(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}):
            media = Path(root) / 'image.png'
            media.write_bytes(b'fixture')
            msg = SimpleNamespace(id=77, attachments=[], edit=AsyncMock())
            channel = SimpleNamespace(id=123, name='denied', parent_id=None, send=AsyncMock(), get_partial_message=lambda _: msg)
            adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', extra={'allowed_channels': ['456']}))
            adapter._client = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock(return_value=channel))
            cases = {
                'send': ('123', 'text'), 'edit_message': ('123', '77', 'text'),
                'send_image_file': ('123', str(media)), 'send_document': ('123', str(media)),
                'send_video': ('123', str(media)), 'send_voice': ('123', str(media)),
                'send_image': ('123', 'https://invalid.example/image.png'),
                'send_animation': ('123', 'https://invalid.example/image.gif'),
            }
            for method, args in cases.items():
                with self.subTest(method=method):
                    with patch('plugins.platforms.discord.adapter.is_safe_url', return_value=True), \
                         patch('aiohttp.ClientSession', side_effect=AssertionError('unexpected network')):
                        result = asyncio.run(getattr(adapter, method)(*args))
                    self.assertFalse(result.success)
                    self.assertIn('DISCORD_ALLOWED_CHANNELS', result.error)
            channel.send.assert_not_awaited()
            msg.edit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
