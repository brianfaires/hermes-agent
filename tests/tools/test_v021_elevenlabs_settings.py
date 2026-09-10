"""Provider SDK is the only fake; both real generation entrypoints execute."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from tools import tts_tool
from tools.tts_streaming import ElevenLabsStreamer


class ElevenLabsSettingsTests(unittest.TestCase):
    def generate(self, config):
        client = Mock()
        client.text_to_speech.convert.side_effect = lambda **kw: iter([b'audio'])
        sdk = types.ModuleType('elevenlabs.types.voice_settings')
        sdk.VoiceSettings = lambda **kw: kw
        with patch.dict('sys.modules', {'elevenlabs.types.voice_settings': sdk}), patch.object(tts_tool, '_import_elevenlabs', return_value=lambda **kw: client), patch.object(tts_tool, '_resolve_provider_key', return_value='test-key'):
            with tempfile.TemporaryDirectory() as tmp:
                dest = str(Path(tmp) / 'speech.mp3')
                self.assertEqual(tts_tool._generate_elevenlabs('text', dest, config), dest)
                self.assertEqual(Path(dest).read_bytes(), b'audio')
            self.assertEqual(list(ElevenLabsStreamer(config, config.get('elevenlabs', {})).stream('text')), [b'audio'])
        return [call.kwargs for call in client.text_to_speech.convert.call_args_list]

    def test_both_paths_forward_bounded_existing_settings(self):
        calls = self.generate({'speed': 1.1, 'elevenlabs': {'speed': 2, 'stability': -1, 'similarity_boost': 2, 'style': .3, 'use_speaker_boost': 'false'}})
        expected = {'speed': 1.2, 'stability': 0.0, 'similarity_boost': 1.0, 'style': .3, 'use_speaker_boost': False}
        for call in calls:
            self.assertEqual(call.get('voice_settings'), expected)
        self.assertEqual(calls[0]['output_format'], 'mp3_44100_128')
        self.assertEqual(calls[1]['output_format'], 'pcm_24000')

    def test_global_speed_fallback_and_defaults(self):
        for call in self.generate({'speed': .1}):
            self.assertEqual(call.get('voice_settings'), {'speed': .7})
        for call in self.generate({}):
            self.assertNotIn('voice_settings', call)

    def test_text_remains_visible_when_streaming_provider_fails(self):
        import queue
        import threading
        from tools import tts_streaming

        text_queue = queue.Queue()
        text_queue.put("This text remains visible despite synthesis failure.")
        text_queue.put(None)
        shown = []
        streamer = Mock(sample_rate=24000, channels=1)
        def fail_stream(text):
            self.assertTrue(shown)  # display occurs before the provider is called
            raise ImportError("provider SDK unavailable")
            yield b""
        streamer.stream.side_effect = fail_stream
        done = threading.Event()
        with patch.dict('sys.modules', {'numpy': types.ModuleType('numpy')}), patch.object(tts_tool, '_load_tts_config', return_value={}), patch.object(tts_streaming, 'resolve_streaming_provider', return_value=streamer), patch.object(tts_tool, '_import_sounddevice', return_value=Mock()):
            tts_tool.stream_tts_to_speaker(text_queue, threading.Event(), done, shown.append)
        self.assertIn("This text remains visible", ''.join(shown))
        self.assertTrue(done.is_set())
