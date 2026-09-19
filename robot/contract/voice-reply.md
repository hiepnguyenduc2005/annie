# Synthetic voice reply policy (incident-correlated)

Two-stage capture protocol for simulated speech replies to a fall check-in.
The app mounts the authenticated router in `robot/app_backend/app/main.py`:

    from robot.app_backend.app.audio_reply import build_audio_reply_router

    app.include_router(build_audio_reply_router(lambda: app.state.service, authorize))

The actual STT model behind robot/simulation/local_stt.py is faster-whisper
tiny.en; requests must declare exactly faster-whisper-tiny.en.

## POST /voice/capture

Stage one, registered while the check-in window is open:

    {
      "utterance_id": "5488e7cb-8d54-4c59-8e02-9b739d694a81",
      "event_id": "required-UUID-of-active-checkin",
      "source": "synthetic_replay",
      "capture_started_at": 1730000000000
    }

Rules: only accepted while service.pending is in phase awaiting_reply with a
non-null deadline_at; event_id must match the pending check-in;
capture_started_at must be at or after the question finished speaking
(audio_started_at), not in the future, and the registration itself must
happen by the deadline (a later registration claiming an older start is
still rejected). One capture per event; the in-memory registry holds at
most 100 events (oldest evicted). Registration declares an anticipated end
at capture_started_at + 20000 so the owner tick can consult
pending_grace(service).

Rejections return 422 with the reason string, e.g. no_active_checkin,
wrong_incident, phase_not_awaiting_reply, question_not_finished,
future_timestamp, registered_after_cutoff, capture_already_registered.
All request timestamps are strict bounded integers; segment numbers must be
finite (NaN, Infinity, and null are rejected at the API boundary with 422).
Segment times stay raw Whisper values: the absolute bound is 0..20 s, and
known tiny.en quantization (a segment end overshooting a short recorded
WAV duration) is accepted without clamping.

## POST /voice/result

Stage two, after transcription:

    {
      "utterance_id": "same-as-capture",
      "event_id": "same-as-capture",
      "capture_started_at": 1730000000000,
      "capture_ended_at": 1730000005000,
      "text": "I am okay",
      "source": "synthetic_replay",
      "model": "faster-whisper-tiny.en",
      "segments": [
        {"start": 0.0, "end": 1.2, "text": "I am okay",
         "avg_logprob": -0.35, "no_speech_prob": 0.08}
      ]
    }

All numbers must be finite; segment bounds require end >= start and the
absolute 0..20 s range; capture_ended_at must be >= capture_started_at,
not in the future, and <= deadline_at (the strict counting rule).
Completion must arrive within deadline_at + 2000 ms wall time; later
results are rejected. Unknown or identity-mismatched captures return 404;
policy rejections return 200 with an ineligible decision.

## Whisper quality (raw signals, never calibrated confidence)

Fixed rule, per segment: avg_logprob >= -1.0 and no_speech_prob <= 0.75.
Additionally the segments must be non-empty and their concatenated text
must normalize identically to the top-level text. Empty text or silence
fails the quality gate. No confidence value is ever fabricated.

## Intent (exact normalized match, no fuzzy matching)

Normalization lowercases, drops apostrophes, and collapses punctuation and
whitespace. Concern: help, help me, i need help, not okay, im not okay,
i am not okay, i cant get up, okay help me, okay help. Reassurance: okay,
ok, im okay, i am okay, im ok, i am ok. Anything else, including the echo
"okay or help" and unrelated speech, is ambiguous. One event yields at
most one effect.

## Decision response

    {
      "utterance_id": "...", "event_id": "...",
      "intent": "reassurance | concern | ambiguous | null",
      "eligible": false, "reason": "low_quality", "applied": false
    }

The applied field reflects the actual Service.apply_voice_reply outcome
(true only when the atomic apply returned true); a rejected apply (window
closed) is stored as the final decision and can never double-fire. A
duplicate submission returns the stored decision verbatim (duplicate:
true) without executing the apply path again.

## Timeout reasons (typed, no fabricated silence claims)

Events carry an optional reason enum: playback_failed, playback_timeout,
input_unavailable, recognition_timeout. Legacy demo mode (no receipt
requirement) keeps the plain checkin_no_reply with no reason. In receipt
mode: playback watchdog expiry emits checkin_audio_failed /
playback_timeout; deadline expiry with no registered capture emits
checkin_audio_failed / input_unavailable (audio never existed, so claiming
resident silence would be false); a registered in-flight capture past the
grace emits checkin_audio_failed / recognition_timeout (recognition failed; an
ambiguous transcript is a decision-level ambiguous_intent returned to the
caller and is never evidence about the resident). A completed but rejected
transcript produces plain checkin_no_reply at the deadline. A validated capture that
completed by the deadline applies even if it arrives up to 2000 ms late in
wall time.

## Deadline-grace integration (owner tick)

pending_grace(service) returns deadline_at + 2000 while a registered
capture for the active check-in is still in flight (registered but no
result submitted yet), and None otherwise. The tick consults this to wait
out the grace window before escalating. The strict counting rule is
unchanged: only already-registered captures whose result shows
capture_ended_at <= deadline_at can ever count as a valid reply.
