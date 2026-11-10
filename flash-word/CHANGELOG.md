# Changelog

All notable changes to this plugin are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-06

SHA-256 of `flash_word.py`: `fd4957e69814c24f3526741678853fde981a56d0817791fa128ba4eebf032659`

### Added
- Document preview in the chat card: a white page rendered from the same parsed Markdown as the `.docx`, at most 440 px high, with its own scroll bar and a bottom fade.

### Changed
- The card is now tall enough for Open WebUI's automatic scroll to land on it; with the small card of 1.0.0, users had to scroll back to find the download button.
- Heading chips removed from the card, replaced by the preview.

## [1.0.0] - 2026-11-06

### Added
- First release: one Action button that converts a chat answer into a `.docx`, without any model call.
- Markdown support: headings, paragraphs, emphasis, inline code, links, two-level bulleted and numbered lists, task lists, tables with alignment, code blocks, quotes, horizontal rules.
- Word layout: A4, Calibri, built-in heading styles, real Word numbering restarted per list, tables with repeated header row, header with title and date, footer with page numbers.
- Removal of reasoning and tool blocks, link scheme allowlist (`http`, `https`, `mailto`).
- In-memory generation and download through an embedded `data:` link; nothing written on the server.
- Start and finish notifications, status line.
- Valves: `priority`, `MESSAGE_COUNT`, `HEADER_FOOTER`, `MIN_TEXT_LENGTH`, `LANGUAGE`, `SHOW_STATUS`.
