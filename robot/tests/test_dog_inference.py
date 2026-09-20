"""Disabled inference is an explicit offline mode, independent of inherited credentials."""
import pytest

from robot.dog.inference import Inference


@pytest.mark.parametrize("provider", ["off", "none"])
def test_disabled_provider_never_sends_text_or_images(provider):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled inference attempted network IO")

    client = Inference(provider, base_url="https://example.invalid", api_key="synthetic",
                       allow_cloud_vision=True, post=forbidden)
    for images in (None, [b"synthetic jpeg"]):
        result = client.chat([{"role": "user", "content": "hello"}], images=images)
        assert not result["ok"] and result["error"] == "inference disabled"
    assert client.stats["calls"] == 0


def test_disabled_provider_from_environment(monkeypatch):
    monkeypatch.setenv("ANNIE_LLM_PROVIDER", "off")
    assert Inference().chat([])["provider"] == "off"
