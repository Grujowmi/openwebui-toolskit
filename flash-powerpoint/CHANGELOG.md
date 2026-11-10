# Changelog

All notable changes to this plugin are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-06

SHA-256 of `flash_powerpoint.py`: `2f260bbc7b2d46164e847ab6e12dec8efd33fec8931da8ca5dd372d29e31c997`

### Added
- First release: one Action button that turns a chat answer into a 16:9 PowerPoint deck.
- Deck structure produced by the conversation model (or `MODEL_ID`), strict validation, one retry on invalid output, `no_content` answer for thin texts.
- Eight slide types: cover, key points, two columns, key figures, native column chart, numbered steps, key message, takeaways.
- Figures guard: key figures and chart values must appear in the source text.
- Speaker notes on every slide (`SPEAKER_NOTES`).
- Single layout rendered twice: `.pptx` with `python-pptx` and an HTML slider preview with arrows, dots, counter and keyboard navigation.
- In-memory generation and download through an embedded `data:` link; nothing written on the server.
- Start and finish notifications, status line, warning when slides are removed.
- Valves: `priority`, `MODEL_ID`, `MAX_SLIDES`, `SPEAKER_NOTES`, `MESSAGE_COUNT`, `MAX_INPUT_CHARS`, `MIN_TEXT_LENGTH`, `TIMEOUT_SECONDS`, `LANGUAGE`, `SHOW_STATUS`.
