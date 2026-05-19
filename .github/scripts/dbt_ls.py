"""Shared dbt ls invocation with file-based cache.

run_dbt_ls(cache_path) — invoke `dbt ls --select state:modified+` at most once
per provision run. On first call (cache miss) the subprocess is executed and the
result written to cache_path. On second call (cache hit) the cached list is
returned immediately without spawning a subprocess.

sys.exit(1) on non-zero subprocess returncode.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

try:
    from scripts import runner_io
except ImportError:
    import runner_io

_DBT_LS_CMD = [
    "dbt", "ls",
    "--select", "state:modified+",
    "--resource-type", "model", "snapshot",
    "--state", "./prod-state",
    "--output", "json",
    "--profiles-dir", ".github/profiles",
    "--target", "dbt_fabric_compile",
]


def run_dbt_ls(cache_path: str = "reports/dbt_ls_cache.json") -> list[str]:
    """Return unique_ids from `dbt ls state:modified+`, using a file cache.

    Reads from cache_path if it exists (cache hit — no subprocess).
    Otherwise runs the subprocess, writes the result to cache_path, and returns.
    sys.exit(1) on non-zero subprocess returncode.
    """
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    result = subprocess.run(_DBT_LS_CMD, capture_output=True, text=True)
    if result.returncode != 0:
        runner_io.error(
            f"dbt ls failed (exit {result.returncode}). "
            f"stdout/stderr (first 500 chars): {(result.stdout + result.stderr)[:500]}"
        )
        sys.exit(1)

    unique_ids: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        uid = entry.get("unique_id")
        if uid:
            unique_ids.append(uid)

    cache_dir = os.path.dirname(cache_path)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(unique_ids, f)

    return unique_ids
