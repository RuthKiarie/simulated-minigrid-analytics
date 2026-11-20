# Mini-Grid Operations Data Pipeline (DRC Simulation)

[![CI](https://github.com/RuthKiarie/simulated-minigrid-analytics/workflows/ci.yml/badge.svg)](https://github.com/RuthKiarie/simulated-minigrid-analytics/actions/workflows/ci.yml)

> **Disclaimer and status (in progress, November 2026).** This is an **independent portfolio project**. It is not affiliated with, endorsed by or built on proprietary data from any company. Operational data (meters, customers, accounting) is **synthetic**. Only the solar irradiance and temperature inputs are real (NASA POWER). The AWS deployment and the QuickSight dashboard are **not built yet**; see the [roadmap](#roadmap).

## What this project is

A data pipeline that models how a solar-hybrid mini-grid operator could consolidate, validate and monitor operational data across four sites in the Democratic Republic of Congo: **Goma**, **Beni**, **Tadu** and **Faradje**.

It practises the work of a data analyst on a mini-grid: bringing data from several sources together, validating it before it reaches a report, flagging anomalies early and documenting every rule so a colleague can take it over.

## What works today

- Generates a month of hourly telemetry for four sites, a 400-account customer table and a three-month mobile-money ledger.
- Pulls **real** hourly solar irradiance and temperature from the NASA POWER API for each site.
- Validates every telemetry row with Pydantic. Invalid rows go to a **quarantine** file with the reason, instead of being dropped silently.
- Detects four kinds of anomaly with tested, documented rules (below).
- 23 automated tests, including an end-to-end test that checks every injected fault is caught and tests of the Lambda handler against mocked AWS services, plus linting of the code and the CloudFormation template, run by GitHub Actions on every push.
- Infrastructure as code (CloudFormation) and a publish script for the AWS deployment are written and tested locally. See [docs/DEPLOY.md](docs/DEPLOY.md). They have **not yet been deployed**.

## Data sources: real vs synthetic

| Dataset | Type | How it is made | File |
|---|---|---|---|
| Solar irradiance and temperature | **Real** | NASA POWER hourly API (`ALLSKY_SFC_SW_DWN`, `T2M`) at each site's coordinates | cached in `data/raw/` |
| Smart-meter telemetry | Synthetic | Generated on top of the irradiance data, with injected faults | `smart_meter_telemetry.csv` |
| CRM customers | Synthetic | 400 accounts across three tariff tiers, no personal data | `salesforce_crm_customers.csv` |
| Mobile-money accounting | Synthetic | Billed amounts and collections for three months | `accounting_mobile_money.csv` |

The generator records where irradiance came from in `data/raw/irradiance_sources.json`. If NASA POWER cannot be reached, it uses a clearly labelled clear-sky fallback (`synthetic-fallback`) so the project still runs offline. The tests use that fallback; a normal run with internet access uses NASA POWER. In my latest run, all four sites reported `NASA POWER`.

**Acknowledgment:** The data was obtained from National Aeronautics and Space Administration (NASA) Langley Research Center's Prediction Of Worldwide Energy Resources (POWER) project funded through the NASA Earth Science Division.

**Data reference:** The data was obtained from the POWER Project's Hourly v2.10.5 version on 2026/11/19.

## Assumptions (please read before reusing)

These are simulation choices, not facts about any real operator:

| Item | Value used | Note |
|---|---|---|
| Site coordinates | Goma, Beni exact to about 3 decimals; **Tadu and Faradje approximate** | verify before relying on site-specific irradiance |
| Installed PV (kW) | Goma 1300, Beni 400, Tadu 150, Faradje 150 | assumed, except that Goma is described publicly as about 1.3 MW |
| Average demand (kW) | Goma 110, Beni 33, Tadu 12, Faradje 12 | sized so daily demand is about 2 kWh per installed kW of PV |
| Battery size | 1.5 kWh per installed kW of PV | assumed |
| Meters per site | 12, 8, 5, 5 | represent aggregated "cluster" meters, not single households |
| Technical loss | 5% | normal distribution loss |
| Exchange rate | 2,800 CDF per USD | assumed constant |
| Time zone | UTC+2 | used for evening-peak and sag rules |

## Anomaly rules

| Rule | Logic |
|---|---|
| `silent_meter` | No reading from a meter for more than 6 hours |
| `distribution_loss` | Daily site energy balance: (supplied minus metered) / supplied above 15% |
| `voltage_sag` | At least 3 evening readings (local 18:00 to 22:00) below 195 V on a day |
| `diesel_spike` | Daily diesel runtime above both 480 minutes and twice the site's median |

The generator injects one known fault of each kind, plus about 1% corrupt rows (negative energy, impossible voltage, unknown site, bad timestamp, missing meter ID). `tests/test_pipeline.py` checks that the validation and rules catch all of them.

The thresholds are my own choices for this simulation, not an industry standard.

## Quick start

```bash
git clone https://github.com/RuthKiarie/simulated-minigrid-analytics.git
cd simulated-minigrid-analytics
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# generate data (needs internet for NASA POWER; add --offline to skip it)
python data/generate_data.py --start 2026-10-01 --end 2026-10-30

# validate: valid rows to curated/, bad rows to quarantine/ with reasons
python -m src.schemas data/raw/smart_meter_telemetry.csv

# run the checks
ruff check .
cfn-lint infra/template.yaml --ignore-checks W3002
pytest -q
```

My latest run produced 21,580 telemetry rows, 400 customers and 1,200 ledger rows.

## Repository structure

```text
simulated-minigrid-analytics/
├── data/
│   ├── generate_data.py      # NASA POWER fetcher and synthetic data generator
│   └── raw/                  # generated CSVs and NASA cache (CSVs not committed)
├── src/
│   ├── schemas.py            # Pydantic validation and quarantine routing
│   └── anomaly_lambda.py     # anomaly rules and AWS Lambda handler (SNS alerts)
├── tests/
│   ├── test_validation.py    # schema and quarantine tests
│   ├── test_anomalies.py     # one test per rule
│   └── test_pipeline.py      # end to end: every injected fault is caught
├── sql/                      # Athena views (planned)
├── dashboard/                # QuickSight export (planned)
└── .github/workflows/ci.yml  # ruff + pytest on every push
```

## Roadmap

- [x] Architecture, schemas and site definitions
- [x] NASA POWER fetcher and synthetic data generator with injected faults
- [x] Pydantic validation and quarantine
- [x] Anomaly rules, unit tests and CI
- [x] Infrastructure as code and deployment guide written (CloudFormation template, publish script, mocked-AWS tests)
- [x] Deploy to AWS: S3 (raw, curated, quarantine), Lambda anomaly handler, CloudWatch, SNS
- [ ] Athena SQL views for technical and accounting reporting
- [ ] Three-tab QuickSight dashboard: executive overview, technical diagnostics, accounting reconciliation
- [ ] Anomaly alerts from real deployed Lambda runs

## Limitations

- The data is a simulation. Findings here say nothing about any real mini-grid.
- Battery, diesel and demand behaviour is a simple model, not engineering-grade.
- The anomaly thresholds are illustrative.
- The cloud and dashboard layers are not built yet.

## Planned architecture

```text
NASA POWER API ──┐
                 ├─> Python ingestion + Pydantic validation ──┬─> S3 quarantine/   (invalid rows)
Synthetic CSVs ──┘                                            └─> S3 curated/      (valid rows)
                                                                        │
                                       Lambda anomaly detector <────────┤
                                       (CloudWatch logs + SNS alerts)   │
                                                                        └─> Athena views ─> QuickSight
```

The ingestion, validation and anomaly rules already run locally, and the AWS infrastructure is defined as code. It will be marked as deployed here only after it runs in a real AWS account, with screenshots.

## AWS Deployment Proof

### CloudFormation Stack & S3 Data Lake
![S3 raw, curated, and quarantine prefixes](docs/images/s3-buckets-prefixes.png)

### Lambda Anomaly Detection & SNS Email Alert
![CloudWatch Lambda execution logs](docs/images/cloudwatch-lambda-logs.png)
![SNS anomaly summary email alert](docs/images/sns-alert-email.png)