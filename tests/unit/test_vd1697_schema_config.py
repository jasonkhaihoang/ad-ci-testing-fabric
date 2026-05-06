import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _load_dbt_project():
    with open(REPO_ROOT / "dbt_project.yml") as f:
        return yaml.safe_load(f)


def _load_sources():
    with open(REPO_ROOT / "models/staging/salescloud/__salescloud_sources.yml") as f:
        return yaml.safe_load(f)


def test_staging_schema_is_stg():
    proj = _load_dbt_project()
    assert proj["models"]["sales_pipeline"]["staging"]["+schema"] == "stg"


def test_marts_schema_is_mrt():
    proj = _load_dbt_project()
    assert proj["models"]["sales_pipeline"]["marts"]["+schema"] == "mrt"


def test_seed_raw_schema_is_raw():
    proj = _load_dbt_project()
    assert proj["seeds"]["sales_pipeline"]["raw"]["+schema"] == "raw"


def test_seed_quote_columns_false():
    proj = _load_dbt_project()
    assert proj["seeds"]["sales_pipeline"]["raw"]["+quote_columns"] is False


def test_salescloud_source_schema_is_raw():
    sources = _load_sources()
    salescloud = next(s for s in sources["sources"] if s["name"] == "salescloud")
    assert salescloud["schema"] == "raw"
