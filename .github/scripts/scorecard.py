"""
dbt doc and test coverage scorecard.

Reads target/manifest.json (from dbt parse) and optionally AGENTS.md
to produce a JSON scorecard with:
  - description_coverage: % of models with descriptions
  - column_coverage: % of columns with descriptions
  - pk_test_coverage: % of PKs with not_null + unique tests
  - naming_violations: list of models violating AGENTS.md conventions

Only the project's own models are evaluated (package models are excluded).
Output is written to stdout as JSON.
"""

import argparse
import json
import os
import re
import sys


def load_manifest(path: str) -> dict:
    if not os.path.exists(path):
        print(f"Warning: manifest not found at {path}. Returning empty scorecard.", file=sys.stderr)
        return {}
    with open(path) as f:
        return json.load(f)


def load_naming_rules(agents_md_path: str) -> list[str]:
    """Extract naming convention patterns from AGENTS.md if present."""
    if not os.path.exists(agents_md_path):
        return []
    with open(agents_md_path) as f:
        content = f.read()
    patterns = re.findall(r"`(stg_\{[^`]+\}|fct_\{[^`]+\}|dim_\{[^`]+\})`", content)
    return patterns


def check_naming(model_name: str) -> bool:
    """Return True if model name follows known medallion patterns."""
    valid_prefixes = ("stg_", "fct_", "dim_", "int_", "mart_", "src_", "elementary")
    return any(model_name.startswith(p) for p in valid_prefixes)


def _build_pk_test_index(nodes: dict) -> dict[str, set[str]]:
    """Return {(model_unique_id, column_name): {test_names}} for _id columns.

    dbt 1.8+ stores tests as separate nodes; the model node's data_tests field
    is no longer populated. This index is used as the authoritative test source.
    """
    index: dict[str, set[str]] = {}
    pk_pattern = re.compile(r"_id$", re.IGNORECASE)
    for v in nodes.values():
        if v.get("resource_type") != "test":
            continue
        attached = v.get("attached_node")
        col = v.get("column_name") or ""
        test_name = (v.get("test_metadata") or {}).get("name", "")
        if not attached or not test_name or not pk_pattern.search(col):
            continue
        key = (attached, col)
        index.setdefault(key, set()).add(test_name)
    return index


def scorecard(manifest: dict, agents_md_path: str) -> dict:
    nodes = manifest.get("nodes", {})
    project_name = manifest.get("metadata", {}).get("project_name")

    # Only evaluate the project's own models, not installed package models.
    def _is_own_model(v: dict) -> bool:
        if v.get("resource_type") != "model":
            return False
        if project_name:
            return v.get("package_name") == project_name
        return True

    models = {k: v for k, v in nodes.items() if _is_own_model(v)}

    if not models:
        return {
            "model_count": 0,
            "description_coverage_pct": 0,
            "column_coverage_pct": 0,
            "pk_test_coverage_pct": 0,
            "naming_violations": [],
            "naming_violation_count": 0,
            "summary": "No models found in manifest.",
            "passed": False,
        }

    pk_test_index = _build_pk_test_index(nodes)

    # Description coverage
    models_with_desc = sum(1 for m in models.values() if m.get("description", "").strip())
    desc_pct = round(100 * models_with_desc / len(models), 1)

    # Column description coverage
    total_cols = 0
    cols_with_desc = 0
    for m in models.values():
        for col in m.get("columns", {}).values():
            total_cols += 1
            if col.get("description", "").strip():
                cols_with_desc += 1
    col_pct = round(100 * cols_with_desc / total_cols, 1) if total_cols else 0

    # PK test coverage: each *_id column must have both not_null and unique tests.
    # Check both the inline data_tests field (dbt <1.8) and the test-node index (dbt 1.8+).
    pk_pattern = re.compile(r"_id$")
    pks_found = 0
    pks_covered = 0
    for model_uid, m in models.items():
        for col_name, col in m.get("columns", {}).items():
            if not pk_pattern.search(col_name):
                continue
            pks_found += 1
            # Try inline field first (dbt <1.8), then test-node index (dbt 1.8+)
            inline = [t.get("name", "") if isinstance(t, dict) else str(t)
                      for t in col.get("data_tests", col.get("tests", []))]
            test_names = set(inline) | pk_test_index.get((model_uid, col_name), set())
            if "not_null" in test_names and "unique" in test_names:
                pks_covered += 1
    pk_pct = round(100 * pks_covered / pks_found, 1) if pks_found else 100.0

    # Naming violations
    violations = []
    for node_id, m in models.items():
        name = m.get("name", "")
        if not check_naming(name):
            violations.append({
                "model": name,
                "path": m.get("original_file_path", ""),
                "issue": f"Name '{name}' does not match expected prefix (stg_/fct_/dim_/int_)",
            })

    passed = (
        desc_pct >= 80
        and col_pct >= 80
        and pk_pct >= 80
        and len(violations) == 0
    )

    return {
        "model_count": len(models),
        "models_with_description": models_with_desc,
        "description_coverage_pct": desc_pct,
        "total_columns": total_cols,
        "columns_with_description": cols_with_desc,
        "column_coverage_pct": col_pct,
        "pks_found": pks_found,
        "pks_covered": pks_covered,
        "pk_test_coverage_pct": pk_pct,
        "naming_violations": violations,
        "naming_violation_count": len(violations),
        "passed": passed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="target/manifest.json")
    parser.add_argument("--agents-md", default="AGENTS.md")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    result = scorecard(manifest, args.agents_md)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
