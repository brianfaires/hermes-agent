from plugins.platforms.discord.voice_timing import VoiceTiming


def test_timestamp_ring_is_bounded_and_has_no_payload_slot():
    timing = VoiceTiming()
    for i in range(200):
        timing.mark("stt_ready", i)
    samples = timing.snapshot()
    assert len(samples) == 128
    assert samples[0] == ("stt_ready", 72.0)
    assert all(stage == "stt_ready" and isinstance(at, float) for stage, at in samples)
