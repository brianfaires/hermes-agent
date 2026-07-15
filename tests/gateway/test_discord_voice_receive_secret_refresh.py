from __future__ import annotations

import struct
import types

from plugins.platforms.discord.adapter import VoiceReceiver


def _rtp_packet(ssrc: int = 1234) -> bytes:
    header = struct.pack(">BBHII", 0x80, 0x78, 1, 960, ssrc)
    return header + b"encrypted" + b"nonce"


def test_voice_receiver_refreshes_secret_key_and_retries_decrypt(monkeypatch):
    """A stale cached Discord voice key should not kill the whole VC join."""

    conn = types.SimpleNamespace(
        secret_key=b"new-secret",
        dave_session=None,
        add_socket_listener=lambda callback: None,
        remove_socket_listener=lambda callback: None,
    )
    vc = types.SimpleNamespace(_connection=conn)
    receiver = VoiceReceiver(vc)
    receiver._running = True
    receiver._secret_key = b"old-secret"
    receiver._bot_ssrc = 9999

    calls: list[bytes] = []

    def fake_decrypt_candidates(**kwargs):
        calls.append(receiver._secret_key)
        if receiver._secret_key == b"old-secret":
            raise ValueError("stale key")
        return b"opus", 0, "fixed"

    class FakeDecoder:
        def decode(self, payload):
            assert payload == b"opus"
            return b"pcm"

    monkeypatch.setattr(receiver, "_decrypt_rtp_payload_candidates", fake_decrypt_candidates)
    monkeypatch.setattr("plugins.platforms.discord.adapter.discord.opus.Decoder", FakeDecoder)

    receiver._on_packet(_rtp_packet())

    assert calls == [b"old-secret", b"new-secret"]
    assert receiver._secret_key == b"new-secret"
    stats = receiver._rtp_stats[1234]
    assert stats["decrypted"] == 1
    assert stats["decrypt_failed"] == 0
    assert receiver._buffers[1234] == bytearray(b"pcm")


def test_voice_receiver_does_not_retry_when_secret_key_unchanged(monkeypatch):
    conn = types.SimpleNamespace(secret_key=b"same-secret", dave_session=None)
    vc = types.SimpleNamespace(_connection=conn)
    receiver = VoiceReceiver(vc)
    receiver._running = True
    receiver._secret_key = b"same-secret"
    receiver._bot_ssrc = 9999

    def fake_decrypt_candidates(**kwargs):
        raise ValueError("bad packet")

    monkeypatch.setattr(receiver, "_decrypt_rtp_payload_candidates", fake_decrypt_candidates)

    receiver._on_packet(_rtp_packet())

    stats = receiver._rtp_stats[1234]
    assert stats["decrypted"] == 0
    assert stats["decrypt_failed"] == 1
