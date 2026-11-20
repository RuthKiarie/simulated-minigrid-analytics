"""Check the Athena (Trino) views by running them on DuckDB over real CSVs.

The SQL is parsed as Trino and transpiled to DuckDB, so this verifies syntax
and logic. It does not prove Athena-specific behaviour (permissions, SerDe
settings), which is checked in the real deployment.
"""
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import sqlglot

from data.generate_data import generate
from src.schemas import validate_dataframe

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "athena_views.sql"
EXPECTED_VIEWS = {
    "v_telemetry", "v_site_hourly", "v_site_daily", "v_distribution_loss_daily",
    "v_voltage_daily", "v_meter_status", "v_diesel_daily", "v_alerts",
    "v_ledger", "v_billing_reconciliation", "v_overdue_aging",
}


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    out = tmp_path_factory.mktemp("views")
    data = generate(out_dir=str(out), seed=7, offline=True)
    valid, _ = validate_dataframe(data["telemetry"])

    # Round-trip through CSV, as the curated file in S3 would be, all strings.
    valid.to_csv(out / "curated.csv", index=False)
    telemetry = pd.read_csv(out / "curated.csv", dtype=str)
    telemetry = telemetry.rename(columns={"timestamp": "ts_raw"})
    ledger = pd.read_csv(out / "accounting_mobile_money.csv", dtype=str)

    db = duckdb.connect()
    db.execute("CREATE SCHEMA minigrid")
    db.register("telemetry", telemetry)
    db.register("ledger", ledger)
    db.execute("CREATE TABLE minigrid.raw_telemetry AS SELECT * FROM telemetry")
    db.execute("CREATE TABLE minigrid.raw_ledger AS SELECT * FROM ledger")
    for statement in sqlglot.parse(SQL_FILE.read_text(encoding="utf-8"), read="trino"):
        db.execute(statement.sql(dialect="duckdb"))
    return db


def test_all_views_exist(con):
    names = {r[0] for r in con.execute(
        "SELECT view_name FROM duckdb_views() WHERE schema_name = 'minigrid'"
    ).fetchall()}
    assert EXPECTED_VIEWS <= names


def test_alert_view_finds_every_injected_fault(con):
    found = set(con.execute("SELECT rule, site FROM minigrid.v_alerts").fetchall())
    assert ("silent_meter", "Goma") in found
    assert ("silent_meter", "Beni") in found
    assert ("distribution_loss", "Tadu") in found
    assert ("voltage_sag", "Faradje") in found
    assert ("diesel_spike", "Beni") in found


def test_daily_view_is_one_row_per_site_and_day(con):
    dupes = con.execute(
        "SELECT count(*) FROM (SELECT site, d FROM minigrid.v_site_daily "
        "GROUP BY site, d HAVING count(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0
    sites = {r[0] for r in con.execute(
        "SELECT DISTINCT site FROM minigrid.v_site_daily").fetchall()}
    assert sites == {"Goma", "Beni", "Tadu", "Faradje"}


def test_normal_loss_is_below_threshold_and_faulty_days_are_above(con):
    normal = con.execute(
        "SELECT max(loss_pct) FROM minigrid.v_distribution_loss_daily "
        "WHERE site = 'Goma' AND NOT is_anomaly").fetchone()[0]
    assert 3 < normal < 15
    faulty = con.execute(
        "SELECT min(loss_pct) FROM minigrid.v_distribution_loss_daily "
        "WHERE site = 'Tadu' AND is_anomaly").fetchone()[0]
    assert faulty > 15


def test_reconciliation_collected_never_exceeds_billed(con):
    over = con.execute(
        "SELECT count(*) FROM minigrid.v_billing_reconciliation "
        "WHERE collected_usd > billed_usd + 0.01").fetchone()[0]
    assert over == 0
    rate = con.execute(
        "SELECT min(collection_rate_pct), max(collection_rate_pct) "
        "FROM minigrid.v_billing_reconciliation").fetchone()
    assert 0 <= rate[0] and rate[1] <= 100.01


def test_ledger_totals_match_between_views(con):
    ledger = con.execute("SELECT sum(billed_usd) FROM minigrid.v_ledger").fetchone()[0]
    recon = con.execute(
        "SELECT sum(billed_usd) FROM minigrid.v_billing_reconciliation").fetchone()[0]
    assert ledger == pytest.approx(recon)


def test_overdue_aging_only_contains_unpaid_accounts(con):
    unpaid = con.execute(
        "SELECT count(*) FROM minigrid.v_ledger WHERE payment_status <> 'Paid'"
    ).fetchone()[0]
    aged = con.execute("SELECT sum(accounts) FROM minigrid.v_overdue_aging").fetchone()[0]
    assert aged == unpaid
