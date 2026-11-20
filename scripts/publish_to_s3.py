"""Validate telemetry locally, then publish raw, quarantine and curated files.

Keep one run per prefix when querying with Athena: each dated folder under
curated/ and raw/ is read, so re-publishing on a new day doubles the rows.

Keep one run per prefix when querying with Athena: each dated folder under
curated/ and raw/ is read, so re-publishing on a new day doubles the rows.

Curated is uploaded LAST: that upload triggers the anomaly Lambda, so the
other files are already in place when it runs.

Usage:
    python -m scripts.publish_to_s3 --bucket <bucket-name>
"""

from __future__ import annotations

import argparse
import io
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pandas as pd

from src.schemas import route_quarantine, validate_dataframe

# Each dataset gets its own prefix so the Athena tables only see their own files.
RAW_FILES = {
    "smart_meter_telemetry.csv": "raw/telemetry",
    "salesforce_crm_customers.csv": "raw/customers",
    "accounting_mobile_money.csv": "raw/ledger",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--source-dir", default="data/raw")
    args = parser.parse_args()
    run = datetime.now(UTC).strftime("%Y-%m-%d")
    s3 = boto3.client("s3")
    source = Path(args.source_dir)
    for name in RAW_FILES:
        s3.upload_file(str(source / name), args.bucket, f"raw/{run}/{name}")
    valid, bad = validate_dataframe(pd.read_csv(source / RAW_FILES[0]))
    route_quarantine(bad, f"s3://{args.bucket}/quarantine/{run}/telemetry_bad.csv")
    buffer = io.StringIO()
    valid.to_csv(buffer, index=False)
    s3.put_object(
        Bucket=args.bucket,
        Key=f"curated/{run}/telemetry.csv",
        Body=buffer.getvalue().encode("utf-8"),
    )
    print(
        f"raw: {len(RAW_FILES)} files | quarantined: {len(bad):,} | "
        f"curated: {len(valid):,} rows -> s3://{args.bucket}/curated/{run}/"
    )


if __name__ == "__main__":
    main()

