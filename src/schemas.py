"""Validation of smart-meter telemetry with Pydantic.

Valid rows go on to the curated layer. Invalid rows are not dropped silently:
they are written to a quarantine location with the reason, so they can be
audited without breaking downstream BI queries.

Usage:
    python -m src.schemas data/raw/smart_meter_telemetry.csv \
        --curated data/curated/telemetry.csv \
        --quarantine data/quarantine/telemetry_bad.csv
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError


class TelemetryRow(BaseModel):
    timestamp: datetime
    site: Literal["Goma", "Beni", "Tadu", "Faradje"]
    meter_id: str = Field(min_length=3)
    feeder_id: str = Field(min_length=1)
    kwh: float = Field(ge=0, le=5000)
    voltage_v: float = Field(ge=100, le=300)  # sags below 195 V are valid data
    battery_soc_pct: float = Field(ge=0, le=100)
    diesel_runtime_min: float = Field(ge=0, le=60)
    irradiance_wm2: float = Field(ge=0, le=1500)
    site_generation_kwh: float = Field(ge=0)
    site_net_supply_kwh: float = Field(ge=0)


def validate_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a telemetry DataFrame into (valid, quarantined) rows."""
    good, bad = [], []
    for record in df.to_dict("records"):
        try:
            good.append(TelemetryRow.model_validate(record).model_dump())
        except ValidationError as exc:
            reasons = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
            )
            bad.append({**record, "quarantine_reason": reasons})
    valid_df = pd.DataFrame(good, columns=list(TelemetryRow.model_fields))
    return valid_df, pd.DataFrame(bad)


def route_quarantine(bad: pd.DataFrame, destination: str) -> str | None:
    """Write quarantined rows to a local path or an s3://bucket/key URI."""
    if bad.empty:
        return None
    if destination.startswith("s3://"):
        import io

        import boto3

        bucket, _, key = destination[5:].partition("/")
        buffer = io.StringIO()
        bad.to_csv(buffer, index=False)
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=buffer.getvalue().encode("utf-8")
        )
    else:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        bad.to_csv(path, index=False)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate telemetry CSV")
    parser.add_argument("source")
    parser.add_argument("--curated", default="data/curated/telemetry.csv")
    parser.add_argument("--quarantine", default="data/quarantine/telemetry_bad.csv")
    args = parser.parse_args()

    valid, bad = validate_dataframe(pd.read_csv(args.source))
    Path(args.curated).parent.mkdir(parents=True, exist_ok=True)
    valid.to_csv(args.curated, index=False)
    route_quarantine(bad, args.quarantine)
    print(f"valid: {len(valid):,} | quarantined: {len(bad):,}")


if __name__ == "__main__":
    main()