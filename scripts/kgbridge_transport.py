"""Shared HF-backed transport for the kgbridge worker and local kernel.

Used by scripts/kgbridge_kernel.py (the local Jupyter kernel) and by
scripts/kg.py's bridge subcommands. Set KGBRIDGE_FAKE_HF=1 to use the
in-memory fake (scripts/kgbridge_fake.py) for offline testing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

FAKE_HF = os.environ.get("KGBRIDGE_FAKE_HF") == "1"


def hf_token(cfg: dict | None = None) -> str | None:
    tok = (cfg or {}).get("token") or os.environ.get("HF_TOKEN")
    if tok:
        return tok
    p = Path.home() / ".cache" / "huggingface" / "token"
    if p.exists():
        return p.read_text().strip() or None
    return None


def resolve_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def auth_headers(cfg: dict | None) -> dict:
    tok = hf_token(cfg)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


class Transport:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.repo = cfg["repo"]
        self.key = cfg["key"]
        self._api = None
        if not FAKE_HF:
            from huggingface_hub import HfApi

            self._api = HfApi(token=hf_token(cfg))

    def upload(self, path: str, obj) -> None:
        data = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        if FAKE_HF:
            import kgbridge_fake

            kgbridge_fake.up(path, data)
            return
        self._api.upload_file(
            path_or_fileobj=data, path_in_repo=path, repo_id=self.repo, repo_type="dataset"
        )

    def download(self, path: str) -> bytes | None:
        if FAKE_HF:
            import kgbridge_fake

            return kgbridge_fake.dl(path)
        r = requests.get(resolve_url(self.repo, path), headers=auth_headers(self.cfg), timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content

    def head_ok(self, path: str) -> bool:
        if FAKE_HF:
            import kgbridge_fake

            return kgbridge_fake.dl(path) is not None
        try:
            r = requests.head(
                resolve_url(self.repo, path), headers=auth_headers(self.cfg),
                allow_redirects=True, timeout=30,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_files(self, prefix: str | None = None) -> list[str]:
        if FAKE_HF:
            import kgbridge_fake

            files = kgbridge_fake.list_dir("") if prefix is None else kgbridge_fake.list_dir(prefix)
            return files
        files = self._api.list_repo_files(repo_id=self.repo, repo_type="dataset")
        if prefix is None:
            return files
        return [f for f in files if f.startswith(prefix.rstrip("/") + "/")]
