"""Minimal platform client constrained to the `/me/*` runtime surface."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable


class PlatformClient:
    def __init__(
        self,
        base_url: str,
        *,
        token_provider: Callable[[], str | None] | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self.opener = opener

    def me_url(self, suffix: str) -> str:
        normalized = suffix.strip("/")
        if not normalized or normalized.startswith("..") or "/../" in f"/{normalized}/":
            raise ValueError("me suffix must be a relative /me/* path")
        return f"{self.base_url}/me/{urllib.parse.quote(normalized, safe='/')}"

    def get_me_json(self, suffix: str, *, timeout: int = 20) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token_provider is not None:
            token = self.token_provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.me_url(suffix), headers=headers)
        with self.opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("/me response must be a JSON object")
        return payload
