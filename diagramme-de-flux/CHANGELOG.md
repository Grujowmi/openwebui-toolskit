# Changelog

All notable changes to this plugin are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.1] - 2026-11-06

SHA-256 of `diagramme_de_flux.py`: `08f09fe6ab961d4a532e94d27e9aef69e3c1ddcdd59e0890fad899a251690bf7`

### Added
- Start and finish notifications ("generation started", "generation finished"), consistent with the other actions.

### Changed
- The downloaded SVG now has an opaque white background behind the rounded card, so the corners are no longer transparent in dark viewers.

## [1.1.0] - 2026-11-06

### Changed
- Visual redesign: card on a dotted background, soft shadows, gradient start and end pills, gold decision diamonds, step cards with an accent bar.
- Yes/No edge labels are colour coded (green, red, grey for other labels).
- Loops are drawn as dashed indigo edges on a side lane.
- Title with a summary line (steps, decisions, loops).
- Download buttons moved under the diagram.

### Fixed
- Missing French accents in status messages.

## [1.0.0] - 2026-11-06

### Added
- First release: Action button that turns a chat answer into a flowchart.
- JSON extraction by the conversation model (or `MODEL_ID`), strict validation, one retry on invalid JSON.
- Layered layout in pure Python: cycle detection, longest-path layers, dummy nodes, barycentre crossing reduction, side lane for loops.
- SVG rendering with standard shapes, readable without JavaScript.
- SVG and PNG downloads (`ENABLE_EXPORT`).
- Valves: `priority`, `MODEL_ID`, `DIRECTION`, `MAX_NODES`, `MESSAGE_COUNT`, `MAX_INPUT_CHARS`, `MIN_TEXT_LENGTH`, `TIMEOUT_SECONDS`, `LANGUAGE`, `ENABLE_EXPORT`, `SHOW_STATUS`.
