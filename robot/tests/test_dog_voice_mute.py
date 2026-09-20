"""Speech muting never invokes live audio or paid providers."""
import threading
from unittest.mock import Mock

from robot.dog.voice.cloud import CloudVoice
from robot.dog.voice.devices import AudioDevices


def test_muted_skips_provider_and_initializes_output_gate():
    tts, player, local, handler = Mock(), Mock(), Mock(), Mock()
    voice = CloudVoice(eleven_key='test', muted=True, tts=tts, player=player,
                       local_speak=local, mute_handler=handler)
    handler.assert_called_once_with(True)
    assert voice.status()['muted'] is True
    assert voice.speak('hello') is False
    tts.assert_not_called()
    player.assert_not_called()
    local.assert_not_called()
    voice.configure(muted=False)
    handler.assert_called_with(False)
    tts.return_value = b'audio'
    player.return_value = True
    assert voice.speak('fresh') is True


def test_mute_unmute_during_tts_discards_old_speech():
    started, release = threading.Event(), threading.Event()
    def tts(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return b'audio'
    player, local = Mock(), Mock()
    voice = CloudVoice(eleven_key='test', muted=False, tts=tts, player=player, local_speak=local)
    result = []
    worker = threading.Thread(target=lambda: result.append(voice.speak('old')))
    worker.start()
    assert started.wait(2)
    voice.configure(muted=True)
    voice.configure(muted=False)
    release.set()
    worker.join(2)
    assert result == [False]
    player.assert_not_called()
    local.assert_not_called()


def test_player_cancellation_does_not_fall_back():
    local = Mock()
    voice = CloudVoice(eleven_key='test', muted=False, tts=lambda *a, **k: b'audio', local_speak=local)
    def player(data):
        voice.configure(muted=True)
        return False
    voice.player = player
    assert voice.speak('hello') is False
    local.assert_not_called()


def test_owned_player_is_terminated(monkeypatch):
    started = threading.Event()
    class Process:
        returncode = None
        def __init__(self, *args, **kwargs):
            self.terminated = False
            started.set()
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
            self.returncode = -15
        def wait(self, timeout):
            return self.returncode
    processes = []
    def spawn(*args, **kwargs):
        p = Process(*args, **kwargs)
        processes.append(p)
        return p
    monkeypatch.setattr('robot.dog.voice.devices.subprocess.Popen', spawn)
    monkeypatch.setattr('robot.dog.voice.devices.resolve', lambda *a, **k: None)
    devices = AudioDevices()
    result = []
    worker = threading.Thread(target=lambda: result.append(devices.play(b'audio')))
    worker.start()
    assert started.wait(2)
    devices.set_muted(True)
    worker.join(2)
    assert result == [False]
    assert processes[0].terminated
    assert devices.play(b'new') is False
    assert len(processes) == 1


def test_cancelled_local_render_cannot_play_after_unmute(monkeypatch):
    monkeypatch.setattr('robot.dog.voice.devices.resolve', lambda *a, **k: None)
    spawn = Mock()
    monkeypatch.setattr('robot.dog.voice.devices.subprocess.Popen', spawn)
    devices = AudioDevices()
    def local(text, *, cancelled):
        voice.configure(muted=True)
        voice.configure(muted=False)
        return devices.play_cancellable(b'old', cancelled=cancelled)
    voice = CloudVoice(eleven_key='', muted=False, mute_handler=devices.set_muted,
                       local_speak_cancellable=local)
    assert voice.speak('hello') is False
    spawn.assert_not_called()
