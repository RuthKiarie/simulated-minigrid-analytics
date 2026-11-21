import logging
import os

import pandas as pd
import streamlit as st
from botocore.exceptions import BotoCoreError, ClientError
from pyathena import connect
from pyathena.error import Error as AthenaError
from pyathena.pandas.cursor import PandasCursor

LOGGER = logging.getLogger(__name__)

DEFAULT_REGION = os.getenv("AWS_REGION", "us-east-1")
DEFAULT_WORKGROUP = os.getenv("ATHENA_WORKGROUP", "minigrid-sim-wg")
DEFAULT_DATABASE = os.getenv("ATHENA_DATABASE", "minigrid")
DEFAULT_STAGING_DIR = os.getenv(
    "ATHENA_S3_STAGING_DIR",
    "s3://minigrid-sim-041904915024-us-east-1/athena-results/",
)

st.set_page_config(page_title="DRC Mini-Grid Analytics", layout="wide")
st.title("DRC Mini-Grid Operations & Billing Dashboard")


def get_aws_config() -> dict:
    try:
        aws_section = st.secrets.get("aws")
        if aws_section is not None:
            return dict(aws_section)
    except (AttributeError, FileNotFoundError, KeyError, OSError, RuntimeError, TypeError) as exc:
        LOGGER.debug("No [aws] section in st.secrets; falling back to defaults: %s", exc)
    return {}


@st.cache_data(ttl=300)
def run_query(sql: str) -> pd.DataFrame:
    aws_cfg = get_aws_config()
    conn = connect(
        region_name=aws_cfg.get("region_name", DEFAULT_REGION),
        work_group=aws_cfg.get("work_group", DEFAULT_WORKGROUP),
        schema_name=aws_cfg.get("schema_name", DEFAULT_DATABASE),
        s3_staging_dir=aws_cfg.get("s3_staging_dir", DEFAULT_STAGING_DIR),
        aws_access_key_id=aws_cfg.get("aws_access_key_id"),
        aws_secret_access_key=aws_cfg.get("aws_secret_access_key"),
        aws_session_token=aws_cfg.get("aws_session_token"),
        cursor_class=PandasCursor,
    )
    with conn.cursor() as cursor:
        return cursor.execute(sql).as_pandas()


try:
    site_daily = run_query("SELECT * FROM v_site_daily ORDER BY d DESC, site")
    alerts = run_query("SELECT * FROM v_alerts ORDER BY d DESC, site")
    billing = run_query("SELECT * FROM v_billing_reconciliation ORDER BY month DESC, site, tier")
except (AthenaError, BotoCoreError, ClientError, OSError, RuntimeError, ValueError) as exc:
    st.error(f"Failed to query Athena ({DEFAULT_DATABASE} / {DEFAULT_WORKGROUP} in {DEFAULT_REGION}): {exc}")
    st.stop()

all_sites = sorted(
    set(site_daily.get("site", pd.Series(dtype=str)).dropna().astype(str))
    | set(alerts.get("site", pd.Series(dtype=str)).dropna().astype(str))
    | set(billing.get("site", pd.Series(dtype=str)).dropna().astype(str))
)

selected_sites = st.sidebar.multiselect("Site", options=all_sites, default=all_sites)

if "d" in site_daily.columns and not site_daily["d"].dropna().empty:
    site_daily["d_parsed"] = pd.to_datetime(site_daily["d"], errors="coerce")
    min_d = site_daily["d_parsed"].min().date()
    max_d = site_daily["d_parsed"].max().date()
    date_range = st.sidebar.date_input("Date range", value=(min_d, max_d), min_value=min_d, max_value=max_d)
else:
    site_daily["d_parsed"] = pd.NaT
    date_range = None


def filter_site_and_date(df: pd.DataFrame, date_col: str = "d") -> pd.DataFrame:
    out = df.copy()
    if selected_sites and "site" in out.columns:
        out = out[out["site"].astype(str).isin(selected_sites)]
    if date_col in out.columns and not out[date_col].dropna().empty:
        parsed = pd.to_datetime(out[date_col], errors="coerce").dt.date
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            start_d, end_d = date_range
            out = out[(parsed >= start_d) & (parsed <= end_d)]
        elif date_range:
            out = out[parsed == date_range]
    return out


filtered_site = filter_site_and_date(site_daily, "d")
filtered_alerts = filter_site_and_date(alerts, "d")
filtered_billing = billing.copy()
if selected_sites and "site" in filtered_billing.columns:
    filtered_billing = filtered_billing[filtered_billing["site"].astype(str).isin(selected_sites)]
if "month" in filtered_billing.columns and not filtered_billing["month"].dropna().empty:
    months = sorted(filtered_billing["month"].dropna().astype(str).unique().tolist())
    selected_months = st.sidebar.multiselect("Billing Month", options=months, default=months)
    if selected_months:
        filtered_billing = filtered_billing[filtered_billing["month"].astype(str).isin(selected_months)]

st.subheader("Daily Site Telemetry (v_site_daily)")
if not filtered_site.empty:
    metric_cols = [c for c in ["generation_mwh", "supply_mwh", "metered_mwh"] if c in filtered_site.columns]
    if metric_cols and "d_parsed" in filtered_site.columns:
        chart_df = filtered_site.pivot_table(
            index="d_parsed", columns="site", values=metric_cols[0], aggfunc="sum"
        )
        st.caption(f"Daily {metric_cols[0]} by site")
        st.line_chart(chart_df)
    display_site = filtered_site.drop(columns=["d_parsed"], errors="ignore")
    st.dataframe(display_site, width="stretch")
else:
    st.info("No v_site_daily rows match the current filters.")

st.subheader("Anomaly Alerts (v_alerts)")
if not filtered_alerts.empty:
    st.dataframe(filtered_alerts, width="stretch")
else:
    st.info("No v_alerts rows match the current filters.")

st.subheader("Billing Reconciliation (v_billing_reconciliation)")
if not filtered_billing.empty:
    st.dataframe(filtered_billing, width="stretch")
else:
    st.info("No v_billing_reconciliation rows match the current filters.")
