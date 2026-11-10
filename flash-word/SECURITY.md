# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.1.x | Yes |
| < 1.1 | No |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting for this repository: **Security** tab, then **Report a vulnerability**. Include the plugin version, the Open WebUI version, the steps to reproduce and the impact you observed. Reports are handled on a best effort basis.

## Security model

This plugin is an Open WebUI **Action**. Its code runs inside the Open WebUI backend with the privileges of the Open WebUI process, like every function an administrator installs. Only install it from a source you trust and check the SHA-256 published in the [changelog](CHANGELOG.md).

### What the plugin does

- Reads the clicked chat message (and optionally a few previous ones) from the request sent by Open WebUI.
- Converts its Markdown into a `.docx` in memory with `python-docx`, and into an HTML preview.
- Returns a card with the preview and the file embedded as a `data:` link, displayed by Open WebUI in a sandboxed iframe.

### What the plugin does not do

- **No model call**: the conversation text is not sent to any model or service.
- No outbound network call, no CDN, no web font, no image download (Markdown images are replaced by their caption).
- No file written on disk, no upload to Open WebUI's file store.
- No call to the Open WebUI API from the browser, no use of `localStorage`, no access to the user token.
- No JavaScript at all in the card, and none executed in the main Open WebUI page.
- No package installation: there is no `requirements:` line; `python-docx` is already part of Open WebUI.

### Controls

| Risk | Control |
|---|---|
| HTML or script injection in the preview | Every text is escaped with `html.escape`. HTML tags found in the Markdown are removed (only `<br>` is interpreted). Code blocks are escaped and shown as text. |
| Dangerous links | Only `http`, `https` and `mailto` URLs become hyperlinks in the `.docx`; `javascript:`, `data:`, `file:` and any other scheme stay plain text. In the preview, links are never clickable. |
| Malformed XML in the `.docx` | Characters that are not valid in XML are removed before conversion. Texts go through `python-docx`, which escapes XML. Elements added directly in XML are inserted in the order required by the schema. |
| Hidden content leaking into the document | `<details>` blocks (reasoning, tool calls) and `<think>` blocks are removed before conversion. |
| Data leakage through logs | Logs contain counts (sections, tables, words), file size, duration and exception class names only. The text is never logged. |
| Shared state between users | Settings are copied at the start of each run; no request data is stored on the shared module. |
| Document metadata | The `.docx` properties contain the title, "Flash Word" as author and the language; no user name or account information. |

### Content Security Policy

The card contains no script. It uses an inline `<style>` block: under a strict `IFRAME_CSP` that forbids inline styles, the card loses its layout but the text and the download link remain.

### Data handling

- The card and the embedded `.docx` are stored by Open WebUI inside the chat message (message embeds), about 60 KB for a one-page document. They contain the exported text.
- In Open WebUI 0.13.0, deleting a chat deletes its messages and embeds (`ON DELETE CASCADE`), so the instance retention policy applies to the documents as well.
- Each click adds an embed to the message; previous cards remain in the database until the chat is deleted.

## Recommendations for administrators

- Keep **Allow Same Origin** disabled for embed iframes unless another tool requires it: with it enabled, a script inside an embed can reach the parent page. This plugin contains no script.
- Restrict who can create or edit functions in Open WebUI to administrators.
