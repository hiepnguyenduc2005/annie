"""Voice commands for the live patrol: wake word + short phrase -> one bounded intent.

`parse_command` is pure: it needs the wake word ("annie", "hey annie", "hey dog", "dog")
at the start of the transcript, then matches a small phrase table. Anything else is
ignored, so background chatter in a venue cannot steer the robot. The listener thread
uses the host microphone (the operator's AirPods when they are the default input),
Silero VAD and local Whisper from `robot/simulation/live_listener.py`; nothing leaves
the machine. A recognised command is a request to the control loop, which still applies
every guardrail; a transcript is recognised text, not understanding.
"""
from __future__ import annotations

import contextlib
import re
import threading
import time

WAKE_WORDS = ("hey annie", "annie", "hey dog", "hey doggy", "dog", "doggy", "any", "annie's")
COMMANDS = [
    ("stop", ("stop", "stay", "halt", "freeze", "wait")),
    ("sit", ("sit", "sit down")),
    ("stand", ("stand", "stand up", "get up")),
    ("dance", ("dance", "dance for me", "boogie")),
    ("hello", ("say hi", "say hello", "wave", "hello", "hi")),
    ("heart", ("heart", "show love", "make a heart")),
    ("stretch", ("stretch",)),
    ("follow", ("follow me", "follow", "come with me", "heel")),
    ("approach", ("come here", "come", "come to me", "over here")),
    ("go_home", ("go home", "home", "go back")),
    ("explore", ("explore", "patrol", "go patrol", "go explore", "walk around", "go for a walk")),
    ("scan", ("look around", "scan", "turn around", "spin")),
]


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)
    return " ".join(text.split())


def parse_command(transcript: str | None, *, require_wake=True) -> dict | None:
    """Return {"intent", "phrase", "wake"} or None when the transcript is not a command for the dog."""
    if not transcript:
        return None
    text = _normalize(transcript)
    wake = None
    for w in sorted(WAKE_WORDS, key=len, reverse=True):
        if text == w or text.startswith(w + " "):
            wake, text = w, text[len(w):].strip()
            break
    if require_wake and wake is None:
        return None
    raw = text
    text = re.sub(r"^(please|can you|could you|now|go|just)\s+", "", text).strip()
    for intent, phrases in COMMANDS:
        for phrase in sorted(phrases, key=len, reverse=True):
            if text == phrase or text.startswith(phrase + " ") or text.endswith(" " + phrase) or f" {phrase} " in f" {text} ":
                return {"intent": intent, "phrase": phrase, "wake": wake}
    if (wake is not None or not require_wake) and len(raw.split()) >= 2:  # addressed to the dog (or a conversation is open): free text
        return {"intent": "instruct", "phrase": raw[:300], "wake": wake}
    return None


class CommandListener:
    """Background thread: mic -> VAD -> Whisper -> parse_command -> callback(intent dict, transcript)."""

    def __init__(self, on_command, *, status=print, max_ms=4000, require_wake=True, recorder=None, vad=None,
                 transcriber=None):
        self.on_command, self.status, self.max_ms, self.require_wake = on_command, status, max_ms, require_wake
        self._recorder, self._vad, self._transcriber = recorder, vad, transcriber
        self.stopped = False
        self.heard = 0
        self.commands = 0
        self._reopen = None  # set by reopen(): a factory for a new recorder, picked up between utterances
        self.open_until = 0.0   # conversation window: until this time speech needs no wake word (Annie just spoke to someone)
        self.muted_until = 0.0  # while Annie herself is talking, what the mic hears is discarded
        self.thread = threading.Thread(target=self._loop, daemon=True, name="voice")

    def start(self):
        self.thread.start()
        return self

    def open_conversation(self, seconds: float = 45.0):
        """After Annie speaks to someone, their next words need no wake word for `seconds`."""
        self.open_until = max(self.open_until, time.time() + seconds)

    def mute(self, seconds: float):
        self.muted_until = max(self.muted_until, time.time() + seconds)

    def reopen(self, recorder_factory):
        """Switch microphones: `recorder_factory()` returns the new chunk generator; applied between utterances."""
        self._reopen = recorder_factory

    def stop(self):
        self.stopped = True

    def _loop(self):
        try:
            from robot.simulation.live_listener import capture_utterance, pcm16_to_wav, silero_vad, sounddevice_recorder
            from robot.simulation.local_stt import LocalSTTAdapter
            recorder = self._recorder or sounddevice_recorder()
            vad = self._vad or silero_vad()
            if self._transcriber is None:
                adapter = LocalSTTAdapter()
                adapter.warm()
                transcriber = lambda wav: (adapter.transcribe(wav).text or "")  # noqa: E731
            else:
                transcriber = self._transcriber
        except Exception as exc:
            self.status(f"voice listener unavailable: {type(exc).__name__}")
            return
        self.status("voice listener ready (say 'Annie, ...')")
        while not self.stopped:
            try:
                if self._reopen is not None:  # the operator picked another microphone
                    factory, self._reopen = self._reopen, None
                    with contextlib.suppress(Exception):
                        recorder.close()
                    recorder = factory()
                    self.status("voice listener: microphone switched")
                utt = capture_utterance(recorder, vad, max_ms=self.max_ms)
                if utt["outcome"] != "speech":
                    continue
                text = transcriber(pcm16_to_wav(utt["pcm"])).strip()
            except Exception as exc:
                self.status(f"voice listener error: {type(exc).__name__}")
                time.sleep(0.5)
                continue
            if not text:
                continue
            if time.time() < self.muted_until:
                continue  # Annie's own voice
            self.heard += 1
            in_conversation = time.time() < self.open_until
            cmd = parse_command(text, require_wake=self.require_wake and not in_conversation)
            if cmd is not None and in_conversation and cmd.get("wake") is None and cmd["intent"] == "instruct":
                cmd = {"intent": "converse", "phrase": text[:300], "wake": None}  # talking to Annie, not commanding her
            if cmd:
                self.commands += 1
                self.status(f"voice command: {cmd['intent']} ({text!r})")
                try:
                    self.on_command(cmd, text)
                except Exception as exc:
                    self.status(f"voice command handler error: {type(exc).__name__}")
            else:
                self.status(f"heard (ignored): {text[:60]!r}")
