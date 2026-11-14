#!/usr/bin/env python3
"""Local Jupyter kernel that executes cells on the Kaggle runtime via kgbridge.

VS Code sees this as an ordinary kernel ("Kaggle Bridge (LosPollos)"); every
cell execution is shipped to the worker loop running on Kaggle (see
`python3 scripts/kg.py bridge start`), and outputs (streams, rich displays,
errors) are merged back inline. Variables persist on the Kaggle side.

Config (JSON), resolved from $KGBRIDGE_CONFIG, else <repo>/kaggle/bridge.local.json:
    {"repo": "<hf-user>/<private-dataset>", "key": "<session-key>", "token": "<hf-token, optional>"}
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kgbridge_transport import Transport

try:
    from ipykernel.kernelbase import Kernel
except Exception:  # pragma: no cover — importable without ipykernel (for kg.py)
    Kernel = None

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "kaggle" / "bridge.local.json"

if Kernel is not None:

    class KaggleBridgeKernel(Kernel):
        implementation = "kgbridge"
        implementation_version = "1.0"
        language = "python"
        language_version = "3"
        banner = "Kaggle Bridge (LosPollos) — executes cells on the Kaggle runtime"
        language_info = {
            "name": "python",
            "mimetype": "text/x-python",
            "file_extension": ".py",
        }

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            cfg_path = Path(os.environ.get("KGBRIDGE_CONFIG", str(DEFAULT_CONFIG)))
            if not cfg_path.exists():
                raise RuntimeError(
                    f"kgbridge config not found at {cfg_path}. "
                    "Run: python3 scripts/kg.py bridge init"
                )
            self.cfg = json.loads(cfg_path.read_text())
            self.t = Transport(self.cfg)
            self.poll_interval = float(self.cfg.get("poll_interval", 1.0))
            self.stale_timeout = float(self.cfg.get("stale_timeout", 180))
            self.count = 0
            self._req_dir = f"req/{self.t.key}"
            self._res_dir = f"res/{self.t.key}"
            self._hb_path = f"state/{self.t.key}/heartbeat.json"

        # -- diagnostics ------------------------------------------------------

        def _bridge_alive(self) -> tuple[bool, str]:
            if not self.t.head_ok(self._hb_path):
                return False, (
                    "Kaggle bridge is not running.\n"
                    "Start it with:  python3 scripts/kg.py bridge start\n"
                    "(or paste the cells from `python3 scripts/kg.py bridge remote` "
                    "into a live Kaggle session)"
                )
            raw = self.t.download(self._hb_path)
            try:
                ts = json.loads(raw).get("ts", 0)
            except Exception:
                ts = 0
            age = time.time() - float(ts)
            if age > self.stale_timeout:
                return False, (
                    f"Kaggle bridge heartbeat is stale ({age:.0f}s old > "
                    f"{self.stale_timeout:.0f}s). The worker probably died or the "
                    "session expired — restart with: python3 scripts/kg.py bridge start"
                )
            return True, ""

        # -- protocol ---------------------------------------------------------

        def do_execute(self, code, silent, store_history=True, user_expressions=None,
                       allow_stdin=False, *, cell_meta=None, cell_id=None):
            self.count += 1
            if not code.strip():
                return {"status": "ok", "execution_count": self.count,
                        "user_expressions": {}, "payload": []}

            alive, msg = self._bridge_alive()
            if not alive:
                self._error(msg.splitlines(), ename="BridgeOffline", evalue=msg)
                return {"status": "error", "ename": "BridgeOffline", "evalue": msg,
                        "traceback": msg.splitlines(), "execution_count": self.count}

            seq = f"{time.time_ns():020d}"
            self.t.upload(f"{self._req_dir}/{seq}.json",
                          {"v": 1, "key": self.t.key, "code": code})

            res_path = f"{self._res_dir}/{seq}.json"
            deadline = time.time() + self.stale_timeout
            result = None
            while True:
                raw = self.t.download(res_path)
                if raw is not None:
                    result = json.loads(raw)
                    break
                if time.time() > deadline:
                    alive, msg = self._bridge_alive()
                    if not alive:
                        self._error(msg.splitlines(), ename="BridgeDied", evalue=msg)
                        return {"status": "error", "ename": "BridgeDied", "evalue": msg,
                                "traceback": msg.splitlines(), "execution_count": self.count}
                    deadline = time.time() + self.stale_timeout
                time.sleep(self.poll_interval)

            if result.get("stdout"):
                self.send_response(self.iopub_socket, "stream",
                                   {"name": "stdout", "text": result["stdout"]})
            if result.get("stderr"):
                self.send_response(self.iopub_socket, "stream",
                                   {"name": "stderr", "text": result["stderr"]})
            for item in result.get("outs", []):
                data = {}
                for mime, val in item.items():
                    if isinstance(val, dict) and "b64" in val:
                        data[mime] = base64.b64decode(val["b64"])
                    else:
                        data[mime] = val
                self.send_response(self.iopub_socket, "display_data",
                                   {"data": data, "metadata": {}})

            if result.get("status") == "error":
                tb = result.get("traceback", ["unknown error"])
                self._error(tb, result.get("ename", "Error"), result.get("evalue", ""))
                return {"status": "error", "ename": result.get("ename", "Error"),
                        "evalue": result.get("evalue", ""), "traceback": tb,
                        "execution_count": self.count}

            return {"status": "ok", "execution_count": self.count,
                    "user_expressions": {}, "payload": []}

        def _error(self, traceback, ename, evalue):
            self.send_response(self.iopub_socket, "error", {
                "ename": ename, "evalue": evalue,
                "traceback": [l if isinstance(l, str) else str(l) for l in traceback],
            })

        def do_complete(self, code, cursor_pos):
            return {"matches": [], "cursor_start": cursor_pos,
                    "cursor_end": cursor_pos, "metadata": {}, "status": "ok"}


if __name__ == "__main__":
    if Kernel is None:
        sys.exit("ipykernel is not installed for this python — "
                 "run `python3 scripts/kg.py bridge install` to pick a working one.")
    from ipykernel.kernelapp import IPKernelApp

    IPKernelApp.launch_instance(kernel_class=KaggleBridgeKernel)
