"""Cron delivery attention markers and platform-scoped thread diagnostics."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cron import scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig


class CronDeliveryAttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = GatewayConfig()
        self.config.platforms[Platform.TELEGRAM] = PlatformConfig(enabled=True)
        self.config.platforms[Platform.DISCORD] = PlatformConfig(enabled=True)
        self.job = {'id': 'attention-test', 'name': 'Daily report', 'deliver': 'telegram:100'}

    def deliver(self, content, *, wrap=True, job=None):
        sender = AsyncMock(return_value={'success': True})
        with patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': wrap}}), patch('gateway.config.load_gateway_config', return_value=self.config), patch('tools.send_message_tool._send_to_platform', sender):
            error = scheduler._deliver_result(job or self.job, content)
        self.assertIsNone(error)
        self.assertEqual(sender.await_count, 1)
        return sender.await_args

    def test_attention_class_survives_wrapping_and_plain_delivery(self):
        for wrap in [True, False]:
            for content, prefix in [('Warning: disk nearly full', '⚠️'), ('status: warn', '⚠️'), ('❌ Failed to collect report', '❌'), ('All systems healthy', '')]:
                with self.subTest(wrap=wrap, content=content):
                    sent = self.deliver(content, wrap=wrap)
                    self.assertEqual(sent.args[2], '100')
                    text = sent.args[3]
                    if prefix:
                        self.assertTrue(text.startswith(prefix), text)
                    else:
                        self.assertTrue(text.startswith('Cronjob Response:' if wrap else content), text)
                        self.assertFalse(text.startswith(('⚠️', '❌')))
                    self.assertIn(content, text)

    def test_actual_failures_are_red_without_changing_classification(self):
        for error, expected in [('429 rate limit', 'provider rate limit'), ('Script timed out after 10s: task.py', 'script timed out'), ('ValueError: bad input', 'bad input'), ('401 unauthorized', 'provider authentication')]:
            with self.subTest(error=error):
                content = scheduler._summarize_cron_failure_for_delivery(self.job, error)
                sent = self.deliver(content).args[3]
                self.assertTrue(sent.startswith('❌'), sent)
                self.assertIn(expected, sent)
        content = scheduler._summarize_cron_failure_for_delivery(self.job, 'skipped to prevent unintended spend: global inference config drifted')
        self.assertTrue(content.startswith('⚠️'))

    def test_thread_warning_only_when_same_platform_and_chat_loses_thread(self):
        for platform, chat, should_warn in [('discord', '100', False), ('telegram', '200', False), ('telegram', '100', True)]:
            with self.subTest(platform=platform, chat=chat):
                job = dict(self.job, origin={'platform': platform, 'chat_id': chat, 'thread_id': '42'})
                with self.assertLogs(scheduler.logger, level='INFO') as logs:
                    sent = self.deliver('Healthy', job=job)
                lost = [line for line in logs.output if 'delivery target lost it' in line]
                self.assertEqual(bool(lost), should_warn, logs.output)
                self.assertEqual(sent.args[2], '100')
                self.assertIsNone(sent.kwargs['thread_id'])

    def test_fire_prompt_teaches_attention_without_changing_silence(self):
        built = scheduler._build_job_prompt(dict(self.job, prompt='Report status'))
        self.assertIn('ATTENTION PREFIXES:', built)
        self.assertIn('[SILENT]', built)


if __name__ == '__main__':
    unittest.main()
