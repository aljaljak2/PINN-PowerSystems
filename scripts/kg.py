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
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NB = ROOT / "lospollos.ipynb"
DEFAULT_META = ROOT / "kaggle" / "kernel-metadata.json"
OUTDIR = ROOT / "kaggle" / "output"
BRIDGE_CFG = ROOT / "kaggle" / "bridge.local.json"
sys.path.insert(0, str(ROOT / "scripts"))

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
    without any housekeeping cell. Also resets kernelspec metadata, so a local
    bridge-kernel selection in VS Code never poisons a Kaggle push."""
    cells = []
    for cell in nb["cells"]:
        if HK_TAG in cell.get("metadata", {}).get("tags", []):
            continue
        c = dict(cell)
        if c["cell_type"] == "code":
            c["outputs"] = []
            c["execution_count"] = None
        cells.append(c)
    meta = dict(nb.get("metadata", {}))
    meta.pop("widgets", None)
    meta["kernelspec"] = {"language": "python", "display_name": "Python 3", "name": "python3"}
    return {**nb, "cells": cells, "metadata": meta}


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


# --------------------------------------------------------------------------- bridge

BRIDGE_SLAVE_SUFFIX = "-bridge"


def _bridge_cfg() -> dict:
    if not BRIDGE_CFG.exists():
        sys.exit(
            f"bridge config not found at {BRIDGE_CFG}.\n"
            "Run first:  python3 scripts/kg.py bridge init"
        )
    cfg = json.loads(BRIDGE_CFG.read_text())
    for k in ("repo", "key"):
        if not cfg.get(k):
            sys.exit(f"'{k}' missing in {BRIDGE_CFG}")
    return cfg


def _bridge_kernel_id() -> str:
    meta = _load_meta(DEFAULT_META)
    owner = meta["id"].split("/")[0]
    return f"{owner}/{meta['id'].split('/')[1]}{BRIDGE_SLAVE_SUFFIX}"


def _hf_token(cfg: dict) -> str | None:
    tok = cfg.get("token") or os.environ.get("HF_TOKEN")
    if tok:
        return tok
    p = Path.home() / ".cache" / "huggingface" / "token"
    if p.exists():
        return p.read_text().strip() or None
    return None


def _hf_api(cfg: dict):
    from huggingface_hub import HfApi

    return HfApi(token=_hf_token(cfg))


def _bridge_transport(cfg: dict):
    """The shared transport module (respects KGBRIDGE_FAKE_HF)."""
    import kgbridge_transport

    return kgbridge_transport.Transport(cfg)


def bridge_init(args) -> int:
    import secrets

    repo = args.repo
    if not repo:
        repo = input("HF dataset repo for the bridge (e.g. youruser/lospollos-bridge): ").strip()
    if not repo or "/" not in repo:
        sys.exit("repo must look like 'youruser/repo-name'")

    token = args.token or _hf_token({})
    if not token:
        token = getpass.getpass("HF token (https://huggingface.co/settings/tokens): ").strip()
    if not token:
        sys.exit("a token is required (the bridge repo is private)")

    from huggingface_hub import HfApi

    api = HfApi(token=token)
    who = api.whoami()
    print(f"[kg] token OK — logged in as {who.get('name')}")
    api.create_repo(repo_id=repo, repo_type="dataset", private=True, exist_ok=True)
    print(f"[kg] private dataset repo ready: {repo}")

    key = secrets.token_hex(16)
    BRIDGE_CFG.write_text(json.dumps({"repo": repo, "key": key, "token": token}, indent=2))
    os.chmod(BRIDGE_CFG, 0o600)
    print(f"[kg] config written to {os.path.relpath(BRIDGE_CFG, ROOT)} (gitignored)")

    print(
        "\nNext steps:\n"
        "  1. Kaggle secrets: make sure HF_TOKEN exists (kaggle.com → your notebook → Add-ons → Secrets)\n"
        "  2. python3 scripts/kg.py bridge install   # register the local kernel for VS Code\n"
        "  3. python3 scripts/kg.py bridge start     # start the worker on Kaggle\n"
        "  4. reload VS Code → kernel picker → 'Kaggle Bridge (LosPollos)'"
    )
    return 0


def bridge_install(args) -> int:
    kernel_py = ROOT / "scripts" / "kgbridge_kernel.py"

    candidates = [ROOT / "windfarms" / "bin" / "python3", Path(sys.executable)]
    chosen = None
    for py in candidates:
        if not Path(py).exists():
            continue
        r = subprocess.run(
            [str(py), "-c", "import ipykernel, huggingface_hub"],
            capture_output=True,
        )
        if r.returncode == 0:
            chosen = Path(py)
            break
    if chosen is None:
        sys.exit(
            "no python found with both ipykernel and huggingface_hub installed.\n"
            "Install into your venv:  windfarms/bin/pip install ipykernel huggingface_hub"
        )

    spec_dir = Path.home() / ".local" / "share" / "jupyter" / "kernels" / "kgbridge-lospollos"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "argv": [str(chosen), str(kernel_py), "-f", "{connection_file}"],
        "display_name": "Kaggle Bridge (LosPollos)",
        "language": "python",
        "metadata": {},
        "env": {"KGBRIDGE_CONFIG": str(BRIDGE_CFG)},
    }
    (spec_dir / "kernel.json").write_text(json.dumps(spec, indent=2))
    print(f"[kg] kernelspec installed: {spec_dir / 'kernel.json'}")
    print(f"[kg] using python: {chosen}")
    print("[kg] reload VS Code (Developer: Reload Window) to see 'Kaggle Bridge (LosPollos)' in the kernel picker.")
    return 0


def _worker_notebook(cfg: dict, idle_max_s: int) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    from kg_worker_template import WORKER_TEMPLATE

    worker_code = (
        WORKER_TEMPLATE.replace("@REPO@", cfg["repo"])
        .replace("@KEY@", cfg["key"])
        .replace("@IDLE_MAX@", str(int(idle_max_s)))
        .replace("@EXTRA@", "")
    )
    setup_code = (
        "!pip -q install 'huggingface_hub>=2.0'\n"
        "from kaggle_secrets import UserSecretsClient\n"
        "from huggingface_hub import login as _hf_login\n"
        "_hf_login(UserSecretsClient().get_secret('HF_TOKEN'))\n"
        "print('hf login ok')\n"
    )
    return {
        "cells": [
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [], "source": setup_code},
            {"cell_type": "code", "execution_count": None, "metadata": {},
             "outputs": [], "source": worker_code},
        ],
        "metadata": {"kernelspec": {"language": "python", "display_name": "Python 3", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def bridge_start(args) -> int:
    cfg = _bridge_cfg()
    idle_max_s = args.idle_minutes * 60
    nb = _worker_notebook(cfg, idle_max_s)

    with tempfile.TemporaryDirectory(prefix="kg-bridge-") as tmp:
        tmp = Path(tmp)
        (tmp / "bridge.ipynb").write_text(json.dumps(nb, indent=1))
        meta = {
            "id": _bridge_kernel_id(),
            "title": "LosPollos bridge",
            "code_file": "bridge.ipynb",
            "language": "python",
            "kernel_type": "notebook",
            "is_private": True,
            "enable_gpu": bool(args.gpu),
            "enable_tpu": False,
            "enable_internet": True,
            "dataset_sources": [],
            "kernel_sources": [],
            "competition_sources": [],
            "model_sources": [],
        }
        (tmp / "kernel-metadata.json").write_text(json.dumps(meta, indent=1))
        api = _api()
        print(f"[kg] starting bridge worker on {_bridge_kernel_id()} "
              f"({'GPU' if args.gpu else 'CPU'}, idle timeout {args.idle_minutes} min) ...")
        resp = api.kernels_push(str(tmp), acc=args.acc, no_run=False)
        url = getattr(resp, "url", None) or f"https://www.kaggle.com/code/{_bridge_kernel_id()}"
        print(f"[kg] pushed: {url}")
        print(f"[kg] then:  python3 scripts/kg.py bridge status   (wait for heartbeat)")
    print(f"[kg] in VS Code: pick kernel 'Kaggle Bridge (LosPollos)' and Shift+Enter a cell.")
    return 0


def bridge_remote(args) -> int:
    cfg = _bridge_cfg()
    idle_max_s = args.idle_minutes * 60
    nb = _worker_notebook(cfg, idle_max_s)
    print("# Paste BOTH cells below into a live Kaggle session and run them:\n")
    for i, cell in enumerate(nb["cells"], 1):
        print(f"# ---- cell {i} ----")
        print("".join(cell["source"]))
        print()
    return 0


def bridge_stop(args) -> int:
    cfg = _bridge_cfg()
    t = _bridge_transport(cfg)
    t.upload(f"state/{cfg['key']}/stop.flag", {"ts": time.time()})
    print("[kg] stop flag uploaded — the worker will exit on its next idle poll (≤ ~10s).")
    return 0


def bridge_status(args) -> int:
    cfg = _bridge_cfg()
    t = _bridge_transport(cfg)
    hb = f"state/{cfg['key']}/heartbeat.json"
    if not t.head_ok(hb):
        print("[kg] bridge worker: NOT RUNNING (no heartbeat).")
        print("[kg] start it:  python3 scripts/kg.py bridge start")
        return 1
    raw = t.download(hb)
    ts = json.loads(raw).get("ts", 0) if raw else 0
    age = time.time() - float(ts)
    print(f"[kg] bridge worker: ALIVE (heartbeat {age:.0f}s old)")
    if age > 120:
        print("[kg] WARNING: heartbeat is old — worker may be busy executing a cell or dying.")
    try:
        api = _api()
        resp = api.kernels_status(_bridge_kernel_id())
        status = _norm_status(str(getattr(resp, "status", resp)))
        print(f"[kg] bridge kernel: {status}")
    except Exception as e:
        print(f"[kg] (bridge kernel status unavailable: {e})")
    return 0


def bridge_selftest(args) -> int:
    """Offline end-to-end test: worker loop + kernel protocol against the in-memory
    fake HF transport (KGBRIDGE_FAKE_HF=1). No network, no tokens."""
    os.environ["KGBRIDGE_FAKE_HF"] = "1"
    sys.path.insert(0, str(ROOT / "scripts"))
    import kgbridge_fake

    with tempfile.TemporaryDirectory(prefix="kg-selftest-") as tmp:
        tmp = Path(tmp)
        cfg = {"repo": "fake/repo", "key": "selftestkey", "token": "fake"}
        cfg_path = tmp / "bridge.local.json"
        cfg_path.write_text(json.dumps(cfg))
        os.environ["KGBRIDGE_CONFIG"] = str(cfg_path)
        kgbridge_fake.clear()

        # --- start the worker loop in a thread, as it would run on Kaggle
        from IPython.core.interactiveshell import InteractiveShell

        shell_holder = {}

        def run_worker():
            import sys as _sys
            import types as _types
            import kgbridge_fake as _fake
            from kg_worker_template import WORKER_TEMPLATE

            # swap in the fake huggingface_hub before the worker imports it
            _mod = _types.ModuleType("huggingface_hub")
            _mod.HfApi = _fake.HfApi
            _sys.modules["huggingface_hub"] = _mod

            shell_holder["shell"] = InteractiveShell.instance()
            code = (
                WORKER_TEMPLATE.replace("@REPO@", cfg["repo"])
                .replace("@KEY@", cfg["key"])
                .replace("@IDLE_MAX@", "60")
                .replace("@EXTRA@", "")
            )
            exec(compile(code, "<worker>", "exec"), {"__name__": "__main__"})

        worker = threading.Thread(target=run_worker, daemon=True)
        worker.start()

        # wait for the worker heartbeat (as the local kernel would)
        hb = f"state/{cfg['key']}/heartbeat.json"
        deadline = time.time() + 15
        while not kgbridge_fake.dl(hb):
            if time.time() > deadline:
                sys.exit("selftest: worker never produced a heartbeat")
            time.sleep(0.5)

        # --- drive cells through the real kernel implementation, incl. zmq plumbing
        spec_dir = tmp / "specs" / "kgbridge-selftest"
        spec_dir.mkdir(parents=True)
        kernel_py = ROOT / "windfarms" / "bin" / "python3"
        if not kernel_py.exists():
            kernel_py = Path(sys.executable)
        (spec_dir / "kernel.json").write_text(json.dumps({
            "argv": [str(kernel_py), str(ROOT / "scripts" / "kgbridge_kernel.py"), "-f", "{connection_file}"],
            "display_name": "kgbridge-selftest",
            "language": "python",
            "env": {"KGBRIDGE_FAKE_HF": "1", "KGBRIDGE_CONFIG": str(cfg_path),
                    "PYTHONPATH": str(ROOT / "scripts")},
        }))

        from jupyter_client.manager import KernelManager

        km = KernelManager(kernel_name="kgbridge-selftest")
        km.kernel_spec_manager.kernel_dirs = [str(tmp / "specs")] + km.kernel_spec_manager.kernel_dirs
        km.start_kernel()
        kc = km.client()
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=60)

            outputs = []

            def hook(msg):
                t = msg["msg_type"]
                if t in ("stream", "display_data", "error"):
                    c = msg["content"]
                    if t == "stream":
                        outputs.append(("stream", c.get("name"), c.get("text")))
                    elif t == "error":
                        outputs.append(("error", c.get("ename"), "".join(c.get("traceback", []))))
                    else:
                        outputs.append(("display", list(c.get("data", {}).keys()), c.get("data")))

            def run(code):
                outputs.clear()
                kc.execute_interactive(code, output_hook=hook, allow_stdin=False, timeout=90)

            run("print('hello from', 'bridge'); 6*7")
            got = [o for o in outputs]
            assert any(o[0] == "stream" and "hello from bridge" in (o[2] or "") for o in got), f"stream missing: {got}"
            assert any(o[0] == "display" and "text/plain" in o[1] for o in got), f"execute_result missing: {got}"

            run("import pandas as pd; pd.DataFrame({'a':[1,2]}).head()")
            assert any(o[0] == "display" and "text/html" in o[1] for o in outputs), "rich html missing"

            run("x = 41")  # statefulness across cells
            run("x + 1")
            assert any(o[0] == "display" and "42" in str(o[2].get("text/plain", "")) for o in outputs), "state lost"

            run("raise ValueError('boom')")
            assert any(o[0] == "error" and o[1] == "ValueError" for o in outputs), "error message missing"

            print("[kg] SELFTEST PASSED ✓  (stream, rich html, statefulness, errors, zmq plumbing)")
        finally:
            kc.stop_channels()
            km.shutdown_kernel(now=True)
            # tell the worker to stop and give it a moment
            try:
                kgbridge_fake.up(f"state/{cfg['key']}/stop.flag", json.dumps({"ts": time.time()}).encode())
                worker.join(timeout=15)
            except Exception:
                pass
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

    br = sub.add_parser("bridge", help="interactive cell-by-cell execution on Kaggle")
    bsub = br.add_subparsers(dest="bridge_cmd", required=True)

    sp = bsub.add_parser("init", help="configure HF repo + session key (one-time)")
    sp.add_argument("--repo", help="HF dataset repo, e.g. youruser/lospollos-bridge")
    sp.add_argument("--token", help="HF token (else: $HF_TOKEN or prompt)")
    sp.set_defaults(func=bridge_init)

    sp = bsub.add_parser("install", help="register the local kernel spec for VS Code")
    sp.set_defaults(func=bridge_install)

    sp = bsub.add_parser("start", help="push & start the worker on Kaggle (headless)")
    sp.add_argument("--gpu", action="store_true", help="run worker on GPU (else CPU)")
    sp.add_argument("--acc", help="accelerator override, e.g. NvidiaTeslaT4")
    sp.add_argument("--idle-minutes", type=int, default=30, help="exit after this many idle minutes (default 30)")
    sp.set_defaults(func=bridge_start)

    sp = bsub.add_parser("remote", help="print worker cells to paste into a live Kaggle session")
    sp.add_argument("--idle-minutes", type=int, default=120, help="idle timeout for the pasted worker")
    sp.set_defaults(func=bridge_remote)

    sp = bsub.add_parser("stop", help="stop the worker (frees the session)")
    sp.set_defaults(func=bridge_stop)

    sp = bsub.add_parser("status", help="worker heartbeat + kernel status")
    sp.set_defaults(func=bridge_status)

    sp = bsub.add_parser("selftest", help="offline end-to-end test (fake transport)")
    sp.set_defaults(func=bridge_selftest)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
