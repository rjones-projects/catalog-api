"""
FileServiceClient

Routes all GitHub file access through a configurable local service instead of
calling GitHub directly. Configure the service URL via the FILE_SERVICE_URL
environment variable (default: https://repo-api-479677124022.europe-west2.run.app).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
import yaml

FILE_SERVICE_URL: str = os.getenv("FILE_SERVICE_URL", "https://repo-api-479677124022.europe-west2.run.app")
GH_TOKEN: Optional[str] = os.getenv("GH_TOKEN")


class FileServiceClient:
    """HTTP client that fetches files via the local GitHub file service."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or FILE_SERVICE_URL).rstrip("/")
        self.token = token if token is not None else GH_TOKEN

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, **params: Any) -> httpx.Response:
        url = f"{self.base_url}{endpoint}"
        filtered = {k: v for k, v in params.items() if v is not None}
        # repo-api authenticates via an Authorization: Bearer <token> header
        # (the token used to be passed as a ?token= query param).
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=filtered, headers=headers)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # File access helpers used by catalog_resolver
    # ------------------------------------------------------------------

    def get_file_content(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> Any:
        """
        Return the parsed `content` field from the file envelope.
        - YAML/JSON files   → already-parsed Python dict / list
        - Plain-text files  → string
        """
        resp = self._get(f"/repos/{owner}/{repo}/file", path=path, ref=ref)
        envelope = yaml.safe_load(resp.text) or {}
        return envelope.get("content")

    def get_text_file(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> str:
        """
        Fetch a plain-text file (e.g. .tf, .hcl) as a raw string.
        Uses raw=true so the service returns the file bytes without re-parsing.
        """
        resp = self._get(
            f"/repos/{owner}/{repo}/file", path=path, ref=ref, raw="true"
        )
        data = yaml.safe_load(resp.text) or {}
        content = data.get("content", "")
        return content if isinstance(content, str) else yaml.dump(content, allow_unicode=True)

    # ------------------------------------------------------------------
    # Catalog file helper used by main.py route handlers
    # ------------------------------------------------------------------

    def proxy_catalog_file(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str = "HEAD",
    ) -> list[dict]:
        """
        Fetch a multi-document YAML catalog file, returning a list of parsed docs.
        Uses get_file_content so the service handles GitHub auth.
        """
        content = self.get_file_content(owner, repo, path, ref)
        if isinstance(content, list):
            return [d for d in content if d is not None]
        if isinstance(content, dict):
            return [content]
        # Fallback: content came back as a string — parse it
        if isinstance(content, str):
            return [d for d in yaml.safe_load_all(content) if d is not None]
        return []


def get_client() -> FileServiceClient:
    """Return a FileServiceClient configured from FILE_SERVICE_URL and GH_TOKEN."""
    return FileServiceClient(FILE_SERVICE_URL, GH_TOKEN)
