# Rinha Dashboard Automation

This repository automates a full refresh pipeline for the `results-preview.json` feed, enriches each repository with its primary GitHub language, and publishes a filterable dashboard.

## What is automated

1. Download latest source JSON from:
   `https://raw.githubusercontent.com/arinhadebackend/arinhadebackend.github.io/2026-preview/results-preview.json`
2. Normalize telemetry and scoring data per repository entry.
3. Query GitHub Languages API for each repository and detect the dominant language.
4. Build per-language rankings and global metrics (average/mean/median/P99 score, error/failure rates, p99 latency).
5. Write dashboard-ready JSON consumed by `dashboard/index.html`.

## Files

- `scripts/update_dashboard_data.py`: pipeline script (stdlib-only).
- `.github/workflows/update-dashboard.yml`: scheduled + manual updater.
- `dashboard/index.html`: rich dashboard with filters.
- `dashboard/data/dashboard-data.json`: dashboard data payload.
- `data/processed/dashboard-data.json`: same payload for downstream processing.

## Run locally

```bash
python scripts/update_dashboard_data.py
```

Optional: provide a GitHub token for higher API quota:

```bash
GITHUB_TOKEN=ghp_xxx python scripts/update_dashboard_data.py
```

## Dashboard usage

Open `dashboard/index.html` from any static server and use filters for:

- language (multi-select)
- participant
- repository search
- score range
- minimum language sample size

The dashboard computes filtered KPIs and per-language rankings live.
