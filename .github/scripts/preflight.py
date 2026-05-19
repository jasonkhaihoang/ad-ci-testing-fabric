"""Preflight validation: intent-slug + ci-config parse.

CLI:
    preflight.py --branch-name <name> --behind-by <n>
                 --ci-config-path <path> --output <path>
                 --github-output <path>
"""

import argparse
import json
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    yaml = None

INTENT_SLUG_RE = re.compile(r"^intent/[a-z0-9][a-z0-9\-]+$")

REQUIRED_KEYS = [
    "domain",
    "prod_workspace_id",
    "prod_workspace_name",
    "prod_lakehouse_id",
    "prod_lakehouse_name",
    "prod_schema",
]


def validate_intent_slug(branch_name: str) -> tuple[bool, str | None]:
    """Return (valid, branch_name_if_valid_else_None)."""
    if INTENT_SLUG_RE.match(branch_name):
        return True, branch_name
    return False, None


def parse_ci_config(yaml_str: str) -> dict:
    """Parse ci-config.yml content.

    Returns:
        {
            "ok": bool,
            "config": dict,
            "error": str | None,
            "line_number": int | None,
            "missing_keys": list[str],
        }
    """
    if yaml is None:
        return {"ok": False, "config": {}, "error": "pyyaml not installed", "line_number": None, "missing_keys": []}

    try:
        config = yaml.safe_load(yaml_str)
    except yaml.YAMLError as exc:
        line_number = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line_number = exc.problem_mark.line + 1
        return {"ok": False, "config": {}, "error": str(exc), "line_number": line_number, "missing_keys": []}

    if config is None:
        config = {}

    if not isinstance(config, dict):
        return {
            "ok": False,
            "config": {},
            "error": "ci-config.yml must be a YAML mapping (key: value pairs), not a list or scalar",
            "line_number": None,
            "missing_keys": [],
        }

    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        return {
            "ok": False,
            "config": config,
            "error": f"Missing required keys: {missing}",
            "line_number": None,
            "missing_keys": missing,
        }

    return {"ok": True, "config": config, "error": None, "line_number": None, "missing_keys": []}


_AUTO_MERGE_VIOLATION_MESSAGE = (
    "Per-PR auto-merge is enabled. Disable the auto-merge toggle on this PR — leaving it "
    "on lets GitHub merge ahead of `domain-deploy` (once it ships) and bypasses the deploy lock."
)


def check_auto_merge_disabled(auto_merge_value) -> dict:
    """Return {passed, message} for the auto_merge_disabled preflight check.

    Null, missing (None), or empty dict → disabled → passed.
    Any non-empty object → enabled → failed.
    """
    enabled = bool(auto_merge_value)
    if enabled:
        return {"passed": False, "message": _AUTO_MERGE_VIOLATION_MESSAGE}
    return {"passed": True, "message": "Per-PR auto-merge is disabled."}


def build_preflight_result(
    branch_name: str,
    yaml_str: str,
    behind_by: int = 0,
    auto_merge=None,
) -> dict:
    """Build the full preflight result dict.

    ci_config is skipped when intent validation fails.
    behind_by > 0 is informational — does not fail preflight.
    auto_merge: raw value from pulls/{pr}.auto_merge API field.
                None / missing / {} → disabled (passes).
                Non-empty object → enabled (fails).
    """
    intent_valid, slug = validate_intent_slug(branch_name)

    auto_rebase = {
        "status": "ok" if behind_by == 0 else "behind",
        "behind_by": behind_by,
        "message": (
            "Branch is up-to-date with main."
            if behind_by == 0
            else f"Branch is {behind_by} commit(s) behind main — rebase manually before merge."
        ),
    }

    intent = {
        "status": "pass" if intent_valid else "fail",
        "slug": slug,
        "message": (
            f"Valid intent slug: `{slug}`"
            if intent_valid
            else (
                f"Invalid branch name: `{branch_name}` — must match "
                r"`intent/<slug>` where `<slug>` matches `[a-z0-9][a-z0-9\-]+`"
            )
        ),
    }

    auto_merge_check = check_auto_merge_disabled(auto_merge)

    if not intent_valid:
        return {
            "overall_status": "fail",
            "auto_rebase": auto_rebase,
            "intent": intent,
            "ci_config": {
                "status": "skipped",
                "message": "intent validation failed",
                "line_number": None,
                "missing_keys": [],
            },
            "auto_merge_disabled": auto_merge_check,
        }

    ci_raw = parse_ci_config(yaml_str)
    ci_config = {
        "status": "pass" if ci_raw["ok"] else "fail",
        "message": ci_raw["error"] or "Parsed successfully.",
        "line_number": ci_raw["line_number"],
        "missing_keys": ci_raw["missing_keys"],
    }

    overall_status = "pass" if (ci_raw["ok"] and auto_merge_check["passed"]) else "fail"

    return {
        "overall_status": overall_status,
        "auto_rebase": auto_rebase,
        "intent": intent,
        "ci_config": ci_config,
        "auto_merge_disabled": auto_merge_check,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight validation for domain CI.")
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--behind-by", type=int, default=0)
    parser.add_argument("--ci-config-path", default="ci-config.yml")
    parser.add_argument("--output", default="reports/preflight.json")
    parser.add_argument("--github-output", default="")
    args = parser.parse_args()

    yaml_str = ""
    try:
        with open(args.ci_config_path) as f:
            yaml_str = f.read()
    except FileNotFoundError:
        pass

    # Fetch per-PR auto_merge field (read-only, uses existing GITHUB_TOKEN)
    auto_merge = None
    pr_number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if pr_number and repo:
        try:
            raw = subprocess.check_output(
                ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".auto_merge"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).strip()
            # gh --jq returns "null" string for JSON null, or a JSON object string
            if raw and raw != "null":
                auto_merge = json.loads(raw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError):
            pass

    result = build_preflight_result(args.branch_name, yaml_str, args.behind_by, auto_merge=auto_merge)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    if result["overall_status"] == "pass" and args.github_output:
        ci_raw = parse_ci_config(yaml_str)
        with open(args.github_output, "a") as out:
            for k, v in ci_raw["config"].items():
                out.write(f"{k}={v}\n")
            slug = result["intent"]["slug"]
            out.write(f"intent_slug={slug}\n")
        print(f"Config outputs written. intent_slug={slug}")

    if result["overall_status"] != "pass":
        am_enabled = not result["auto_merge_disabled"]["passed"]
        print(f"Preflight failed: intent={result['intent']['status']}, ci_config={result['ci_config']['status']}, auto_merge_enabled={am_enabled}")
        sys.exit(1)

    print("Preflight passed.")


if __name__ == "__main__":
    main()
