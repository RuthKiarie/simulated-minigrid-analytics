"""Generate mini-grid simulation data.

Real input : hourly solar irradiance and temperature from the NASA POWER API.
Synthetic  : smart-meter telemetry, CRM customers and mobile-money accounting,
             with deliberately injected faults so the validation and anomaly
             rules have something real to catch.

If the NASA API cannot be reached (or --offline is passed), a clear-sky
irradiance curve is used instead and the source is reported as
"synthetic-fallback". Never describing fallback data as NASA data.

Usage:
    python data/generate_data.py --start 2026-10-01 --end 2026-10-30
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

NASA_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
UTC_OFFSET = 2  # DRC east (Goma, Beni) is UTC+2
TECH_LOSS = 0.05  # normal technical distribution loss
PERFORMANCE_RATIO = 0.8

# Coordinates are approximate. Verify them before you publish.
# pv_kw, avg_kw and battery sizes are ASSUMPTIONS for the simulation.
# avg_kw is sized so daily demand is roughly 2 kWh per installed kW of PV.
SITES = {
    "Goma": {"lat": -1.679, "lon": 29.222, "pv_kw": 1300, "avg_kw": 110,
             "meters": 12, "feeders": 3},
    "Beni": {"lat": 0.491, "lon": 29.473, "pv_kw": 400, "avg_kw": 33,
             "meters": 8, "feeders": 2},
    "Tadu": {"lat": 3.600, "lon": 29.800, "pv_kw": 150, "avg_kw": 12,
             "meters": 5, "feeders": 1},
    "Faradje": {"lat": 3.733, "lon": 29.717, "pv_kw": 150, "avg_kw": 12,
                "meters": 5, "feeders": 1},
}
TIERS = {
    "Residential": {"share": 0.80, "tariff_usd_kwh": 0.25, "mean_kwh": 25},
    "SME_Commercial": {"share": 0.17, "tariff_usd_kwh": 0.20, "mean_kwh": 180},
    "Clinic_Anchor": {"share": 0.03, "tariff_usd_kwh": 0.18, "mean_kwh": 600},
}
FX_CDF_PER_USD = 2800  # assumption for the simulation


# ---------------------------------------------------------------- irradiance
def fetch_nasa(site, cfg, hours, cache_dir):
    """Return hourly irradiance (W/m2) from NASA POWER, cached on disk."""
    import requests

    start, end = hours[0].strftime("%Y%m%d"), hours[-1].strftime("%Y%m%d")
    cache = Path(cache_dir) / f"nasa_{site}_{start}_{end}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        resp = requests.get(
            NASA_URL,
            params={
                "parameters": "ALLSKY_SFC_SW_DWN,T2M",
                "community": "RE",
                "longitude": cfg["lon"],
                "latitude": cfg["lat"],
                "start": start,
                "end": end,
                "format": "JSON",
                "time-standard": "UTC",
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        cache.write_text(json.dumps(data))
    ghi = data["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
    idx = pd.to_datetime(list(ghi.keys()), format="%Y%m%d%H", utc=True)
    series = pd.Series(list(ghi.values()), index=idx, dtype=float)
    series[series < 0] = np.nan  # NASA uses -999 for missing values
    return series.reindex(hours)


def fallback_irradiance(cfg, hours, rng):
    """Clear-sky curve with random daily cloud cover (clearly labelled)."""
    solar_hour = (hours.hour + cfg["lon"] / 15.0) % 24
    clear = np.clip(950 * np.sin(np.pi * (solar_hour - 6) / 12), 0, None)
    days = (hours - hours[0]).days
    cloud_by_day = rng.uniform(0.35, 1.0, size=days.max() + 1)
    return pd.Series(clear * cloud_by_day[days], index=hours)


def get_irradiance(site, cfg, hours, out_dir, offline, rng):
    fallback = fallback_irradiance(cfg, hours, rng)
    if offline:
        return fallback, "synthetic-fallback"
    try:
        nasa = fetch_nasa(site, cfg, hours, out_dir)
        return nasa.fillna(fallback), "NASA POWER"
    except (OSError, ImportError, KeyError, ValueError) as exc:  # network/HTTP/parse
        print(f"[warn] NASA POWER unavailable for {site}: {exc}. Using fallback.")
        return fallback, "synthetic-fallback"


# ---------------------------------------------------------------- simulation
def demand_profile(local_hour):
    return (
        0.4
        + 0.9 * np.exp(-(((local_hour - 19.5) / 2.2) ** 2))
        + 0.25 * np.exp(-(((local_hour - 7.0) / 1.5) ** 2))
    )


def simulate_site(site, cfg, hours, irr, rng):
    n, m, feeders = len(hours), cfg["meters"], cfg["feeders"]
    code = site[:3].upper()
    local_hour = (hours.hour.to_numpy() + UTC_OFFSET) % 24
    profile = demand_profile(local_hour)

    weights = rng.uniform(0.5, 2.0, size=m)
    weights /= weights.sum()
    noise = rng.lognormal(0, 0.12, size=(n, m))
    kwh = cfg["avg_kw"] * (profile / profile.mean())[:, None] * weights * noise

    supply = kwh.sum(axis=1) * (1 + TECH_LOSS + rng.normal(0, 0.01, n))
    gen = cfg["pv_kw"] * irr.to_numpy() / 1000 * PERFORMANCE_RATIO

    capacity = cfg["pv_kw"] * 1.5  # battery kWh (assumption)
    soc = np.empty(n)
    diesel_min = np.zeros(n)
    level = 70.0
    mean_supply = supply.mean()
    for i in range(n):
        level += (gen[i] - supply[i]) / capacity * 100
        deficit = 0.0
        if level < 10:
            deficit = (10 - level) / 100 * capacity
            level = 10.0
        level = min(level, 100.0)
        soc[i] = level
        diesel_min[i] = min(60.0, 60 * deficit / (0.7 * mean_supply))

    v_feeder = (
        230 - 8 * (profile / profile.max())[:, None]
        + rng.normal(0, 1.5, size=(n, feeders))
    )
    feeder_idx = np.arange(m) % feeders
    voltage = v_feeder[:, feeder_idx] + rng.normal(0, 0.8, size=(n, m))

    def rep(a):  # site-level value repeated for each meter
        return np.repeat(a, m)

    return pd.DataFrame({
        "timestamp": np.repeat(hours.to_numpy(), m),
        "site": site,
        "meter_id": np.tile([f"{code}-M{j + 1:02d}" for j in range(m)], n),
        "feeder_id": np.tile([f"{code}-F{f + 1}" for f in feeder_idx], n),
        "kwh": kwh.ravel().round(3),
        "voltage_v": voltage.ravel().round(1),
        "battery_soc_pct": rep(soc).round(1),
        "diesel_runtime_min": rep(diesel_min).round(1),
        "irradiance_wm2": rep(irr.to_numpy()).round(1),
        "site_generation_kwh": rep(gen).round(3),
        "site_net_supply_kwh": rep(supply).round(3),
    })


def inject_faults(df, rng):
    """Inject known faults. The anomaly rules should find every one."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    day = (df["timestamp"] - df["timestamp"].min()).dt.days
    local_hour = (df["timestamp"].dt.hour + UTC_OFFSET) % 24

    # 1. Non-technical loss: Tadu meters under-report for three days.
    mask = (df["site"] == "Tadu") & day.between(20, 22)
    df.loc[mask, "kwh"] = (df.loc[mask, "kwh"] * 0.65).round(3)

    # 2. Evening voltage sags at Faradje.
    mask = (df["site"] == "Faradje") & day.between(10, 12) & local_hour.between(18, 21)
    df.loc[mask, "voltage_v"] = rng.uniform(175, 193, size=mask.sum()).round(1)

    # 3. Abnormal diesel runtime at Beni for one full day.
    mask = (df["site"] == "Beni") & (day == 15)
    df.loc[mask, "diesel_runtime_min"] = 60.0

    # 4. Two meters go silent for the last 10 hours.
    cutoff = df["timestamp"].max() - pd.Timedelta(hours=10)
    silent = df["meter_id"].isin(["GOM-M03", "BEN-M02"]) & (df["timestamp"] > cutoff)
    df = df[~silent].reset_index(drop=True)

    # 5. Corrupt rows (~1%): five kinds, to exercise quarantine.
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    idx = rng.choice(df.index, size=max(5, int(0.01 * len(df))), replace=False)
    for k, i in enumerate(idx):
        kind = k % 5
        if kind == 0:
            df.loc[i, "kwh"] = -abs(df.loc[i, "kwh"]) - 1
        elif kind == 1:
            df.loc[i, "voltage_v"] = 999.0
        elif kind == 2:
            df.loc[i, "site"] = "Unknown"
        elif kind == 3:
            df.loc[i, "timestamp"] = "not-a-date"
        else:
            df.loc[i, "meter_id"] = None
    return df


# ------------------------------------------------------------ CRM + accounting
def make_customers(rng, n=400):
    sites = rng.choice(list(SITES), size=n, p=[0.55, 0.2, 0.125, 0.125])
    tiers = rng.choice(list(TIERS), size=n, p=[t["share"] for t in TIERS.values()])
    return pd.DataFrame({
        "customer_id": [f"CUST-{i + 1:04d}" for i in range(n)],
        "site": sites,
        "tier": tiers,
        "tariff_usd_per_kwh": [TIERS[t]["tariff_usd_kwh"] for t in tiers],
    })


def make_ledger(customers, rng, months=("2026-08", "2026-09", "2026-10")):
    rows = []
    for _, c in customers.iterrows():
        for age, month in enumerate(reversed(months)):  # age 0 = latest month
            kwh = TIERS[c["tier"]]["mean_kwh"] * rng.lognormal(0, 0.3)
            usd = kwh * c["tariff_usd_per_kwh"]
            currency = rng.choice(["USD", "CDF"], p=[0.4, 0.6])
            billed = usd if currency == "USD" else usd * FX_CDF_PER_USD
            p_paid = 0.9 - 0.25 * (1 - min(age, 1))  # latest month more unpaid
            status = rng.choice(
                ["Paid", "Partial", "Overdue"],
                p=[p_paid, (1 - p_paid) * 0.6, (1 - p_paid) * 0.4],
            )
            frac = {"Paid": 1.0, "Partial": rng.uniform(0.2, 0.8), "Overdue": 0.0}[status]
            rows.append({
                "customer_id": c["customer_id"],
                "site": c["site"],
                "tier": c["tier"],
                "month": month,
                "billed_kwh": round(kwh, 1),
                "billed_usd": round(usd, 2),
                "currency": currency,
                "billed_amount_local": round(billed, 2),
                "collected_amount_local": round(billed * frac, 2),
                "payment_status": status,
                "days_overdue": 0 if status == "Paid" else int(rng.integers(5, 90)),
            })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ entry point
def generate(start="2026-10-01", end="2026-10-30", out_dir="data/raw",
             seed=42, offline=False):
    rng = np.random.default_rng(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    hours = pd.date_range(f"{start} 00:00", f"{end} 23:00", freq="h", tz="UTC")

    frames, sources = [], {}
    for site, cfg in SITES.items():
        irr, sources[site] = get_irradiance(site, cfg, hours, out, offline, rng)
        frames.append(simulate_site(site, cfg, hours, irr, rng))
    telemetry = inject_faults(pd.concat(frames, ignore_index=True), rng)

    customers = make_customers(rng)
    ledger = make_ledger(customers, rng)

    telemetry.to_csv(out / "smart_meter_telemetry.csv", index=False)
    customers.to_csv(out / "salesforce_crm_customers.csv", index=False)
    ledger.to_csv(out / "accounting_mobile_money.csv", index=False)
    (out / "irradiance_sources.json").write_text(json.dumps(sources, indent=2))
    return {"telemetry": telemetry, "customers": customers,
            "ledger": ledger, "irradiance_sources": sources}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2026-10-01")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--out", default="data/raw")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--offline", action="store_true",
                        help="skip NASA POWER and use the synthetic fallback")
    args = parser.parse_args()
    result = generate(args.start, args.end, args.out, args.seed, args.offline)
    print(f"telemetry rows : {len(result['telemetry']):,}")
    print(f"customers      : {len(result['customers']):,}")
    print(f"ledger rows    : {len(result['ledger']):,}")
    print(f"irradiance     : {result['irradiance_sources']}")