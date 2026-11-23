"""Minimal Mistral chat-completions client with retries, a disk cache and usage tracking.

- The API key comes from `config.require_secret("MISTRAL_API_KEY")` (environment or the
  git-ignored `.env`). It is only sent in the Authorization header: never logged, never cached.
- Every request/response pair is cached on disk, keyed by a hash of the request. Reruns and
  resumed runs make no new calls and give identical results (API output itself is not
  deterministic; the cache is what makes reruns reproducible).
"""

import hashlib
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from dataset_builder.config import require_secret

logger = logging.getLogger(__name__)
# One INFO line per HTTP request drowns the progress lines; keep only warnings from httpx.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_URL = "https://api.mistral.ai/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}


class MistralError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    model: str  # model id reported by the API
    prompt_tokens: int
    completion_tokens: int
    cached: bool


@dataclass
class UsageTracker:
    """Thread-safe token counts per requested model."""

    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, model: str, c: Completion) -> None:
        with self._lock:
            m = self.by_model.setdefault(
                model, {"calls": 0, "cached_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            m["calls"] += 1
            m["cached_calls"] += int(c.cached)
            m["prompt_tokens"] += c.prompt_tokens
            m["completion_tokens"] += c.completion_tokens

    def cost(self, prices: dict[str, dict[str, float]]) -> dict[str, float]:
        """$ per model, from prices keyed by model-name prefix ($ per 1M tokens)."""
        out = {}
        for model, m in self.by_model.items():
            p = next((v for k, v in prices.items() if model.startswith(k)), None)
            if p is None:
                raise MistralError(f"no price configured for model {model!r}")
            out[model] = (
                m["prompt_tokens"] * p["input"] + m["completion_tokens"] * p["output"]
            ) / 1e6
        return out


def message_text(message: dict[str, Any]) -> str:
    """`content` is either a string or a list of chunks (e.g. thinking + text): keep the text."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict) and chunk.get("type") == "text":
                parts.append(chunk.get("text", ""))
        return "".join(parts)
    return ""


class MistralClient:
    def __init__(
        self,
        cache_dir: Path,
        max_retries: int = 6,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        api_key: Callable[[], str] = lambda: require_secret("MISTRAL_API_KEY"),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_retries = max_retries
        self.timeout = timeout
        self.usage = UsageTracker()
        self._transport = transport
        self._api_key = api_key
        self._sleep = sleep
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()
        # Retries, for the log and the report: "429" (rate limit), "5xx", "network".
        self.retries: dict[str, int] = {"429": 0, "5xx": 0, "network": 0}
        self._last_retry_log = 0.0

    def _note_retry(self, kind: str, wait: float, detail: str) -> None:
        """Count a retry and log it: the first 3 one by one, then a summary every 30 s."""
        with self._lock:
            self.retries[kind] += 1
            n, now = self.retries[kind], time.monotonic()
            if n <= 3 or now - self._last_retry_log >= 30:
                self._last_retry_log = now
                what = "rate limited (429)" if kind == "429" else f"{kind} error ({detail})"
                logger.warning(
                    "%s, waiting %.1f s and retrying. Retries so far: 429=%d, 5xx=%d, "
                    "network=%d. Many 429s mean p3.concurrency is above your Mistral limit.",
                    what,
                    wait,
                    self.retries["429"],
                    self.retries["5xx"],
                    self.retries["network"],
                )

    # The HTTP client (and the key) is only created when a call is not in the cache.
    def _client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(
                    base_url=API_URL,
                    headers={"Authorization": f"Bearer {self._api_key()}"},
                    timeout=self.timeout,
                    transport=self._transport,
                )
            return self._http

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            try:
                r = self._client().request(method, path, json=payload)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise MistralError(f"{method} {path}: {type(e).__name__}") from e
                wait = min(60.0, 2.0**attempt) + random.random()
                self._note_retry("network", wait, type(e).__name__)
                self._sleep(wait)
                continue
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                retry_after = r.headers.get("retry-after")
                header_wait = (
                    float(retry_after)
                    if retry_after and retry_after.replace(".", "").isdigit()
                    else None
                )
                wait = (
                    header_wait
                    if header_wait is not None
                    else min(60.0, 2.0**attempt) + random.random()
                )
                kind = "429" if r.status_code == 429 else "5xx"
                self._note_retry(kind, wait, f"HTTP {r.status_code}")
                self._sleep(wait)
                continue
            if r.status_code >= 400:
                raise MistralError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
            return r.json()
        raise MistralError(f"{method} {path}: retries exhausted")

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
        random_seed: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if random_seed is not None:
            payload["random_seed"] = random_seed
        if response_format is not None:
            payload["response_format"] = response_format

        key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{key}.json"
        if path.is_file():
            cached = json.loads(path.read_text(encoding="utf-8"))["response"]
            c = Completion(**cached, cached=True)
        else:
            data = self._request("POST", "/chat/completions", payload)
            usage = data.get("usage") or {}
            c = Completion(
                text=message_text(data["choices"][0]["message"]),
                model=data.get("model", model),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                cached=False,
            )
            self._save(path, payload, c)
        self.usage.add(model, c)
        return c

    def _save(self, path: Path, payload: dict[str, Any], c: Completion) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        response = {k: v for k, v in c.__dict__.items() if k != "cached"}
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"request": payload, "response": response}), encoding="utf-8")
        os.replace(tmp, path)

    def resolve_model(self, name: str) -> dict[str, Any]:
        """Exact model id behind `name` (an id or an alias), with its deprecation date."""
        data = self._request("GET", "/models")
        models = data.get("data", data) if isinstance(data, dict) else data
        exact = [m for m in models if m.get("id") == name]
        by_alias = [m for m in models if name in (m.get("aliases") or [])]
        hits = exact or by_alias
        if not hits:
            raise MistralError(f"model {name!r} not found in /v1/models")
        m = hits[0]
        names = [m["id"], *(m.get("aliases") or [])]
        # Prefer a dated id (e.g. mistral-medium-2604) over a moving alias (…-latest).
        dated = [n for n in names if n[-4:].isdigit()]
        return {
            "requested": name,
            "id": dated[0] if dated else m["id"],
            "aliases": names,
            "deprecation": m.get("deprecation"),
        }
