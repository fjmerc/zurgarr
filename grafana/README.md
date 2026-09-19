# Zurgarr Grafana dashboard

A canned dashboard for zurgarr's built-in Prometheus exporter
(`/metrics` on the Status UI port). Five rows: Overview (service/process
health), Debrid Quota & Expiry, Pipeline (blackhole outcomes by status,
symlink/timeout/rejection counters, events, network), System resources,
and Mount state timelines.

## 1. Scrape zurgarr from Prometheus

`/metrics` is served by the Status UI (`STATUS_UI_ENABLED=true`,
default port 8080) and is **deliberately unauthenticated** (scrapers
can't do the dashboard's basic auth), so the scrape config needs no
credentials:

```yaml
scrape_configs:
  - job_name: zurgarr
    scrape_interval: 60s
    static_configs:
      - targets: ['zurgarr:8080']   # container name : STATUS_UI_PORT
```

Keep `job_name: zurgarr` — the dashboard's "Zurgarr" up-stat queries
`up{job="zurgarr"}` (Prometheus's own scrape-health series, which goes
red when zurgarr is down; the exporter's `zurgarr_up` would merely go
stale). If you use a different job name, edit that one panel.

Most gauges update per scrape; the debrid quota gauges update when the
`debrid_quota_poll` task sweeps (every 6 h by default) and are absent
until its first run — panels fill in after that.

## 2. Import the dashboard

Grafana → Dashboards → **New → Import** → upload
`zurgarr-dashboard.json` → pick your Prometheus datasource when
prompted → Import. The dashboard uid is `zurgarr`, so re-importing
updates in place.

## Keeping it honest

`tests/test_grafana_dashboard.py` cross-checks every metric the panels
query against what `utils/metrics.py` actually emits (both directions),
so a renamed or added metric fails CI until the dashboard catches up.
If you add a metric that deliberately has no panel, list it in that
test's `ALLOWED_UNUSED` with a comment.
