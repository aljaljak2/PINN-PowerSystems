#!/usr/bin/env bash
# One-time bootstrap for LosPollos collaborators.
# Usage: ./setup.sh
set -euo pipefail

echo "=== LosPollos setup ==="

# 1. python + pip
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH"; exit 1
fi

# 2. tools: kaggle python module (>=2.2.5, used by scripts/kg.py) and pre-commit
echo "[setup] installing kaggle module and pre-commit ..."
if ! python3 -c "import kaggle" >/dev/null 2>&1; then
    python3 -m pip install --user --quiet "kaggle>=2.2.5" 2>/dev/null \
        || python3 -m pip install --user --quiet --break-system-packages "kaggle>=2.2.5" \
        || { echo "ERROR: could not pip-install kaggle (externally managed python?)."; \
             echo "  e.g. with uv:  uv pip install --system 'kaggle>=2.2.5'"; exit 1; }
fi
if command -v uv >/dev/null 2>&1; then
    uv tool install --quiet pre-commit || true
elif ! command -v pre-commit >/dev/null 2>&1; then
    python3 -m pip install --user --quiet pre-commit 2>/dev/null \
        || python3 -m pip install --user --quiet --break-system-packages pre-commit || true
fi
export PATH="$HOME/.local/bin:$PATH"
command -v pre-commit >/dev/null 2>&1 || { echo "ERROR: pre-commit not available"; exit 1; }

# 3. kaggle credentials
if [ -f "$HOME/.kaggle/kaggle.json" ] || [ -f "$HOME/.kaggle/access_token" ]; then
    echo "[setup] kaggle credentials found."
else
    echo "WARNING: no Kaggle credentials."
    echo "  1. Log in at https://www.kaggle.com → avatar → Settings → API → 'Create New Token'"
    echo "  2. Save the downloaded kaggle.json to ~/.kaggle/kaggle.json (chmod 600)"
    echo "  3. Re-run ./setup.sh"
fi

# 4. git hooks (strips notebook outputs on commit)
if [ -d .git ]; then
    pre-commit install
    echo "[setup] pre-commit installed (notebook outputs are stripped automatically on commit)."
fi

# 5. local bridge kernel (cell-by-cell execution on Kaggle)
python3 scripts/kg.py bridge install || echo "(bridge install skipped — run it after 'kg.py bridge init')"

# 6. connectivity + access check
echo "[setup] checking kernel access ..."
if python3 scripts/kg.py status; then
    echo "[setup] OK — you can access the shared kernel."
else
    echo "NOTE: could not access the kernel yet."
    echo "  The kernel owner must add you as a collaborator:"
    echo "  kaggle.com/code/tarikhastor/lospollos → ⋮ menu → 'Sharing' → add your Kaggle username."
fi

echo
echo "=== Daily loop (or use the VS Code tasks: Ctrl+Shift+B) ==="
echo "  python3 scripts/kg.py run     # push → run on Kaggle → merge results back"
echo "  python3 scripts/kg.py sync    # check you wouldn't clobber a collaborator"
echo "  python3 scripts/kg.py pull    # re-download results of the last run"
