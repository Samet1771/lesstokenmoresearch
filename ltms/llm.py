"""Local model client.

Ollama native and any OpenAI-compatible server (LM Studio, llama.cpp, vLLM).
No cloud providers -- that is a deliberate constraint of this project, not an
omission.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .config import ModelConfig

# Reasoning models emit a visible thinking block. Locally that is pure latency
# for extraction work, so we disable it where supported and strip it otherwise.
THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)

# /v1/models lists embedding and reranker models alongside chat models. Picking
# one of those automatically produces a baffling failure, so exclude them.
NON_CHAT_MODEL = re.compile(r"(embed|embedding|reranker|rerank|bge-|e5-|gte-|clip|whisper|tts)", re.I)


def is_embedding_model(name: str) -> bool:
    return bool(NON_CHAT_MODEL.search(name))


class ModelError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0


def strip_thinking(text: str) -> str:
    return THINK_BLOCK.sub("", text).strip()


class LocalModel:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._resolved: str = config.name

    @property
    def resolved_name(self) -> str:
        return self._resolved or self.config.name or "(unset)"

    @property
    def openai_base(self) -> str:
        """LM Studio users paste the url with or without /v1; accept both."""
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    @property
    def list_url(self) -> str:
        if self.config.provider == "ollama":
            return f"{self.base_url}/api/tags"
        return f"{self.openai_base}/models"

    async def list_models(self, client: httpx.AsyncClient | None = None) -> list[str]:
        owns = client is None
        client = client or httpx.AsyncClient(timeout=5.0)
        try:
            response = await client.get(self.list_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        finally:
            if owns:
                await client.aclose()

        if self.config.provider == "ollama":
            entries = payload.get("models", [])
            return [entry["name"] for entry in entries if entry.get("name")]
        entries = payload.get("data", [])
        return [entry["id"] for entry in entries if entry.get("id")]

    async def available(self, client: httpx.AsyncClient | None = None) -> bool:
        owns = client is None
        client = client or httpx.AsyncClient(timeout=4.0)
        try:
            response = await client.get(self.list_url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False
        finally:
            if owns:
                await client.aclose()

    async def ensure_model(self, client: httpx.AsyncClient | None = None) -> str:
        """Resolve an empty model name to whatever the server has loaded."""
        if self._resolved:
            return self._resolved
        models = await self.list_models(client)
        chat_models = [name for name in models if not is_embedding_model(name)]
        if not chat_models:
            detail = f" (only embedding models are loaded: {', '.join(models)})" if models else ""
            raise ModelError(
                f"no chat model available at {self.base_url}{detail}.\n"
                "In LM Studio: load a model, then turn the local server on "
                "(Developer tab). Or set model.name in your ltms config."
            )
        self._resolved = chat_models[0]
        return self._resolved

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 800,
        temperature: float = 0.2,
        client: httpx.AsyncClient | None = None,
        timeout: float = 180.0,
    ) -> Reply:
        owns = client is None
        client = client or httpx.AsyncClient(timeout=timeout)
        try:
            await self.ensure_model(client)
            if self.config.provider == "ollama":
                return await self._ollama(client, system, user, max_tokens, temperature)
            return await self._openai(client, system, user, max_tokens, temperature)
        finally:
            if owns:
                await client.aclose()

    async def _ollama(
        self, client: httpx.AsyncClient, system: str, user: str, max_tokens: int, temperature: float
    ) -> Reply:
        payload = {
            "model": self._resolved,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "options": {
                "num_predict": max_tokens,
                "num_ctx": self.config.context_tokens,
                "temperature": temperature,
            },
        }
        try:
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
        except httpx.HTTPError as error:
            raise ModelError(f"cannot reach model server at {self.base_url}: {error}") from error
        if response.status_code == 400 and "think" in response.text:
            # Older Ollama, or a model with no thinking mode: retry without it.
            payload.pop("think")
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
        if response.status_code != 200:
            raise ModelError(f"ollama {response.status_code}: {response.text[:300]}")
        data = response.json()
        return Reply(
            text=strip_thinking(data.get("message", {}).get("content", "")),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )

    async def _openai(
        self, client: httpx.AsyncClient, system: str, user: str, max_tokens: int, temperature: float
    ) -> Reply:
        base = self.openai_base
        payload: dict = {
            "model": self._resolved,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            # Understood by LM Studio and vLLM for Qwen-style reasoning models.
            # Servers that do not know it either ignore it or 400, and we retry.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            response = await client.post(f"{base}/chat/completions", json=payload)
            if response.status_code >= 400 and "chat_template_kwargs" in response.text:
                payload.pop("chat_template_kwargs")
                response = await client.post(f"{base}/chat/completions", json=payload)
        except httpx.HTTPError as error:
            raise ModelError(f"cannot reach model server at {base}: {error}") from error
        if response.status_code != 200:
            raise ModelError(f"model server {response.status_code}: {response.text[:300]}")

        data = response.json()
        usage = data.get("usage") or {}
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = strip_thinking(message.get("content") or "")

        # A reasoning model that could not be talked out of thinking returns its
        # whole answer under `reasoning_content` with an empty `content`. The
        # answer is usually still in there, so look rather than fail.
        if not text:
            text = strip_thinking(message.get("reasoning_content") or "")

        if not text and choice.get("finish_reason") == "length":
            raise ModelError(
                f"{self._resolved} spent its entire {max_tokens}-token budget thinking and "
                "produced no answer. Use a non-reasoning model for this work, or disable "
                "thinking in the server."
            )

        return Reply(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


@dataclass
class DetectedServer:
    label: str
    provider: str
    base_url: str
    models: list[str]


def detect_servers(extra: list[tuple[str, str, str]] | None = None) -> list[DetectedServer]:
    """Probe the usual local model servers. Synchronous: used by setup and status."""
    from .config import KNOWN_SERVERS

    found: list[DetectedServer] = []
    for label, provider, base_url in (extra or []) + KNOWN_SERVERS:
        probe = LocalModel(ModelConfig(provider=provider, base_url=base_url))
        try:
            response = httpx.get(probe.list_url, timeout=2.0)
            if response.status_code != 200:
                continue
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        if provider == "ollama":
            models = [entry["name"] for entry in payload.get("models", []) if entry.get("name")]
        else:
            models = [entry["id"] for entry in payload.get("data", []) if entry.get("id")]
        found.append(DetectedServer(label=label, provider=provider, base_url=base_url, models=models))
    return found


def parse_json_list(text: str) -> list[str]:
    """Pull a JSON array of strings out of a model reply, tolerating prose around it."""
    match = re.search(r"\[.*?\]", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [str(item).strip() for item in data if isinstance(item, (str, int, float)) and str(item).strip()]


def estimate_tokens(text: str) -> int:
    """Rough but stable: good enough for a savings counter, never billed on."""
    return max(1, len(text) // 4)
