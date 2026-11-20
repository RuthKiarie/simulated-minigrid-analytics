-- Athena (Trino) views for the mini-grid simulation. Database: minigrid.
-- All dates are UTC, matching the Lambda alert rules. local_hour is UTC+2.
-- Thresholds mirror src/anomaly_lambda.py. Run with: python -m scripts.run_athena_sql

-- 1. Typed telemetry (raw_telemetry is all strings, read from curated/).
--    Accepts both '2026-10-01 00:00:00+00:00' and '2026-10-01T00:00:00Z'.
CREATE OR REPLACE VIEW minigrid.v_telemetry AS
SELECT
  t.*,
  CAST(t.ts_utc AS date) AS d,
  (hour(t.ts_utc) + 2) % 24 AS local_hour
FROM (
  SELECT
    date_parse(substr(replace(ts_raw, 'T', ' '), 1, 19), '%Y-%m-%d %H:%i:%s') AS ts_utc,
    site,
    meter_id,
    feeder_id,
    CAST(kwh AS double) AS kwh,
    CAST(voltage_v AS double) AS voltage_v,
    CAST(battery_soc_pct AS double) AS battery_soc_pct,
    CAST(diesel_runtime_min AS double) AS diesel_runtime_min,
    CAST(irradiance_wm2 AS double) AS irradiance_wm2,
    CAST(site_generation_kwh AS double) AS site_generation_kwh,
    CAST(site_net_supply_kwh AS double) AS site_net_supply_kwh
  FROM minigrid.raw_telemetry
) t;

-- 2. One row per site and hour (site-level values repeat on every meter row)
CREATE OR REPLACE VIEW minigrid.v_site_hourly AS
SELECT
  site,
  ts_utc,
  d,
  local_hour,
  max(site_generation_kwh) AS generation_kwh,
  max(site_net_supply_kwh) AS supply_kwh,
  max(diesel_runtime_min) AS diesel_runtime_min,
  max(battery_soc_pct) AS battery_soc_pct,
  max(irradiance_wm2) AS irradiance_wm2,
  sum(kwh) AS metered_kwh,
  count(DISTINCT meter_id) AS meters_reporting
FROM minigrid.v_telemetry
GROUP BY site, ts_utc, d, local_hour;

-- 3. Executive overview: one row per site and day
CREATE OR REPLACE VIEW minigrid.v_site_daily AS
SELECT
  site,
  d,
  sum(generation_kwh) / 1000.0 AS generation_mwh,
  sum(supply_kwh) / 1000.0 AS supply_mwh,
  sum(metered_kwh) / 1000.0 AS metered_mwh,
  sum(generation_kwh) / sum(supply_kwh) AS solar_to_supply_ratio,
  sum(diesel_runtime_min) AS diesel_minutes,
  100.0 * sum(diesel_runtime_min) / (count(*) * 60.0) AS diesel_runtime_share_pct,
  avg(battery_soc_pct) AS avg_battery_soc_pct,
  100.0 * count(*) / 24.0 AS reporting_coverage_pct
FROM minigrid.v_site_hourly
GROUP BY site, d;

-- 4. Technical: daily energy balance (supplied vs metered), flag above 15%
CREATE OR REPLACE VIEW minigrid.v_distribution_loss_daily AS
SELECT
  site,
  d,
  supply_kwh,
  metered_kwh,
  supply_kwh - metered_kwh AS loss_kwh,
  100.0 * (supply_kwh - metered_kwh) / supply_kwh AS loss_pct,
  (supply_kwh - metered_kwh) / supply_kwh > 0.15 AS is_anomaly
FROM (
  SELECT site, d, sum(supply_kwh) AS supply_kwh, sum(metered_kwh) AS metered_kwh
  FROM minigrid.v_site_hourly
  GROUP BY site, d
) daily;

-- 5. Technical: evening voltage sags (local 18:00-22:00, below 195 V)
CREATE OR REPLACE VIEW minigrid.v_voltage_daily AS
SELECT
  site,
  d,
  count_if(local_hour BETWEEN 18 AND 21 AND voltage_v < 195) AS sag_readings,
  min(CASE WHEN local_hour BETWEEN 18 AND 21 THEN voltage_v END) AS min_evening_voltage,
  count_if(local_hour BETWEEN 18 AND 21 AND voltage_v < 195) >= 3 AS is_anomaly
FROM minigrid.v_telemetry
GROUP BY site, d;

-- 6. Technical: meters silent for more than 6 hours (relative to latest data)
CREATE OR REPLACE VIEW minigrid.v_meter_status AS
SELECT
  l.site,
  l.meter_id,
  l.last_seen_utc,
  date_diff('second', l.last_seen_utc, n.now_utc) / 3600.0 AS hours_silent,
  date_diff('second', l.last_seen_utc, n.now_utc) / 3600.0 > 6 AS is_silent
FROM (
  SELECT site, meter_id, max(ts_utc) AS last_seen_utc
  FROM minigrid.v_telemetry
  GROUP BY site, meter_id
) l
CROSS JOIN (
  SELECT max(ts_utc) AS now_utc FROM minigrid.v_telemetry
) n;

-- 7. Technical: daily diesel runtime against the site's normal (approximate median)
CREATE OR REPLACE VIEW minigrid.v_diesel_daily AS
SELECT
  daily.site,
  daily.d,
  daily.diesel_minutes,
  m.site_median,
  daily.diesel_minutes > greatest(480, 2 * m.site_median) AS is_anomaly
FROM (
  SELECT site, d, sum(diesel_runtime_min) AS diesel_minutes
  FROM minigrid.v_site_hourly
  GROUP BY site, d
) daily
JOIN (
  SELECT site, approx_percentile(diesel_minutes, 0.5) AS site_median
  FROM (
    SELECT site, d, sum(diesel_runtime_min) AS diesel_minutes
    FROM minigrid.v_site_hourly
    GROUP BY site, d
  ) x
  GROUP BY site
) m ON daily.site = m.site;

-- 8. All anomalies in one list for the diagnostics tab
CREATE OR REPLACE VIEW minigrid.v_alerts AS
SELECT
  'silent_meter' AS rule,
  site,
  CAST(last_seen_utc AS date) AS d,
  meter_id AS subject,
  concat('silent for ', CAST(round(hours_silent, 1) AS varchar), ' hours') AS detail
FROM minigrid.v_meter_status
WHERE is_silent
UNION ALL
SELECT
  'distribution_loss',
  site,
  d,
  site,
  concat('loss of ', CAST(round(loss_pct, 1) AS varchar), ' percent')
FROM minigrid.v_distribution_loss_daily
WHERE is_anomaly
UNION ALL
SELECT
  'voltage_sag',
  site,
  d,
  site,
  concat(CAST(sag_readings AS varchar), ' readings, minimum ',
         CAST(round(min_evening_voltage, 1) AS varchar), ' V')
FROM minigrid.v_voltage_daily
WHERE is_anomaly
UNION ALL
SELECT
  'diesel_spike',
  site,
  d,
  site,
  concat(CAST(round(diesel_minutes, 0) AS varchar), ' diesel minutes')
FROM minigrid.v_diesel_daily
WHERE is_anomaly;

-- 9. Typed ledger. The 2800 CDF per USD rate matches data/generate_data.py
--    (a simulation assumption) and converts collections to USD.
CREATE OR REPLACE VIEW minigrid.v_ledger AS
SELECT
  customer_id,
  site,
  tier,
  month,
  CAST(billed_kwh AS double) AS billed_kwh,
  CAST(billed_usd AS double) AS billed_usd,
  currency,
  CAST(billed_amount_local AS double) AS billed_amount_local,
  CAST(collected_amount_local AS double) AS collected_amount_local,
  CASE WHEN currency = 'CDF'
       THEN CAST(collected_amount_local AS double) / 2800.0
       ELSE CAST(collected_amount_local AS double) END AS collected_usd,
  payment_status,
  CAST(days_overdue AS integer) AS days_overdue
FROM minigrid.raw_ledger;

-- 10. Accounting: billed vs collected by month, site and tariff tier
CREATE OR REPLACE VIEW minigrid.v_billing_reconciliation AS
SELECT
  month,
  site,
  tier,
  count(*) AS accounts,
  sum(billed_kwh) AS billed_kwh,
  sum(billed_usd) AS billed_usd,
  sum(collected_usd) AS collected_usd,
  sum(billed_usd) - sum(collected_usd) AS outstanding_usd,
  100.0 * sum(collected_usd) / sum(billed_usd) AS collection_rate_pct,
  count_if(payment_status = 'Overdue') AS overdue_accounts
FROM minigrid.v_ledger
GROUP BY month, site, tier;

-- 11. Accounting: unpaid balances by age
CREATE OR REPLACE VIEW minigrid.v_overdue_aging AS
SELECT
  site,
  tier,
  CASE
    WHEN days_overdue <= 30 THEN '1-30 days'
    WHEN days_overdue <= 60 THEN '31-60 days'
    ELSE '61+ days'
  END AS aging_bucket,
  count(*) AS accounts,
  sum(billed_usd - collected_usd) AS outstanding_usd
FROM minigrid.v_ledger
WHERE payment_status <> 'Paid'
GROUP BY
  site,
  tier,
  CASE
    WHEN days_overdue <= 30 THEN '1-30 days'
    WHEN days_overdue <= 60 THEN '31-60 days'
    ELSE '61+ days'
  END;
