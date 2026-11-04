# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-04

First public release.

### Added
- Periodic email report to administrators (daily, weekly on Monday, monthly on the 1st), covering the previous full period.
- SMTP relay valves: host, port, `starttls`, `ssl` or `none`, certificate verification, optional authentication, password from the `HELIOS_SMTP_PASSWORD` environment variable.
- Recipient check: only addresses of Open WebUI accounts with the `admin` role receive the report. Others are skipped and shown on the page.
- One send per period, kept across restarts. Redis claim when Redis is available, 12 hour catch-up window.
- "Send test report" button (last 7 days) and last-send status on the page.
- Report in English or French.

## [0.3.0] - 2026-11-04

### Added
- Dashboard: period filters, estimated cost, daily chart (inline SVG, no external resources), per-model and per-user tables, CSV export.
- `price_map` with longest-prefix matching on the model, then on its base model.
- Flags for replies without token count and for models without a price.
- Optional pseudonymization of user names (salted SHA-256).
- Light and dark themes, responsive layout.

## [0.2.1] - 2026-11-04

### Fixed
- After a code update, routes of the previous version kept being served. Open WebUI loads each version under a new module name, so routes are now tagged and the stale ones removed.

## [0.2.0] - 2026-11-04

### Added
- Own `helios` schema with `helios.usage` and `helios.state`.
- Collection of token counts from `chat_message`, idempotent, with backfill of existing replies, late-count completion and retention purge (`retention_months`).

## [0.1.2] - 2026-11-04

### Changed
- The page uses the Open WebUI favicon.

## [0.1.1] - 2026-11-04

### Changed
- English code and valves, bilingual page (`ui_language`), diagnostics for the background loop.

## [0.1.0] - 2026-11-04

### Added
- Skeleton: valves, administrator-only page, status API, background loop.
