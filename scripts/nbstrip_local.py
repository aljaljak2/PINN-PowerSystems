#!/usr/bin/env python3
"""Pre-commit hook: strip cell outputs and execution counts from notebooks.

Keeps the committed notebook clean so 4 collaborators can diff/merge it.
stdlib-only (no network, no dependencies) — replaces the nbstripout remote hook,
which cannot be cloned when git rewrites github https→ssh URLs.
Also drops any kg-housekeeping cell: those exist only inside Kaggle runs.
"""
import json
import sys

HK_TAG = "kg-housekeeping"


def strip(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        nb = json.load(f)

    changed = False
    cells = []
    for cell in nb.get("cells", []):
        if HK_TAG in cell.get("metadata", {}).get("tags", []):
            changed = True
            continue
        if cell.get("cell_type") == "code":
            if cell.get("outputs"):
                cell["outputs"] = []
                changed = True
            if cell.get("execution_count") is not None:
                cell["execution_count"] = None
                changed = True
        cells.append(cell)
    nb["cells"] = cells

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1)
        print(f"stripped outputs: {path}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        strip(p)
