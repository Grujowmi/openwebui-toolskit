# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting for this repository: **Security** tab, then **Report a vulnerability**. Include the plugin version, the Open WebUI version, the steps to reproduce and the impact you observed. Reports are handled on a best effort basis.

## Security model

This plugin is an Open WebUI **Action**. Its code runs inside the Open WebUI backend with the privileges of the Open WebUI process, like every function an administrator installs. Only install it from a source you trust and check the SHA-256 published in the [changelog](CHANGELOG.md).

### What the plugin does

- Reads the clicked chat message (and optionally a few previous ones) from the request sent by Open WebUI.
- Sends that text to a model through Open WebUI's internal `generate_chat_completion`, on behalf of the user who clicked. Open WebUI's model access control applies.
- Validates the JSON deck returned by the model, builds a `.pptx` in memory with `python-pptx` and returns an HTML preview with the file embedded as a `data:` link, displayed by Open WebUI in a sandboxed iframe.

### What the plugin does not do

- No outbound network call of its own, no CDN, no web font, no image download.
- No file written on disk, no upload to Open WebUI's file store.
- No call to the Open WebUI API from the browser, no use of `localStorage`, no access to the user token.
- No JavaScript executed in the main Open WebUI page (`__event_call__` with `execute` is not used).
- No package installation: there is no `requirements:` line; `python-pptx` is already part of Open WebUI.

### Controls

| Risk | Control |
|---|---|
| HTML or script injection through model output | Every text placed in the preview is escaped with `html.escape`. Texts written to the `.pptx` go through `python-pptx`, which escapes XML. No model content is inserted with `innerHTML`; the slider script only writes a counter with `textContent`. |
| Prompt injection from the chat text | The text is wrapped in `<texte>` tags and declared as data. The answer must match a strict JSON structure; unknown layouts fall back to a plain list, invalid slides are dropped. This reduces the risk, it does not remove it: the deck is a draft to review. |
| Invented figures | The figures guard removes any key figure or chart whose numbers are not present in the source text, and tells the user. See [docs/GUIDE.md](docs/GUIDE.md#the-figures-guard). |
| Oversized or malformed output | Input size limit, slide limit (15 maximum), per-field length limits, one retry only, timeout on each model call. |
| Server responsiveness | The `.pptx` is built in a worker thread. |
| Data leakage through logs | Logs contain durations, counts, file size and exception class names only. Conversation text and model output are never logged. |
| Shared state between users | Settings are copied at the start of each run; no request data is stored on the shared module. |
| Document metadata | The `.pptx` properties contain the deck title and "Flash Powerpoint" as author; no user name or account information. |

### Inline script

The preview contains a small inline script for the slider controls (arrows, dots, counter, keyboard). It makes no network request, reads no data outside its own iframe and writes text only with `textContent`. Without it, the slides remain readable and the download link still works.

### Content Security Policy

The preview uses inline styles. Under a strict `IFRAME_CSP` that forbids inline styles, the preview layout degrades; under a CSP that forbids inline scripts, only the slider controls disappear.

### Data handling

- The preview and the embedded `.pptx` are stored by Open WebUI inside the chat message (message embeds), about 120 KB for a 9-slide deck. They contain text derived from the conversation.
- In Open WebUI 0.13.0, deleting a chat deletes its messages and embeds (`ON DELETE CASCADE`), so the instance retention policy applies to the decks as well.
- Each click adds an embed to the message; previous decks remain in the database until the chat is deleted.

## Recommendations for administrators

- Keep **Allow Same Origin** disabled for embed iframes unless another tool requires it: with it enabled, a script inside an embed can reach the parent page. This plugin does not use that access.
- Restrict who can create or edit functions in Open WebUI to administrators.
- Prefer a local or contractually covered model for `MODEL_ID` when conversations contain sensitive data.
- Remind users that a generated deck must be reviewed before it is shared.
