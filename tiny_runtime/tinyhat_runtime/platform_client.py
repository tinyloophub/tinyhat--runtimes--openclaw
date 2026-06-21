"""Minimal platform client constrained to the Computer runtime surface."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Callable

DEV_RUNTIME_BEARER = "dev-runtime"


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

    def api_url(self, path: str) -> str:
        normalized = "/" + path.strip("/")
        if (
            not normalized.startswith("/hapi/v1/computers/me/")
            or "/../" in f"{normalized}/"
        ):
            raise ValueError("path must stay within /hapi/v1/computers/me/*")
        return f"{self.base_url}{urllib.parse.quote(normalized, safe='/?=&')}"

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

    def get_json(self, path: str, *, timeout: int = 30) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token_provider is not None:
            token = self.token_provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.api_url(path), headers=headers)
        with self.opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("platform response must be a JSON object")
        return payload

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token_provider is not None:
            token = self.token_provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.api_url(path),
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self.opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("platform response must be a JSON object")
        return payload


def platform_base_url_from_env() -> str:
    base = (os.environ.get("TINYHAT_PLATFORM_BASE_URL") or "").strip()
    if not base:
        raise RuntimeError("TINYHAT_PLATFORM_BASE_URL is required")
    return base


def backend_audience_from_env() -> str:
    return (os.environ.get("TINYHAT_BACKEND_AUDIENCE") or "").strip()


def fetch_gce_identity_token(*, timeout: int = 5) -> str | None:
    audience = backend_audience_from_env()
    if not audience:
        return None
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        f"instance/service-accounts/default/identity?{query}"
    )
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8").strip()


def dev_runtime_identity_token() -> str | None:
    if (os.environ.get("TINYHAT_DEV_RUNTIME") or "").strip() == "1":
        return DEV_RUNTIME_BEARER
    return None


def default_platform_client() -> PlatformClient:
    token_provider = (
        dev_runtime_identity_token
        if (os.environ.get("TINYHAT_DEV_RUNTIME") or "").strip() == "1"
        else fetch_gce_identity_token
    )
    return PlatformClient(
        platform_base_url_from_env(),
        token_provider=token_provider,
    )
