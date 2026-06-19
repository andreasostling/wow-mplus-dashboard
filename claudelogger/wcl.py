"""Warcraft Logs v2 GraphQL client: OAuth client-credentials + a disk cache.

Stdlib only (urllib) so the tool runs with zero installs. Responses are cached
on disk keyed by a hash of the query+variables, so re-analysis is offline and
kind to the 3600-points/hour rate limit. Pass use_cache=False to force-refresh.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"


class WCLError(RuntimeError):
    pass


class WCLClient:
    def __init__(self, client_id: str, client_secret: str, cache_dir: Path):
        self._cid = client_id
        self._secret = client_secret
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._token: str | None = None

    # --- auth -------------------------------------------------------------
    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        basic = base64.b64encode(f"{self._cid}:{self._secret}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL, data=b"grant_type=client_credentials", method="POST"
        )
        req.add_header("Authorization", f"Basic {basic}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._token = json.loads(r.read())["access_token"]
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:400]
            raise WCLError(f"OAuth token failed: HTTP {e.code} {e.reason} — {body}") from e
        return self._token

    # --- query ------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self._cache_dir / f"q_{h}.json"

    def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
        retries: int = 3,
    ) -> dict[str, Any]:
        variables = variables or {}
        cache_key = json.dumps({"q": query, "v": variables}, sort_keys=True)
        cache_path = self._cache_path(cache_key)
        if use_cache and cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))

        token = self._ensure_token()
        body = json.dumps({"query": query, "variables": variables}).encode()
        last_err: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(API_URL, data=body, method="POST")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    payload = json.loads(r.read())
                if "errors" in payload:
                    raise WCLError(f"GraphQL errors: {json.dumps(payload['errors'])[:600]}")
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                body_txt = e.read().decode(errors="replace")[:400]
                raise WCLError(f"HTTP {e.code} {e.reason} — {body_txt}") from e
            except urllib.error.URLError as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise WCLError(f"Query failed after {retries} attempts: {last_err}")

    def rate_limit(self) -> dict[str, Any]:
        res = self.query(
            "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }",
            use_cache=False,
        )
        return res["data"]["rateLimitData"]
