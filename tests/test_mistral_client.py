import json

import httpx
import pytest

from dataset_builder.config import require_secret
from dataset_builder.mistral_client import MistralClient, MistralError, message_text

SECRET = "sk-test-NEVER-WRITE-ME-123"


def _reply(text, model="mistral-small-2603"):
    return {
        "model": model,
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def test_message_text_string_and_chunks():
    assert message_text({"content": "hi"}) == "hi"
    chunks = [
        {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    assert message_text({"content": chunks}) == "ab"


def test_cache_retry_and_no_secret_on_disk(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_reply('["x"]'))

    sleeps = []
    client = MistralClient(
        tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        api_key=lambda: SECRET,
        sleep=sleeps.append,
    )
    kw = dict(
        model="mistral-small-2603",
        messages=[{"role": "user", "content": "q"}],
        temperature=0.9,
        max_tokens=50,
        reasoning_effort="none",
        random_seed=1,
    )
    c1 = client.complete(**kw)
    assert c1.text == '["x"]' and not c1.cached and calls["n"] == 2 and sleeps == [0.0]
    c2 = client.complete(**kw)
    assert c2.cached and c2.text == c1.text and calls["n"] == 2  # served from the cache
    assert client.usage.by_model["mistral-small-2603"]["cached_calls"] == 1
    files = list((tmp_path / "cache").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text("utf-8"))["request"]
    assert payload["reasoning_effort"] == "none" and payload["random_seed"] == 1
    assert SECRET not in files[0].read_text("utf-8")
    cost = client.usage.cost({"mistral-small": {"input": 0.15, "output": 0.6}})
    assert cost["mistral-small-2603"] == pytest.approx((200 * 0.15 + 40 * 0.6) / 1e6)


def test_cached_run_needs_no_key(tmp_path):
    def handler(request):
        return httpx.Response(200, json=_reply("ok"))

    kw = dict(model="m", messages=[], temperature=0.0, max_tokens=5)
    MistralClient(
        tmp_path, transport=httpx.MockTransport(handler), api_key=lambda: SECRET
    ).complete(**kw)

    def no_key():
        raise AssertionError("key requested although the call is cached")

    assert MistralClient(tmp_path, api_key=no_key).complete(**kw).cached


def test_http_error_raises(tmp_path):
    client = MistralClient(
        tmp_path,
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")),
        api_key=lambda: SECRET,
    )
    with pytest.raises(MistralError, match="401"):
        client.complete(model="m", messages=[], temperature=0.0, max_tokens=5)


def test_resolve_model(tmp_path):
    models = {
        "data": [
            {"id": "mistral-medium-2604", "aliases": ["mistral-medium-latest"], "deprecation": None}
        ]
    }
    client = MistralClient(
        tmp_path,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=models)),
        api_key=lambda: SECRET,
    )
    assert client.resolve_model("mistral-medium-latest")["id"] == "mistral-medium-2604"
    with pytest.raises(MistralError):
        client.resolve_model("nope")


def test_require_secret_env_and_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text('# comment\nMISTRAL_API_KEY="from-dotenv"\nHF_TOKEN=\n', "utf-8")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert require_secret("MISTRAL_API_KEY", dotenv) == "from-dotenv"
    monkeypatch.setenv("MISTRAL_API_KEY", "from-env")
    assert require_secret("MISTRAL_API_KEY", dotenv) == "from-env"  # env wins
    with pytest.raises(RuntimeError, match="HF_TOKEN is not set"):
        require_secret("HF_TOKEN", dotenv)
    import os

    assert os.environ.get("HF_TOKEN") is None  # .env values are never copied into os.environ


def test_retries_are_counted_and_logged(tmp_path, caplog):
    import logging

    answers = iter(
        [httpx.Response(429, headers={"retry-after": "0"})] * 4
        + [httpx.Response(503)]
        + [httpx.Response(200, json=_reply("ok"))]
    )
    client = MistralClient(
        tmp_path,
        transport=httpx.MockTransport(lambda r: next(answers)),
        api_key=lambda: SECRET,
        sleep=lambda s: None,
        max_retries=10,
    )
    with caplog.at_level(logging.WARNING, logger="dataset_builder.mistral_client"):
        client.complete(model="m", messages=[], temperature=0.0, max_tokens=5)
    assert client.retries == {"429": 4, "5xx": 1, "network": 0}
    warnings = [r.getMessage() for r in caplog.records]
    assert any("rate limited (429)" in w for w in warnings)
    assert len([w for w in warnings if "429)" in w]) == 3  # first 3 only, then throttled
