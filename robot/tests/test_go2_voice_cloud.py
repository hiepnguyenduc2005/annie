"""Cloud voice adapters with fakes: cloud first, local fallback, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from go2_voice_cloud import CloudVoice, deepgram_transcribe, elevenlabs_tts  # noqa: E402


def test_elevenlabs_and_deepgram_parse_and_fail_closed():
    assert elevenlabs_tts("hi", api_key="k", post=lambda url, body, headers, t: b"MP3") == b"MP3"
    assert elevenlabs_tts("hi", api_key="k", post=lambda *a: (_ for _ in ()).throw(RuntimeError("down"))) is None
    ok = {"results": {"channels": [{"alternatives": [{"transcript": "Okay, I will plug it in."}]}]}}
    assert deepgram_transcribe(b"RIFF", api_key="k", post=lambda *a: ok) == "Okay, I will plug it in."
    assert deepgram_transcribe(b"RIFF", api_key="k", post=lambda *a: {"results": {}}) is None


def test_cloud_voice_uses_cloud_when_keys_exist_and_falls_back_otherwise():
    spoken, played = [], []
    v = CloudVoice(eleven_key="k", deepgram_key="d", local_speak=lambda t: spoken.append(t) or True,
                   local_transcribe=lambda w: "local words", player=lambda a: played.append(a) or True,
                   tts=lambda text, api_key, voice_id: b"AUDIO", stt=lambda wav, api_key: "cloud words")
    assert v.speak("Hi Jeanine") and played == [b"AUDIO"] and spoken == []
    assert v.transcribe(b"RIFF") == "cloud words"
    assert v.stats["tts_cloud"] == 1 and v.stats["stt_cloud"] == 1
    # cloud failure -> local path, still succeeds
    v.tts, v.stt = (lambda *a, **k: None), (lambda *a, **k: None)
    assert v.speak("Hi again") and spoken == ["Hi again"]
    assert v.transcribe(b"RIFF") == "local words"
    assert v.stats["tts_local"] == 1 and v.stats["stt_local"] == 1
    # no keys at all -> local only, never touches the cloud callables
    w = CloudVoice(eleven_key="", deepgram_key="", local_speak=lambda t: True, local_transcribe=lambda w: "x",
                   tts=lambda *a, **k: (_ for _ in ()).throw(AssertionError("cloud called")),
                   stt=lambda *a, **k: (_ for _ in ()).throw(AssertionError("cloud called")))
    assert w.enabled == {"elevenlabs": False, "deepgram": False}
    assert w.speak("hi") and w.transcribe(b"RIFF") == "x"
