"""Pure helpers for ci-config.yml loading and intent-slug validation.

Extracted from preflight.py so these functions are unit-testable in isolation
and reusable without going through the CLI shell.
"""
import re

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
