"""File-backed fake of the huggingface_hub transport used by kgbridge.

Enabled via KGBRIDGE_FAKE_HF=1 for offline end-to-end tests. State is kept in
a JSON file (KGBRIDGE_FAKE_STORE, default /tmp/kgbridge_fake_store.json) so
that worker threads and kernel subprocesses share the same view.
"""
from __future__ import annotations

import json
import os
import threading

_FILE = os.environ.get("KGBRIDGE_FAKE_STORE", "/tmp/kgbridge_fake_store.json")
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        with open(_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(store: dict) -> None:
    tmp = _FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, _FILE)


def up(path: str, data: bytes) -> None:
    with _LOCK:
        store = _load()
        store[path] = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        _save(store)


def dl(path: str) -> bytes | None:
    with _LOCK:
        v = _load().get(path)
    return v.encode() if isinstance(v, str) else v


def list_dir(path: str) -> list[str]:
    prefix = (path.rstrip("/") + "/") if path else ""
    with _LOCK:
        return [p for p in _load() if p.startswith(prefix)]


def delete(path: str) -> None:
    with _LOCK:
        store = _load()
        store.pop(path, None)
        _save(store)


def clear() -> None:
    with _LOCK:
        _save({})


class HfApi:  # minimal stand-in
    _default_token = "fake-token"

    def __init__(self, token=None):
        self._token = token if token is not None else HfApi._default_token

    @property
    def token(self):
        return self._token

    def upload_file(self, path_or_fileobj, path_in_repo, repo_id=None, repo_type=None, **kw):
        data = path_or_fileobj if isinstance(path_or_fileobj, bytes) else bytes(
            path_or_fileobj.read()
            if hasattr(path_or_fileobj, "read")
            else str(path_or_fileobj).encode()
        )
        up(path_in_repo, data)
        return {"status": "ok"}

    def list_repo_files(self, repo_id=None, repo_type=None, **kw):
        return list_dir("")

    def list_repo_tree(self, repo_id=None, path_in_repo="", repo_type=None, recursive=False, **kw):
        class E:
            def __init__(self, p):
                self.path = p
        return [E(p) for p in list_dir(path_in_repo)]

    def delete_file(self, path_in_repo, repo_id=None, repo_type=None, **kw):
        delete(path_in_repo)
