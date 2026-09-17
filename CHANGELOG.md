# Changelog

## [Unreleased]

### Added
- Navigation links in dashboard header: TeslaMate home, Dashboards (tag `tesla`), Custom Dashboards (tag `TeslamateCustomDashboards`)
- TCO dashboard (`grafana/ev_tco_dashboard.json`): depreciation, fuel/electricity, maintenance, insurance and road tax compared between the EV and an equivalent gasoline car, using `depreciation_curve` for current market value

### Fixed
- Date column in trips table now sorts correctly and displays in local format instead of raw string

## [1.0.0] - 2026-03-18

Initial release.

- Sidecar Docker container that fetches weekly gasoline prices from the Italian MASE/SISEN API
- Prices stored in TeslaMate's PostgreSQL database (`fuel_prices` table)
- Configurable sync schedule (default: every Tuesday at 15:00)
- Historical backfill on first start (configurable start date)
- Grafana dashboard with total savings, weekly/monthly trends and per-trip breakdown
- Per-trip electricity cost based on real TeslaMate charging data, with configurable fallback price
- Dashboard variables: car selection, ICE consumption (L/100km), electricity price fallback
