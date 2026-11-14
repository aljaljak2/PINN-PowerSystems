#!/usr/bin/env python3
"""kg - sync the local notebook with the shared Kaggle kernel.

The shared kernel is defined in kaggle/kernel-metadata.json (committed).
Every collaborator authenticates with their own Kaggle token (~/.kaggle/kaggle.json
or ~/.kaggle/access_token) and must be added as a collaborator on the kernel.

How it works:
  push   strips outputs, appends a hidden housekeeping cell, pushes a new version
         to Kaggle and starts (or skips) a run
  watch  polls the run status until it finishes
  pull   downloads the output bundle; the housekeeping cell had saved the
         *executed* notebook (with cell outputs) as an artifact, which we merge
         back into the local notebook so results appear inline in VS Code
  run    push + watch + pull
  sync   compares local notebook against the kernel's current source (drift check)
  status / logs

Typical loop:
  python scripts/kg.py run
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NB = ROOT / "lospollos.ipynb"
DEFAULT_META = ROOT / "kaggle" / "kernel-metadata.json"
OUTDIR = ROOT / "kaggle" / "output"

HK_TAG = "kg-housekeeping"  # cell metadata tag identifying the appended cell
HK_PREFIX = "executed_"  # executed_<stem>.ipynb artifact name

TERMINAL = {"COMPLETE", "ERROR", "CANCEL", "CANCELLED"}


# --------------------------------------------------------------------------- helpers

def _api():
    from kaggle.api.kaggle_api_extended import KaggleApi

    api = KaggleApi()
    api.authenticate()
    return api


def _load_meta(meta_path) -> dict:
    meta_path = Path(meta_path)
    if not meta_path.exists():
        sys.exit(f"metadata file not found: {meta_path}")
    meta = json.loads(meta_path.read_text())
    if not meta.get("id"):
        sys.exit(f"'id' missing in {meta_path}")
    if "version" in meta["id"]:
        sys.exit(f"'id' must not contain a version: {meta['id']}")
    return meta


def _kernel_id(meta: dict) -> str:
    return meta["id"]


def _executed_name(meta: dict) -> str:
    return f"{HK_PREFIX}{Path(meta.get('code_file', 'notebook.ipynb')).stem}.ipynb"


def _hk_cell(executed_name: str) -> dict:
    """Housekeeping cell: saves the executed notebook (with outputs) as an output
    artifact, so `pull` can merge cell outputs back locally."""
    src = "/kaggle/working/__notebook__.ipynb"
    dst = f"/kaggle/working/{executed_name}"
    code = (
        "import os, shutil\n"
        f"_src, _dst = {src!r}, {dst!r}\n"
        "if os.path.exists(_src):\n"
        "    shutil.copy(_src, _dst)\n"
    )
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {"tags": [HK_TAG]},
        "outputs": [],
        "source": code,
    }


def _strip_nb(nb: dict) -> dict:
    """Return a copy of the notebook without outputs/execution counts and
    without any housekeeping cell."""
    cells = []
    for cell in nb["cells"]:
        if HK_TAG in cell.get("metadata", {}).get("tags", []):
            continue
        c = dict(cell)
        if c["cell_type"] == "code":
            c["outputs"] = []
            c["execution_count"] = None
        cells.append(c)
    return {**nb, "cells": cells}


def _cell_src(cell: dict) -> str:
    s = cell.get("source", "")
    return s if isinstance(s, str) else "".join(s)


def _norm_status(raw: str) -> str:
    return (raw or "").split(".")[-1].upper()


# --------------------------------------------------------------------------- commands

def cmd_push(args) -> int:
    meta = _load_meta(args.meta)
    nb_path = Path(args.nb)
    nb = json.loads(nb_path.read_text())

    executed_name = _executed_name(meta)
    prepared = _strip_nb(nb)
    prepared["cells"].append(_hk_cell(executed_name))

    # drift check against what is currently on Kaggle, unless overridden
    if not args.no_sync_check:
        drift = _drift(meta, nb_path)
        if drift is None:
            print("[kg] (could not check kernel source for drift)")
        elif drift:
            print(f"[kg] WARNING: local notebook differs from the kernel's current source ({drift} differing cells).")
            print("[kg] Your push will overwrite the kernel's version. Run 'kg sync' to review first.")
            if not args.force:
                sys.exit("refusing to push; use --force to override")

    with tempfile.TemporaryDirectory(prefix="kg-push-") as tmp:
        tmp = Path(tmp)
        shutil.copy(nb_path.parent / nb_path.name, tmp / nb_path.name)
        (tmp / nb_path.name).write_text(json.dumps(prepared, indent=1))
        shutil.copy(args.meta, tmp / "kernel-metadata.json")

        api = _api()
        print(f"[kg] pushing {_kernel_id(meta)} ({'no-run' if args.no_run else 'save & run'}) ...")
        resp = api.kernels_push(
            str(tmp), acc=args.acc, no_run=args.no_run, timeout=args.timeout
        )
        url = getattr(resp, "url", None) or f"https://www.kaggle.com/code/{_kernel_id(meta)}"
        print(f"[kg] pushed: {url}")
        print(f"[kg] watch with: python scripts/kg.py watch")
    return 0


def _drift(meta: dict, nb_path: Path) -> int | None:
    """Number of differing code cells between local notebook and kernel source.
    None = could not check."""
    api = _api()
    with tempfile.TemporaryDirectory(prefix="kg-sync-") as tmp:
        tmp = Path(tmp)
        try:
            api.kernels_pull(_kernel_id(meta), path=str(tmp), metadata=False, quiet=True)
        except Exception as e:
            print(f"[kg] sync check failed: {e}")
            return None
        pulled_file = tmp / meta.get("code_file", "lospollos.ipynb")
        if not pulled_file.exists():
            candidates = list(tmp.glob("*.ipynb"))
            if not candidates:
                return None
            pulled_file = candidates[0]
        remote = json.loads(pulled_file.read_text())
    local = _strip_nb(json.loads(nb_path.read_text()))
    remote_cells = _strip_nb(remote)["cells"]
    a, b = [_cell_src(c) for c in local["cells"]], [_cell_src(c) for c in remote_cells]
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x != y)


def cmd_sync(args) -> int:
    meta = _load_meta(args.meta)
    drift = _drift(meta, Path(args.nb))
    if drift is None:
        print("[kg] could not compare with kernel source.")
        return 2
    if drift == 0:
        print("[kg] local notebook and kernel source are in sync.")
        return 0
    print(f"[kg] {drift} cell(s) differ between local notebook and kernel source.")
    print("[kg] 'kg push' overwrites the kernel; use 'kg sync --diff' to see details.")
    return 1


def cmd_status(args) -> int:
    meta = _load_meta(args.meta)
    api = _api()
    resp = api.kernels_status(_kernel_id(meta))
    status = _norm_status(str(getattr(resp, "status", resp)))
    msg = getattr(resp, "failure_message", None)
    print(f"[kg] {_kernel_id(meta)}: {status}" + (f" — {msg}" if msg else ""))
    return 0 if status in TERMINAL else 3


def cmd_watch(args) -> int:
    meta = _load_meta(args.meta)
    api = _api()
    deadline = time.time() + args.max_minutes * 60
    last = None
    while True:
        resp = api.kernels_status(_kernel_id(meta))
        status = _norm_status(str(getattr(resp, "status", resp)))
        if status != last:
            print(f"[kg] {time.strftime('%H:%M:%S')} status: {status}", flush=True)
            last = status
        if status == "COMPLETE":
            print("[kg] run complete → python scripts/kg.py pull")
            return 0
        if status in {"ERROR", "CANCEL", "CANCELLED"}:
            msg = getattr(resp, "failure_message", None)
            print(f"[kg] run failed: {status}" + (f" — {msg}" if msg else ""))
            return 1
        if time.time() > deadline:
            print(f"[kg] gave up waiting after {args.max_minutes} min (still {status})")
            return 2
        time.sleep(args.interval)


def cmd_pull(args) -> int:
    meta = _load_meta(args.meta)
    kernel = _kernel_id(meta)
    executed_name = _executed_name(meta)
    OUTDIR.mkdir(parents=True, exist_ok=True)

    api = _api()
    files, _token = api.kernels_output(kernel, path=str(OUTDIR), quiet=True)
    files = files or []
    print(f"[kg] downloaded {len(files)} output file(s) to {OUTDIR}")
    for f in files:
        print(f"     - {os.path.relpath(f, ROOT)}")

    executed = Path(OUTDIR) / executed_name
    if not executed.exists():
        print(f"[kg] no executed notebook artifact ({executed_name}).")
        print("[kg] If this run was pushed with this tool, the housekeeping cell failed —")
        print("[kg] otherwise this version predates the tooling. Results are on kaggle.com.")
        return 1

    merged = _merge_outputs(Path(args.nb), executed, meta)
    return 0 if merged else 1


def _merge_outputs(nb_path: Path, executed_path: Path, meta: dict) -> bool:
    """Merge cell outputs from the executed artifact into the local notebook."""
    local = json.loads(nb_path.read_text())
    executed = json.loads(executed_path.read_text())

    remote_cells = [c for c in executed["cells"] if HK_TAG not in c.get("metadata", {}).get("tags", [])]
    local_cells = local["cells"]

    if len(remote_cells) != len(local_cells):
        print(
            f"[kg] cell count mismatch: local {len(local_cells)} vs executed {len(remote_cells)} — "
            "did the notebook change between push and pull? Skipping merge."
        )
        return False

    merged, mismatched = 0, []
    for i, (lc, rc) in enumerate(zip(local_cells, remote_cells)):
        if lc["cell_type"] != "code":
            continue
        if _cell_src(lc) != _cell_src(rc):
            mismatched.append(i)
            continue
        if rc.get("outputs"):
            lc["outputs"] = rc["outputs"]
            lc["execution_count"] = rc.get("execution_count")
            merged += 1

    if mismatched:
        print(f"[kg] WARNING: {len(mismatched)} cell(s) differ in source; outputs not merged for them: {mismatched}")

    nb_path.write_text(json.dumps(local, indent=1))
    print(f"[kg] merged outputs into {os.path.relpath(nb_path, ROOT)} for {merged} code cell(s).")
    print("[kg] reload the notebook in VS Code to see results inline.")
    return True


def cmd_run(args) -> int:
    rc = cmd_push(args)
    if rc != 0:
        return rc
    rc = cmd_watch(args)
    if rc != 0:
        print("[kg] pull skipped because the run failed.")
        return rc
    pull_args = argparse.Namespace(
        nb=args.nb, meta=args.meta, json_output=False, output_format=None
    )
    return cmd_pull(pull_args)


def cmd_logs(args) -> int:
    meta = _load_meta(args.meta)
    api = _api()
    seen = 0
    for entry in api.kernels_logs_stream(_kernel_id(meta)):
        data = entry.get("data", "")
        if not args.stderr_only or entry.get("stream_name") == "stderr":
            if seen >= args.tail:
                continue
            seen += 1
            print(data.rstrip())
    return 0


# --------------------------------------------------------------------------- cli

def main() -> int:
    p = argparse.ArgumentParser(prog="kg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nb", default=str(DEFAULT_NB), help="notebook path (default: lospollos.ipynb)")
    p.add_argument("--meta", default=str(DEFAULT_META), help="kernel metadata path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("push", help="push notebook to Kaggle (appends housekeeping cell)")
    sp.add_argument("--no-run", action="store_true", help="save version without executing")
    sp.add_argument("--acc", help="accelerator override, e.g. NvidiaTeslaT4")
    sp.add_argument("--timeout", help="max run time in seconds")
    sp.add_argument("--force", action="store_true", help="push even if kernel source drifted")
    sp.add_argument("--no-sync-check", action="store_true", help="skip drift check")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("watch", help="poll run status until finished")
    sp.add_argument("--interval", type=int, default=15)
    sp.add_argument("--max-minutes", type=int, default=180)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("pull", help="download outputs and merge cell outputs locally")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("run", help="push + watch + pull")
    sp.add_argument("--no-run", action="store_true")
    sp.add_argument("--acc", help="accelerator override")
    sp.add_argument("--timeout", help="max run time in seconds")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-sync-check", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("sync", help="compare local notebook with kernel source")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("status", help="show kernel run status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("logs", help="print run log")
    sp.add_argument("--tail", type=int, default=10000, help="max lines")
    sp.add_argument("--stderr-only", action="store_true")
    sp.set_defaults(func=cmd_logs)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
