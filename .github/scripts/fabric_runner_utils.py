"""Shared pure helpers for Fabric GHA gate runner scripts (AC-11).

Extracted here to eliminate duplication across gate2/3/4/5_fabric_runner.py.
Each function is stateless and takes only pre-fetched scalar inputs.
"""
from __future__ import annotations

import json
import pathlib
import shutil


def select_names(deployment_manifest_path: str) -> list[str]:
    """Extract leaf model names from the deployment manifest, excluding ephemeral."""
    try:
        with open(deployment_manifest_path) as f:
            dm = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [
        (a.get("name", "").split(".")[-1] or a.get("name", ""))
        for a in dm.get("artifacts") or []
        if a.get("name") and a.get("materialized") != "ephemeral"
    ]


def setup_defer(prod_state_dir: str) -> list[str]:
    """Copy manifest_prod.json to target/prod-state-defer/manifest.json.

    Returns --defer --state args if the source file exists, otherwise [].
    Used by Gates 2 and 4 for cross-workspace ref resolution.
    """
    src = pathlib.Path(prod_state_dir) / "manifest_prod.json"
    if not src.exists():
        return []
    dest_dir = pathlib.Path("target/prod-state-defer")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest_dir / "manifest.json")
    return ["--defer", "--state", str(dest_dir)]


def write_gate_result(path: str | None, result: dict) -> None:
    """Write gate result JSON to the output file, creating parent dirs as needed."""
    if not path:
        return
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
