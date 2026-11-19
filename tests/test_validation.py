import pandas as pd
import pytest

from src.schemas import route_quarantine, validate_dataframe

GOOD = {
    "timestamp": "2026-10-05T12:00:00Z",
    "site": "Goma",
    "meter_id": "GOM-M01",
    "feeder_id": "GOM-F1",
    "kwh": 12.5,
    "voltage_v": 228.0,
    "battery_soc_pct": 64.0,
    "diesel_runtime_min": 0.0,
    "irradiance_wm2": 640.0,
    "site_generation_kwh": 800.0,
    "site_net_supply_kwh": 500.0,
}

def run(**overrides):
    return validate_dataframe(pd.DataFrame([{**GOOD, **overrides}]))

def test_good_row_is_valid():
    valid, bad = run()
    assert len(valid) == 1 and bad.empty

@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"kwh": -3.0}, "kwh"),
        ({"voltage_v": 999.0}, "voltage_v"),
        ({"site": "Unknown"}, "site"),
        ({"timestamp": "not-a-date"}, "timestamp"),
        ({"meter_id": None}, "meter_id"),
        ({"battery_soc_pct": 140.0}, "battery_soc_pct"),
        ({"kwh": float("nan")}, "kwh"),
    ],
)
def test_bad_rows_are_quarantined_with_reason(overrides, expected):
    valid, bad = run(**overrides)
    assert valid.empty and len(bad) == 1
    assert expected in bad.iloc[0]["quarantine_reason"]

def test_low_voltage_sag_is_valid_data():
    valid, bad = run(voltage_v=180.0)
    assert len(valid) == 1 and bad.empty

def test_quarantine_written_locally(tmp_path):
    _, bad = run(kwh=-1.0)
    dest = tmp_path / "q" / "bad.csv"
    assert route_quarantine(bad, str(dest)) == str(dest)
    assert "quarantine_reason" in dest.read_text()

def test_no_quarantine_file_when_nothing_is_bad(tmp_path):
    _, bad = run()
    assert route_quarantine(bad, str(tmp_path / "bad.csv")) is None
