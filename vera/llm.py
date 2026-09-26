"""Minimal OpenAI-compatible chat client (Gemini / Groq / DeepSeek / OpenAI / OpenRouter).

Config via env:
    LLM_PROVIDER   gemini | groq | deepseek | openai | openrouter   (sets default base URL + model)
    LLM_API_KEY    API key (required to enable the LLM path)
    LLM_MODEL      optional model override
    LLM_BASE_URL   optional base URL override (must expose /chat/completions)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

PRESETS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5"),
}

CACHE_PATH = Path(os.getenv("LLM_CACHE_PATH", Path(__file__).resolve().parent.parent / ".llm_cache.json"))


class LLM:
    def __init__(self) -> None:
        provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        base, model = PRESETS.get(provider, PRESETS["gemini"])
        self.provider = provider
        self.base_url = os.getenv("LLM_BASE_URL", base).rstrip("/")
        self.model = os.getenv("LLM_MODEL", model)
        self.api_key = os.getenv("LLM_API_KEY", "")
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._cooldown_until = 0.0
        if CACHE_PATH.exists():
            try:
                self._cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> str:
        return f"{self.provider}:{self.model}" if self.enabled else "deterministic-templates"

    def _key(self, system: str, prompt: str) -> str:
        return hashlib.sha256(f"{self.model}\n{system}\n{prompt}".encode("utf-8")).hexdigest()

    def complete(self, system: str, prompt: str, timeout: float = 12.0, max_tokens: int = 700) -> Optional[str]:
        if not self.enabled:
            return None
        key = self._key(system, prompt)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if time.time() < self._cooldown_until:  # rate-limited: let the caller use its template fallback
                return None
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if "gpt-oss" in self.model:  # reasoning model: keep thinking short so replies stay fast
            body["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "low")
            body["max_tokens"] = max_tokens + 600  # headroom for low-effort reasoning; counts toward TPM, so keep it tight
        text, status = self._post(body, timeout)
        if text is None and status == 400:  # some providers reject response_format — retry once without it
            body.pop("response_format", None)
            text, status = self._post(body, timeout)
        if text:
            with self._lock:
                self._cache[key] = text
                try:
                    CACHE_PATH.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
        return text

    def _post(self, body: dict, timeout: float) -> tuple[Optional[str], Optional[int]]:
        """Returns (content, http_status). On 429, pauses LLM use for the time the provider asks."""
        req = urlrequest.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            # explicit User-Agent: Cloudflare in front of some providers (Groq) rejects urllib's default with 403/1010
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "User-Agent": "vera-bot/1.0 (+magicpin-ai-challenge)", "Accept": "application/json"},
        )
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"], 200
        except urlerror.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", detail)
                wait = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 20.0
                with self._lock:
                    self._cooldown_until = max(self._cooldown_until, time.time() + min(wait, 120) + 0.5)
                print(f"[llm] rate-limited by {self.provider}; templates only for {wait:.0f}s", file=sys.stderr)
            else:
                print(f"[llm] HTTP {e.code} from {self.provider}: {detail}", file=sys.stderr)
            return None, e.code
        except (urlerror.URLError, TimeoutError, KeyError, IndexError, ValueError, OSError) as e:
            print(f"[llm] {self.provider} call failed: {e!r}", file=sys.stderr)
            return None, None


def parse_json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except ValueError:
        return None


_llm: Optional[LLM] = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm
