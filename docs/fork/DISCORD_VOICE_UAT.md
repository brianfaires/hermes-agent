# Discord voice human UAT

All cases below are **unchecked**. Source candidate `4557bc60348250d464530d6cc63bdd19c59e48e8`; run only after separately authorized integration, exact-SHA CI and runtime activation. Start with a headset and the existing authorized channel/speaker/provider/voice configuration. No benchmark targets are claimed from source tests.

1. [ ] Restart/reconnect: owning profile joins configured voice channel; another profile does not receive or speak this session.
2. [ ] Manual leave and `/voice off`: leave stays suppressed as intended; off remains muted through departure/rejoin.
3. [ ] Basic conversation: one utterance produces one complete spoken answer; transcript echo follows existing opt-in policy.
4. [ ] Alias arguments: a configured spoken alias preserves case/punctuation in its tail and uses normal command authorization.
5. [ ] Long work: hear meaningful interim speech before long work and sparse truthful progress after sustained silence, without tool arguments or duplicate speech.
6. [ ] Barge-in: speak during playback; old audio stops before transcription finishes, and never resumes after delayed STT, transcript echo, queued PCM or reconnect.
7. [ ] Status: say “status please” while work runs; hear a short truthful status and verify work was not canceled.
8. [ ] Correction: say “correction: …”; old work receives normal stop and the preserved replacement becomes a fresh turn. Verify completed side effects are not represented as rolled back.
9. [ ] Explicit stop / input discard: “stop” stops the session; “cancel that” still discards that utterance under the prior behavior.
10. [ ] Streaming/error: hear complete user-facing clauses only; after an interruption or partial provider failure, never hear the full answer replayed from the beginning.
11. [ ] Denials: existing unauthorized speaker/channel/profile cases still cannot dispatch commands or receive replies. Do not add captured speakers to perform this check.
12. [ ] Measurements: record end-of-speech/first actual sound/onset-to-stop manually; compare headset and separately approved speakerphone echo/noise behavior before tuning or provider/model A-B.
