import re
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
_PLACEHOLDER = re.compile(r"<[^>]+>")

EXPECTED_WORKSPACE_ID = "7aeb9e12-8ad0-421d-82ad-262512491e16"
EXPECTED_LAKEHOUSE_ID = "fe90ede0-e379-446e-9e69-1b487dc71946"


def _load_ci_config():
    with open(REPO_ROOT / "ci-config.yml") as f:
        return yaml.safe_load(f)


def _load_profiles():
    with open(REPO_ROOT / "profiles.yml") as f:
        return yaml.safe_load(f)


def test_ci_config_no_placeholders():
    with open(REPO_ROOT / "ci-config.yml") as f:
        content = f.read()
    found = _PLACEHOLDER.findall(content)
    assert not found, f"Placeholders still present: {found}"


def test_ci_config_prod_workspace_id():
    cfg = _load_ci_config()
    assert cfg["prod_workspace_id"] == EXPECTED_WORKSPACE_ID


def test_ci_config_prod_lakehouse_id():
    cfg = _load_ci_config()
    assert cfg["prod_lakehouse_id"] == EXPECTED_LAKEHOUSE_ID


def test_ci_config_domain_and_schema():
    cfg = _load_ci_config()
    assert cfg["domain"] == "sales"
    assert cfg["prod_schema"] == "mrt"


def test_profiles_prod_workspaceid():
    profiles = _load_profiles()
    prod = profiles["dbt_fab_spark"]["outputs"]["prod"]
    assert prod["workspaceid"] == EXPECTED_WORKSPACE_ID
