# LosPollos

One notebook (`lospollos.ipynb`), executed on Kaggle's GPUs, edited in VS Code,
kept in git by 4 collaborators. This repo contains the notebook plus the tooling
that syncs it with the shared Kaggle kernel.

```
                         ┌──────────────────────────────┐
   edit cells locally    │  lospollos.ipynb (git)       │
                         └──────────────┬───────────────┘
              kg run (Ctrl+Shift+B)     │ push (stripped + housekeeping cell)
                                        ▼
                         ┌──────────────────────────────┐
                         │  Kaggle kernel:              │
                         │  tarikhastor/lospollos       │
                         │  GPU T4 · internet enabled   │
                         └──────────────┬───────────────┘
                     run completes      │ pull (outputs merged back inline)
                                        ▼
                         ┌──────────────────────────────┐
                         │  results visible in VS Code  │
                         │  commit = code only          │
                         └──────────────────────────────┘
```

- **Data** comes from a public HuggingFace dataset over HTTPS — no Kaggle
  dataset uploads, no API keys for the data itself. (The notebook uses
  Kaggle's `UserSecretsClient` for an optional `HF_TOKEN`.)
- **Git never contains notebook outputs.** A pre-commit hook strips them, so
  the notebook always diffs cleanly between collaborators.
- **Everyone runs their own copies of the runs**, but against the same shared
  kernel — see [Team etiquette](#team-etiquette).

---

## One-time setup (fresh machine)

### 1. Prerequisites

- `git`
- `python3` (3.10+) and `pip`
- VS Code / Code OSS with the **Python** and **Jupyter** extensions
  (open the repo once and accept the "install recommended extensions" prompt,
  or see `.vscode/extensions.json`)

### 2. Clone and bootstrap

```bash
git clone git@github.com:lilhast1/LosPollosWind.git
cd LosPollosWind
./setup.sh
```

`setup.sh` installs the `kaggle` python module and `pre-commit`, enables the
git hook that strips notebook outputs, and verifies you can reach the kernel.

If `pip` refuses to install (PEP 668 "externally managed environment"), use:

```bash
python3 -m pip install --user --break-system-packages "kaggle>=2.2.5"
# or, if you use uv:  uv tool install pre-commit && uv pip install --system "kaggle>=2.2.5"
```

### 3. Kaggle credentials

1. Log in at <https://www.kaggle.com>
2. Avatar → **Settings** → **API** → **Create New Token**
3. Save the downloaded `kaggle.json` to `~/.kaggle/kaggle.json` and lock it down:

```bash
mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/ && chmod 600 ~/.kaggle/kaggle.json
```

(If your setup uses the newer token style, `~/.kaggle/access_token` +
`credentials.json` work too.) **Never commit this file** — it's already
gitignored by convention; keep it that way.

### 4. Get access to the shared kernel

The kernel owner adds every collaborator manually:

> <https://www.kaggle.com/code/tarikhastor/lospollos> → **⋮ menu** → **Sharing** →
> add each collaborator's Kaggle username as a collaborator.

Check that it worked:

```bash
python3 scripts/kg.py status     # should print: ... has status COMPLETE/RUNNING/...
```

### 5. Optional: local quick-test interpreter

For cheap edits (typos, small logic fixes) you can run single cells locally
before spending GPU minutes. VS Code is preconfigured to use the repo-local
venv `windfarms/bin/python3` as the notebook interpreter — select it in the
kernel picker, or just use any Python that has the packages you need.
Remember: the notebook is written for Kaggle paths (`/kaggle/working/...`)
and `UserSecretsClient`, so most cells **only run correctly on Kaggle**.

---

## Daily workflow

### The main loop

| Step | Action |
|---|---|
| 1. Edit | Open `lospollos.ipynb`, write cells. Local execution is optional and only for quick sanity checks. |
| 2. Sync check | `Ctrl+Shift+B` does this automatically, but for courtesy run the **Kaggle: Sync Check** task first. |
| 3. Push & run | **`Ctrl+Shift+B`** (task *Kaggle: Push & Run & Pull*). Pushes your notebook, runs it on the T4, waits, and merges all cell outputs back inline into the local notebook. |
| 4. Review | Reload the notebook in VS Code — outputs (tables, plots, prints) appear inline. Artifacts land in `kaggle/output/`. |
| 5. Commit | `git add lospollos.ipynb && git commit` — the hook strips outputs, so you commit code only. |

Equivalent terminal commands (see the full list with `python3 scripts/kg.py --help`):

```bash
python3 scripts/kg.py run        # push → watch → pull (the whole loop)
python3 scripts/kg.py push       # just push and start the run
python3 scripts/kg.py watch      # follow an already-started run
python3 scripts/kg.py pull       # fetch results of the last run + merge outputs
python3 scripts/kg.py status     # current run status
python3 scripts/kg.py logs       # print the run log
python3 scripts/kg.py sync       # compare local notebook vs kernel source
```

Useful variants:

```bash
kg push --no-run                 # save version without executing ("Quick Save")
kg push --acc NvidiaTeslaP100    # override accelerator for one push
kg run --force                   # push even though the kernel source drifted
```

> **Note:** the very first `pull` on any given run may need a small delay after
> the run completes — artifacts appear as soon as Kaggle finalizes them.

### Interactive bridge: exec one cell at a time on Kaggle

`kg run` is *headless and stateless* — every push re-executes the whole
notebook. For real development you want to edit one cell and execute just
that cell, with **variables persisting between cells**. The bridge gives you
exactly that: VS Code sees a normal kernel called **Kaggle Bridge (LosPollos)**
that actually lives on Kaggle.

**One-time setup:**

```bash
python3 scripts/kg.py bridge init       # asks for an HF token, creates a private HF dataset repo
python3 scripts/kg.py bridge install    # registers the local kernel (already done by setup.sh)
python3 scripts/kg.py bridge selftest   # optional: offline end-to-end check
```

`bridge init` needs a Hugging Face token (<https://huggingface.co/settings/tokens>,
"read" is enough) because the bridge queues cells/results through a **private**
HF dataset repo. The same token must exist as a Kaggle secret named `HF_TOKEN`
(Add-ons → Secrets) — the notebook already uses one.

**Every work session:**

```bash
python3 scripts/kg.py bridge start      # pushes & starts the worker (CPU, idle-exits after 30 min)
python3 scripts/kg.py bridge status     # check the heartbeat
python3 scripts/kg.py bridge stop       # when done — frees the Kaggle session
```

Then in VS Code: kernel picker → **Kaggle Bridge (LosPollos)** → Shift+Enter
runs the cell *on Kaggle* and shows outputs inline. Variables, imports and
trained models persist across cells, so you can iterate on one cell without
re-running the world.

**How it works:** the worker is a loop running inside a Kaggle kernel that
polls the private HF repo for queued cells, executes them in the live kernel's
namespace (full IPython semantics: magics, `display()`, last-expression
results, tracebacks), and writes outputs back. Heartbeats keep the session
alive and let the local kernel detect a dead worker.

**Caveats (read once):**

- The bridge run occupies a Kaggle session — **CPU by default**. Start it on
  GPU (`bridge start --gpu`) only while you're interactively training.
- The worker idle-exits after 30 min without cells (configurable with
  `--idle-minutes`); the local kernel then tells you to restart it.
- One bridge at a time; cells run to completion — a hung cell needs
  `bridge stop` + `bridge start`.
- ipywidgets and interrupts are not supported yet; very long-running cells
  return only when finished (no live streaming mid-cell).
- Alternative mode: `python3 scripts/kg.py bridge remote` prints the worker
  cells to paste into a live session in the Kaggle web editor — useful when
  you want the bridge inside a browser session instead of a pushed run.

### Team etiquette

All four collaborators push to **one shared kernel**. Kaggle pushes are
last-write-wins, so:

1. **Always `kg sync` before `kg push`.** It compares your local notebook with
   what is currently on the kernel. `kg push` refuses to run if the kernel
   drifted (someone else pushed) unless you pass `--force`.
2. **Pull others' work from git, not from Kaggle.** The repo is the source of
   truth for *code*; the kernel is just the execution engine.
3. If two of you genuinely need to run conflicting versions simultaneously,
   coordinate — or temporarily push with `--no-run` and run one at a time.
4. Long runs are queued by Kaggle; check `kg status` before pushing if
   something is already running.

### Where results live

| Location | Content | Committed? |
|---|---|---|
| `lospollos.ipynb` (working copy) | code + latest pulled outputs | no (outputs stripped on commit) |
| `kaggle/output/` | run log + any artifact files | no (gitignored) |
| kaggle.com/code/tarikhastor/lospollos | all versions, full logs, rendered output | yes (on Kaggle's side) |

---

## How it works under the hood

You don't need this to use the tooling, but it explains the "magic":

1. **Push:** `kg.py` strips all outputs, appends a hidden *housekeeping* cell
   (tagged `kg-housekeeping`) that copies Kaggle's own
   `/kaggle/working/__notebook__.ipynb` — the executed notebook *with cell
   outputs* — to `/kaggle/working/executed_lospollos.ipynb`, and pushes.
2. **Run:** Kaggle executes the notebook headless. The copy becomes a
   downloadable output artifact.
3. **Pull:** `kg.py` downloads the output bundle, takes
   `executed_lospollos.ipynb`, and merges each cell's outputs back into your
   local notebook (matched positionally, with a source-equality check).
4. The housekeeping cell never exists in your repo copy — it is appended on
   push and ignored on pull, so `kg sync` always stays clean.

Because of this, results appear **inline in VS Code** without Kaggle exposing
any official API for executed notebook outputs.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pip` errors with *externally managed environment* | `pip install --user --break-system-packages ...` or use `uv` (see setup) |
| `git push` says *Permission denied (publickey)* | Add your SSH key (`ssh-keygen` → upload to GitHub), or for this repo only: `git config --local url."https://github.com/".insteadOf "ssh://git@github.com/"` |
| pre-commit hook fails to clone a remote repo | This project uses a **local** hook on purpose — if you see clone errors, you replaced the config; restore `.pre-commit-config.yaml` |
| `pull`: *no executed notebook artifact* | The run predates the tooling or the housekeeping cell failed. Check `kg logs`; results are still on kaggle.com |
| `pull`: *cell count mismatch* | The notebook changed between push and pull (two pushes interleaved). `kg sync`, take the newest code, re-run |
| `push` refuses with drift warning | Someone pushed meanwhile: `git pull` first, then re-run `kg sync` to inspect; override with `--force` only if you are sure |
| Run fails on Kaggle | `kg logs` (stderr is what matters), or open the kernel page → output tab |
| Kernel status stuck `QUEUED` | Kaggle GPU queue or weekly GPU quota exhausted (check kaggle.com → profile → Settings) |
| Notebook refuses to run locally (`UserSecretsClient`, `/kaggle/working`) | Expected — the notebook targets Kaggle. Use Kaggle for execution |
| Code OSS kernel picker shows no Python | Make sure the Python + Jupyter extensions are active; if you're on Arch's `code` package with a recent version, the API-proposals whitelist patch may need re-applying after an update |

---

## File map

```
lospollos.ipynb              the notebook (code + your local outputs)
kaggle/kernel-metadata.json  shared kernel definition (committed)
kaggle/bridge.local.json     bridge config: HF repo, session key, token (gitignored)
kaggle/output/               pulled logs & artifacts (gitignored)
scripts/kg.py                the sync CLI (+ bridge subcommands)
scripts/kgbridge_kernel.py   local Jupyter kernel: "Kaggle Bridge (LosPollos)"
scripts/kgbridge_transport.py shared HF transport for kernel & worker
scripts/kg_worker_template.py the worker-cell template
scripts/kgbridge_fake.py     offline fake transport for `bridge selftest`
scripts/nbstrip_local.py     pre-commit hook: strip outputs (stdlib-only)
setup.sh                     collaborator bootstrap
.vscode/tasks.json           Ctrl+Shift+B etc. (committed)
.gitignore                   keeps outputs, configs and secrets out of git
```
