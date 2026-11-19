"""Anomaly rules for mini-grid telemetry, packaged as an AWS Lambda handler.

The rules are plain functions over a DataFrame so they can be unit-tested
without AWS. The handler reads a telemetry file from S3, runs every rule,
logs each alert (CloudWatch Logs) and publishes a summary to SNS.

Rules
  silent_meter       no reading from a meter for more than 6 hours
  distribution_loss  daily site energy balance: supplied vs metered > 15%
  voltage_sag        evening (local 18:00-22:00) readings below 195 V
  diesel_spike       daily diesel runtime far above the site's normal
"""

from __future__ import annotations

import io
import json
import logging
import os

import pandas as pd

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STALE_HOURS = 6
LOSS_THRESHOLD = 0.15
VOLTAGE_MIN = 195.0
EVENING = (18, 22)  # local hours, inclusive start, exclusive end
UTC_OFFSET = 2
MIN_SAG_READINGS = 3
DIESEL_MIN_MINUTES = 480
DIESEL_RATIO = 2.0


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df


def _site_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """One row per site and hour (site-level columns repeat per meter)."""
    return df.drop_duplicates(["site", "timestamp"])


def detect_silent_meters(df, now=None, hours=STALE_HOURS):
    df = _prep(df)
    now = df["timestamp"].max() if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    last = df.groupby(["site", "meter_id"])["timestamp"].max().reset_index()
    stale = last[(now - last["timestamp"]) > pd.Timedelta(hours=hours)]
    return [
        {
            "rule": "silent_meter",
            "site": r.site,
            "meter_id": r.meter_id,
            "last_seen": r.timestamp.isoformat(),
            "hours_silent": round((now - r.timestamp).total_seconds() / 3600, 1),
        }
        for r in stale.itertuples()
    ]


def detect_distribution_loss(df, threshold=LOSS_THRESHOLD):
    df = _prep(df)
    supply = _site_hourly(df).groupby(["site", "date"])["site_net_supply_kwh"].sum()
    metered = df.groupby(["site", "date"])["kwh"].sum()
    out = pd.concat([supply, metered], axis=1).dropna()
    out["loss_ratio"] = (out["site_net_supply_kwh"] - out["kwh"]) / out[
        "site_net_supply_kwh"
    ]
    flagged = out[out["loss_ratio"] > threshold].reset_index()
    return [
        {
            "rule": "distribution_loss",
            "site": r.site,
            "date": r.date,
            "loss_pct": round(r.loss_ratio * 100, 1),
        }
        for r in flagged.itertuples()
    ]


def detect_voltage_sags(df, vmin=VOLTAGE_MIN, min_readings=MIN_SAG_READINGS):
    df = _prep(df)
    local_hour = (df["timestamp"].dt.hour + UTC_OFFSET) % 24
    evening = local_hour.between(EVENING[0], EVENING[1] - 1)
    low = df[evening & (df["voltage_v"] < vmin)]
    grouped = low.groupby(["site", "date"])["voltage_v"].agg(["count", "min"])
    grouped = grouped[grouped["count"] >= min_readings].reset_index()
    return [
        {
            "rule": "voltage_sag",
            "site": r.site,
            "date": r.date,
            "readings": int(r.count),
            "min_voltage": round(float(r.min), 1),
        }
        for r in grouped.itertuples()
    ]


def detect_diesel_spikes(df, min_minutes=DIESEL_MIN_MINUTES, ratio=DIESEL_RATIO):
    df = _prep(df)
    daily = (
        _site_hourly(df).groupby(["site", "date"])["diesel_runtime_min"].sum()
    ).reset_index()
    daily["site_median"] = daily.groupby("site")["diesel_runtime_min"].transform(
        "median"
    )
    limit = (daily["site_median"] * ratio).clip(lower=min_minutes)
    flagged = daily[daily["diesel_runtime_min"] > limit]
    return [
        {
            "rule": "diesel_spike",
            "site": r.site,
            "date": r.date,
            "diesel_minutes": round(r.diesel_runtime_min, 0),
        }
        for r in flagged.itertuples()
    ]


def run_all(df, now=None):
    return (
        detect_silent_meters(df, now)
        + detect_distribution_loss(df)
        + detect_voltage_sags(df)
        + detect_diesel_spikes(df)
    )


# ------------------------------------------------------------- Lambda wiring
def _read_s3(bucket: str, key: str) -> pd.DataFrame:
    import boto3

    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    if key.endswith(".parquet"):
        return pd.read_parquet(io.BytesIO(body))
    return pd.read_csv(io.BytesIO(body))


def _parse_event(event: dict) -> tuple[str, str]:
    if "Records" in event:  # S3 event notification
        s3 = event["Records"][0]["s3"]
        return s3["bucket"]["name"], s3["object"]["key"]
    return event["bucket"], event["key"]  # direct invocation


def lambda_handler(event, context=None):
    bucket, key = _parse_event(event)
    alerts = run_all(_read_s3(bucket, key))
    for alert in alerts:
        logger.warning(json.dumps(alert))
    topic = os.environ.get("SNS_TOPIC_ARN")
    if topic and alerts:
        import boto3

        counts: dict[str, int] = {}
        for a in alerts:
            counts[a["rule"]] = counts.get(a["rule"], 0) + 1
        boto3.client("sns").publish(
            TopicArn=topic,
            Subject="Mini-grid data alerts",
            Message=json.dumps({"file": f"s3://{bucket}/{key}", "counts": counts}),
        )
    return {"alerts": len(alerts)}