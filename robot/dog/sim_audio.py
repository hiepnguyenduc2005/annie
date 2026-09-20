"""Explicit simulation audio: records text without opening devices or providers."""
class MockAudio:
    source = "simulation"

    def __init__(self, transcript="I'm doing well, thank you."):
        self.transcript = transcript
        self.spoken = []

    def speak(self, text):
        self.spoken.append(str(text))
        return True

    def listen(self, max_s):
        return {"transcript": self.transcript or None, "heard": bool(self.transcript),
                "speech_ms": 0, "source": "simulation", "mocked": True}
