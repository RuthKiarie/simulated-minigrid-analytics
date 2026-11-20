"""Run sql/athena_views.sql in Athena, one statement at a time.

Usage:
    python -m scripts.run_athena_sql --workgroup <stack>-wg
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import boto3


def split_statements(sql: str) -> list[str]:
    """Split a SQL file on semicolons, dropping comment-only lines."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def run(client, statement: str, workgroup: str, database: str) -> None:
    qid = client.start_query_execution(
        QueryString=statement,
        WorkGroup=workgroup,
        QueryExecutionContext={"Database": database},
    )["QueryExecutionId"]
    while True:
        status = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"][
            "Status"
        ]
        if status["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if status["State"] != "SUCCEEDED":
        reason = status.get("StateChangeReason", "no reason given")
        raise RuntimeError(f"{status['State']}: {reason}\n{statement[:200]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workgroup", required=True)
    parser.add_argument("--database", default="minigrid")
    parser.add_argument("--file", default="sql/athena_views.sql")
    args = parser.parse_args()

    client = boto3.client("athena")
    statements = split_statements(Path(args.file).read_text())
    for i, statement in enumerate(statements, 1):
        run(client, statement, args.workgroup, args.database)
        print(f"[{i}/{len(statements)}] ok")


if __name__ == "__main__":
    main()
