import csv
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
SEEDS_RAW = REPO_ROOT / "seeds" / "raw"
SOURCES_FILE = REPO_ROOT / "models" / "staging" / "salescloud" / "__salescloud_sources.yml"


def _load_sources():
    with open(SOURCES_FILE) as f:
        return yaml.safe_load(f)


def _csv_headers(table_name: str) -> list[str]:
    with open(SEEDS_RAW / f"{table_name}.csv") as f:
        return [h.strip() for h in next(csv.reader(f))]


def _csv_rows(table_name: str) -> list[dict]:
    with open(SEEDS_RAW / f"{table_name}.csv") as f:
        return list(csv.DictReader(f))


def test_csv_exists_for_every_declared_source_table():
    sources = _load_sources()
    salescloud = next(s for s in sources["sources"] if s["name"] == "salescloud")
    for table in salescloud["tables"]:
        assert (SEEDS_RAW / f"{table['name']}.csv").exists(), \
            f"Missing: seeds/raw/{table['name']}.csv"


def test_opportunity_csv_has_required_columns():
    headers = _csv_headers("opportunity")
    required = [
        "id", "accountid", "ownerid", "name", "stagename", "type", "leadsource",
        "amount", "probability", "expectedrevenue", "createddate", "closedate",
        "laststagechangedate", "isclosed", "iswon", "isdeleted",
        "lastmodifieddate", "systemmodstamp",
    ]
    for col in required:
        assert col in headers, f"opportunity.csv missing column: {col}"


def test_account_csv_has_required_columns():
    headers = _csv_headers("account")
    required = [
        "id", "name", "type", "industry", "billingcity", "billingstate",
        "billingcountry", "ownerid", "isdeleted", "createddate", "lastmodifieddate",
    ]
    for col in required:
        assert col in headers, f"account.csv missing column: {col}"


def test_user_csv_has_required_columns():
    headers = _csv_headers("user")
    required = [
        "id", "name", "email", "username", "userroleid", "profileid",
        "title", "isactive", "createddate", "lastmodifieddate",
    ]
    for col in required:
        assert col in headers, f"user.csv missing column: {col}"


def test_opportunitylineitem_csv_has_required_columns():
    # opportunitylineitem uses mixed-case headers — check exact case
    headers = _csv_headers("opportunitylineitem")
    required = [
        "Id", "OpportunityId", "PricebookEntryId", "Product2Id", "Name",
        "ProductCode", "Quantity", "UnitPrice", "TotalPrice", "Discount",
        "Description", "ServiceDate", "SortOrder", "CreatedDate",
    ]
    for col in required:
        assert col in headers, f"opportunitylineitem.csv missing column: {col}"


def test_opportunityhistory_csv_has_required_columns():
    headers = [h.lower() for h in _csv_headers("opportunityhistory")]
    assert "id" in headers
    assert "opportunityid" in headers


def test_opportunity_has_at_least_5_rows():
    assert len(_csv_rows("opportunity")) >= 5


def test_opportunity_has_iswon_true_and_false():
    rows = _csv_rows("opportunity")
    values = {r["iswon"].lower() for r in rows}
    assert "true" in values, "opportunity.csv needs at least one iswon=true row"
    assert "false" in values, "opportunity.csv needs at least one iswon=false row"


def test_account_ownerids_present_in_user_csv():
    user_ids = {r["id"] for r in _csv_rows("user")}
    for row in _csv_rows("account"):
        assert row["ownerid"] in user_ids, \
            f"account.ownerid '{row['ownerid']}' not found in user.csv"


def test_opportunity_accountids_present_in_account_csv():
    account_ids = {r["id"] for r in _csv_rows("account")}
    for row in _csv_rows("opportunity"):
        assert row["accountid"] in account_ids, \
            f"opportunity.accountid '{row['accountid']}' not found in account.csv"


def test_opportunitylineitem_opportunityids_present_in_opportunity_csv():
    opp_ids = {r["id"] for r in _csv_rows("opportunity")}
    for row in _csv_rows("opportunitylineitem"):
        assert row["OpportunityId"] in opp_ids, \
            f"opportunitylineitem.OpportunityId '{row['OpportunityId']}' not found in opportunity.csv"
