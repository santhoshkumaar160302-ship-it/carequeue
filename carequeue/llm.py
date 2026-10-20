"""A small client for asking the NexusAI gateway for JSON.

The gateway picks the model, fails over between providers and caches answers; this
side only sends a prompt, gets text back, and pulls the JSON object out of it. Every
caller treats the model's answer as untrusted and validates it before using it.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx


class GatewayUnavailable(Exception):
    """No URL configured, or the gateway failed, timed out or replied with no usable JSON."""


@dataclass
class Gateway:
    url: str | None
    api_key: str | None = None
    transport: httpx.BaseTransport | None = None
    use_cache: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def ask_json(self, system: str, user: str, timeout_s: float, max_tokens: int = 600) -> tuple[dict, dict]:
        if not self.url:
            raise GatewayUnavailable("NEXUSAI_URL is not set")
        payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   "temperature": 0, "max_tokens": max_tokens}
        if not self.use_cache:
            payload["cache"] = False
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_s, transport=self.transport) as client:
                resp = client.post(f"{self.url}/v1/chat", headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
            data = extract_json(body.get("content", ""))
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayUnavailable(f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0][:160]}") from exc
        meta = {"model": body.get("model"), "cache": body.get("cache"), "cost_usd": body.get("cost_usd"),
                "ms": round((time.perf_counter() - start) * 1000)}
        return data, meta


def extract_json(content: str) -> dict:
    """The first JSON object in a reply, even if the model wrapped it in prose or a code block."""
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("no JSON object in the reply")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("the reply's JSON is not an object")
    return data
