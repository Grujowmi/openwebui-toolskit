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
- Sends that text to a model through Open WebUI's internal `generate_chat_completion`, on behalf of the user who clicked. Open WebUI's model access control applies: the user must be allowed to use the model.
- Validates the JSON returned by the model, computes a layout and returns an HTML page containing an SVG, displayed by Open WebUI as an embed in a sandboxed iframe.

### What the plugin does not do

- No outbound network call of its own, no CDN, no web font, no external library.
- No file written on disk, no upload to Open WebUI's file store.
- No call to the Open WebUI API from the browser, no use of `localStorage`, no access to the user token.
- No JavaScript executed in the main Open WebUI page (`__event_call__` with `execute` is not used).
- No package installation: there is no `requirements:` line in the header.

### Controls

| Risk | Control |
|---|---|
| HTML or script injection through model output | Every text coming from the model is escaped with `html.escape` before being placed in SVG or HTML. No model content is inserted with `innerHTML`. |
| Prompt injection from the chat text | The text is wrapped in `<texte>` tags and declared as data in the system prompt. The answer must match a strict JSON schema; anything else is rejected. This reduces the risk, it does not remove it: the diagram is a drawing aid and must be read critically. |
| Oversized or malformed output | Input size limit (`MAX_INPUT_CHARS`), node limit (`MAX_NODES`, 60 maximum), label length limits, one retry only, timeout on each model call. |
| Data leakage through logs | Logs contain durations, counts, layout direction and exception class names only. Conversation text and model output are never logged. |
| Shared state between users | The module is shared by all requests; settings are copied at the start of each run and no request data is stored on the instance. |

### Inline script

When `ENABLE_EXPORT` is true, the page contains a small inline script that only draws the SVG on a canvas to produce the PNG download. It makes no network request and reads no data outside its own iframe. With `ENABLE_EXPORT` set to false, the page contains no script at all.

### Content Security Policy

The diagram is drawn with SVG presentation attributes. It stays readable under a strict `IFRAME_CSP` that blocks inline scripts and styles; only the PNG button and the dark variant would be disabled.

### Data handling

- The generated page is stored by Open WebUI inside the chat message (message embeds) in the database. It contains the diagram labels, which come from the conversation.
- In Open WebUI 0.13.0, deleting a chat deletes its messages and embeds (`ON DELETE CASCADE`), so the instance retention policy applies to the diagrams as well.
- Each click adds an embed to the message; previous diagrams remain in the database until the chat is deleted.

## Recommendations for administrators

- Keep **Allow Same Origin** disabled for embed iframes unless another tool requires it: with it enabled, a script inside an embed can reach the parent page. This plugin does not use that access.
- Restrict who can create or edit functions in Open WebUI to administrators.
- Prefer a local or contractually covered model for `MODEL_ID` when conversations contain sensitive data.
