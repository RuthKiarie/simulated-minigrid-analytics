import pandas as pd
from src.anomaly_lambda import (
    detect_diesel_spikes,
    detect_distribution_loss,
    detect_silent_meters,
    detect_voltage_sags,
)

def frame(rows):
    base = {
        "site": "Goma", "meter_id": "GOM-M01", "kwh": 10.0, "voltage_v": 230.0,
        "diesel_runtime_min": 0.0, "site_net_supply_kwh": 10.5,
    }
    return pd.DataFrame([{**base, **r} for r in rows])

def test_silent_meter_flagged_after_six_hours():
    df = frame([
        {"timestamp": "2026-10-05T00:00:00Z", "meter_id": "GOM-M01"},
        {"timestamp": "2026-10-05T12:00:00Z", "meter_id": "GOM-M02"},
    ])
    alerts = detect_silent_meters(df)
    assert [a["meter_id"] for a in alerts] == ["GOM-M01"]

def test_meter_within_six_hours_is_not_flagged():
    df = frame([
        {"timestamp": "2026-10-05T08:00:00Z", "meter_id": "GOM-M01"},
        {"timestamp": "2026-10-05T12:00:00Z", "meter_id": "GOM-M02"},
    ])
    assert detect_silent_meters(df) == []

def test_distribution_loss_over_threshold_is_flagged():
    df = frame([{"timestamp": "2026-10-05T10:00:00Z", "kwh": 7.0,
                 "site_net_supply_kwh": 10.0}])
    alerts = detect_distribution_loss(df)
    assert alerts and alerts[0]["loss_pct"] == 30.0

def test_normal_technical_loss_is_not_flagged():
    df = frame([{"timestamp": "2026-10-05T10:00:00Z", "kwh": 9.5,
                 "site_net_supply_kwh": 10.0}])
    assert detect_distribution_loss(df) == []

def test_evening_voltage_sag_is_flagged_only_in_the_evening():
    # 17:00 UTC is 19:00 local: evening. 05:00 UTC is 07:00 local: not evening.
    evening = frame([{"timestamp": f"2026-10-05T17:{m:02d}:00Z", "voltage_v": 185.0}
                     for m in (0, 10, 20)])
    morning = frame([{"timestamp": f"2026-10-05T05:{m:02d}:00Z", "voltage_v": 185.0}
                     for m in (0, 10, 20)])
    assert len(detect_voltage_sags(evening)) == 1
    assert detect_voltage_sags(morning) == []

def test_diesel_spike_flagged_against_site_normal():
    rows = [{"timestamp": f"2026-10-0{d}T{h:02d}:00:00Z",
             "diesel_runtime_min": 60.0 if d == 5 else 0.0}
            for d in range(1, 6) for h in range(24)]
    alerts = detect_diesel_spikes(frame(rows))
    assert [a["date"] for a in alerts] == ["2026-10-05"]
