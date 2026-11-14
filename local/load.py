import os, sys, glob, time
import numpy as np, pandas as pd
from huggingface_hub import HfApi, login, snapshot_download, hf_hub_download

api = HfApi()

REPO = "B4BUK4/MPVI_project"
W = "./kaggle/working"
TS = "Date and time"
rated = 2050

print("Downloading codebase, flags, layouts, and training tables from HF...")
t0 = time.time()

snapshot_download(
    REPO,
    repo_type="dataset",
    allow_patterns=["code/*", "parquet/flags/*", "parquet/train/*"],
    local_dir=W
)

for fn in ["penmanshiel_flags.parquet", "kelmarsh_flags.parquet", "MASK_SPEC.txt"]:
    hf_hub_download(REPO, f"parquet/flags/{fn}", repo_type="dataset", local_dir=W, force_download=True)

os.makedirs(f"{W}/figs", exist_ok=True)
os.makedirs(f"{W}/hf", exist_ok=True)

sys.path.insert(0, f"{W}/code")


print(f"Restore finished in {time.time() - t0:.1f}s.\n")

print("=" * 60)
print("ENVIRONMENT & DATA INTEGRITY VERIFICATION")
print("=" * 60)

for farm in ["penmanshiel", "kelmarsh"]:
    fl = pd.read_parquet(f"{W}/parquet/flags/{farm}_flags.parquet", columns=["mask_m1", "sensor_drift"])
    print(f"[{farm.upper()} FLAGS] Clean share: {(~fl.mask_m1).mean():.4f} (target 0.9186/0.9489) | Drift share: {fl.sensor_drift.mean():.4f}")

print("-" * 60)

for farm in ["penmanshiel", "kelmarsh"]:
    p = f"{W}/parquet/train/{farm}_m1_clean.parquet"
    if os.path.exists(p):
        df = pd.read_parquet(p, columns=[TS, "turbine", "ws", "power_kw", "is_test"])
        n_train = (~df["is_test"]).sum()
        n_test = df["is_test"].sum()
        nulls = df.isna().sum().sum()
        print(f"[{farm.upper()} M1 DATASET] Total: {len(df):,} | Train: {n_train:,} | Test: {n_test:,} | Nulls: {nulls}")
    else:
        print(f"[{farm.upper()}] Warning: training table not found locally!")