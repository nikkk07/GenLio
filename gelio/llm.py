"""LLM provider abstraction with free-tier Groq (primary) and Gemini (fallback).

Design goals:
  * Free services only — Groq and Google Gemini free tiers, keys from env.
  * JSON discipline — every call requests JSON-only output; we strip code
    fences and ``json.loads`` the result before handing back a ``dict``.
  * Resilience — network/5xx errors retry with exponential backoff (tenacity);
    if the primary provider exhausts its retries, we fail over to the fallback.

The rest of the codebase depends only on the small :class:`JSONLLM` protocol
(``generate_json``), so unit tests inject a plain mock and never touch the
network.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import Settings

logger = logging.getLogger("gelio.llm")

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# High enough for creative copy, low enough that strict JSON stays well-formed.
_TEMPERATURE = 0.6


class LLMError(RuntimeError):
    """Raised when an LLM call cannot be completed or parsed."""


class RetryableHTTPError(RuntimeError):
    """A transient HTTP error (429 / 5xx) that is worth retrying."""


@runtime_checkable
class JSONLLM(Protocol):
    """Minimal interface the rest of gelio depends on."""

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        ...


def _strip_fences(text: str) -> str:
    """Remove Markdown code fences an LLM may wrap around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text)
    return text.strip()


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse an LLM response into a JSON object, tolerating code fences.

    Falls back to extracting the outermost ``{...}`` span if there is leading
    or trailing prose.
    """
    cleaned = _strip_fences(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMError(f"response is not JSON: {raw[:200]!r}")
        try:
            obj = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError(f"expected a JSON object, got {type(obj).__name__}")
    return obj


# Retry transient transport errors and explicitly-classified 429/5xx, but NOT
# permanent 4xx (bad request, auth) which will never succeed on retry.
_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, RetryableHTTPError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(4),
    reraise=True,
)


def _raise_for_status(provider: str, resp: httpx.Response) -> None:
    """Classify an HTTP error: retryable (429/5xx) vs fatal (other 4xx).

    Always includes the response body so failures are diagnosable instead of a
    bare status code.
    """
    if resp.status_code < 400:
        return
    body = resp.text[:500]
    msg = f"{provider} HTTP {resp.status_code}: {body}"
    # Groq's strict json_object mode 400s when the model emits malformed JSON.
    # That is a stochastic generation failure, not a bad request — re-roll it.
    if resp.status_code == 429 or resp.status_code >= 500 or (
        resp.status_code == 400 and "json_validate_failed" in body
    ):
        raise RetryableHTTPError(msg)
    raise LLMError(msg)


class LLMProvider(ABC):
    """Abstract single-provider client returning a parsed JSON object."""

    name: str = "abstract"

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the raw model text for a JSON-mode completion."""

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.complete(system, user)
        return parse_json_object(raw)


class GroqProvider(LLMProvider):
    """Groq OpenAI-compatible chat completions (free tier)."""

    name = "groq"
    _URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set")
        self._api_key = api_key
        self._model = model

    @_retry
    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": _TEMPERATURE,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(self._URL, json=payload, headers=headers)
            _raise_for_status(self.name, resp)
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:  # pragma: no cover - defensive
            raise LLMError(f"unexpected Groq response shape: {data}") from exc


class GeminiProvider(LLMProvider):
    """Google Gemini generateContent (free tier)."""

    name = "gemini"
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._api_key = api_key
        self._model = model

    @_retry
    def complete(self, system: str, user: str) -> str:
        url = f"{self._BASE}/{self._model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": _TEMPERATURE,
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": self._api_key}
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(url, json=payload, headers=headers)
            _raise_for_status(self.name, resp)
            data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:  # pragma: no cover - defensive
            raise LLMError(f"unexpected Gemini response shape: {data}") from exc


class LLMClient:
    """Primary-with-fallback client implementing :class:`JSONLLM`.

    Tries providers in order; each provider already retries transient errors
    internally, so a provider that still raises is considered down and we move
    to the next. Raises :class:`LLMError` only if every provider fails.
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise LLMError("LLMClient requires at least one provider")
        self._providers = providers

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for provider in self._providers:
            try:
                logger.info("llm call provider=%s", provider.name)
                return provider.generate_json(system, user)
            except Exception as exc:  # noqa: BLE001 - fail over on any error
                logger.warning(
                    "provider=%s failed, falling over: %s", provider.name, exc
                )
                last_error = exc
        raise LLMError(f"all LLM providers failed; last error: {last_error}")


def _build_provider(name: str, settings: Settings) -> LLMProvider:
    if name == "groq":
        return GroqProvider(settings.groq_api_key, settings.groq_model)
    if name == "gemini":
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    raise LLMError(f"unknown provider {name!r}")


def build_client(settings: Settings) -> LLMClient:
    """Construct an :class:`LLMClient` honoring primary/fallback selection.

    Providers whose API key is missing are skipped (so a single configured key
    still works). Raises :class:`LLMError` if no provider can be built.
    """
    order = [settings.provider, settings.fallback_provider]
    providers: list[LLMProvider] = []
    for name in order:
        try:
            providers.append(_build_provider(name, settings))
        except LLMError as exc:
            logger.warning("skipping provider=%s: %s", name, exc)
    if not providers:
        raise LLMError(
            "No usable LLM provider. Set GROQ_API_KEY and/or GEMINI_API_KEY in .env"
        )
    return LLMClient(providers)
