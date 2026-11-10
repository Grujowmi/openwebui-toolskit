# Diagramme de flux automatique: user and admin guide

This guide covers how the action works, how to configure it, how to check it after installation and how to troubleshoot it.

## Contents

- [How it works](#how-it-works)
- [Installation and update](#installation-and-update)
- [Configuration](#configuration)
- [What the user sees](#what-the-user-sees)
- [Data lifecycle](#data-lifecycle)
- [Logs](#logs)
- [Troubleshooting](#troubleshooting)
- [Test checklist](#test-checklist)
- [Compatibility](#compatibility)
- [Known limitations](#known-limitations)

## How it works

The action runs entirely inside the Open WebUI backend, in four steps.

1. **Text selection.** The clicked answer is located by its message id (fallback: the last message). With `MESSAGE_COUNT` greater than 1, the preceding messages are included with their role. HTML blocks left by other plugins and `<details>` blocks are removed, and the text is cut at `MAX_INPUT_CHARS`.
2. **Extraction by the model.** The text is sent to the model through Open WebUI's internal `generate_chat_completion`, on behalf of the user who clicked. It is wrapped in `<texte>` tags and declared as data, so instructions it may contain are not to be followed. The model must answer with a strict JSON graph:

   ```json
   {"title": "...", "direction": "TB",
    "nodes": [{"id": "n1", "label": "...", "type": "start"}],
    "edges": [{"from": "n1", "to": "n2", "label": null}]}
   ```

   Node types: `start`, `end`, `process`, `decision`, `io`, `note`. If the text describes no process, the model answers `{"no_process": true}`.
3. **Validation.** `<think>` blocks and code fences are stripped, the first balanced JSON object is parsed, then checked: unique ids, edges pointing to existing nodes, at least one `start` node, at least two nodes and one edge. Unknown types become `process`, self loops and duplicate edges are dropped, labels are cut at 60 characters (edge labels at 20). If the JSON is invalid, the model gets **one** retry with an error message written by the plugin; after that, a clean error is shown.
4. **Layout and rendering.** A layered layout is computed in pure Python:
   - back edges (cycles) detected by depth-first search and routed on a side lane;
   - layers by longest path, dummy nodes for long edges;
   - crossing reduction by barycentre ordering;
   - coordinates, distributed edge ports and horizontal routing tracks.

   The result is an SVG drawn with presentation attributes (no external resource, no web font), wrapped in a small HTML page and returned as an inline `HTMLResponse`, which Open WebUI displays as a Rich UI embed.

## Installation and update

1. **Admin Panel > Functions**, create a new function (or open the existing one to update it).
2. Paste the content of `diagramme_de_flux.py` and save. The function id is derived from the title (`diagramme_de_flux_automatique`).
3. Enable it and mark it as **Global**, or attach it to specific models.
4. Verify the file you deployed:

   ```bash
   sha256sum diagramme_de_flux.py
   ```

   Compare with the hash published in [CHANGELOG.md](../CHANGELOG.md).

If Open WebUI cannot load the module (syntax error, damaged paste), it deactivates the function automatically. Check that the function is still active after saving.

## Configuration

| Valve | Default | Bounds / values | Effect |
|---|---|---|---|
| `priority` | `0` | integer | Order of the button among actions (lower = further left). |
| `MODEL_ID` | empty | model id | Model used for extraction. Empty = model of the conversation. The user must have access to this model. |
| `DIRECTION` | `AUTO` | `AUTO`, `TB`, `LR` | `AUTO` follows the model's choice (`TB` by default, `LR` for long linear sequences). |
| `MAX_NODES` | `30` | 3 to 60 | Beyond this, the graph is truncated and a warning is shown. |
| `MESSAGE_COUNT` | `1` | 1 or more | Number of recent messages used. |
| `MAX_INPUT_CHARS` | `8000` | 500 minimum | Text size sent to the model. |
| `MIN_TEXT_LENGTH` | `80` | characters | Shorter texts are rejected with a notification, without calling the model. |
| `TIMEOUT_SECONDS` | `120` | 10 minimum | Timeout of each model call. |
| `LANGUAGE` | `fr` | `fr`, `en` (labels also `es`, `de`, `it`) | Language of the labels requested from the model and of the interface messages. Interface messages exist in French and English; other values fall back to English. |
| `ENABLE_EXPORT` | `true` | boolean | Shows the SVG and PNG download buttons. |
| `SHOW_STATUS` | `true` | boolean | Shows progress in the chat status line. |

**Model choice.** Mid-size instruction models produce the most reliable JSON. Small models may need the retry more often. Reasoning models work: their `<think>` output is removed before parsing.

## What the user sees

- A notification "generation started", then a status line during the work.
- The diagram as a card under the answer: title, summary (steps, decisions, loops), the diagram itself, and the download buttons underneath.
- A notification "generation finished".

Visual rules:

| Element | Rendering |
|---|---|
| Start, end | Green and red gradient pills |
| Step | White card with an indigo accent bar |
| Decision | Gold diamond |
| Input / output | Light blue parallelogram |
| Note | Dashed card |
| Yes / No labels | Green / red pills (other labels in grey) |
| Loop | Dashed indigo edge on a side lane |

In the chat, the diagram follows the operating system's light or dark preference (`prefers-color-scheme`). Downloads are always light, on an opaque white background.

**Downloads.** The SVG button is a plain link (works without JavaScript). The PNG button appears only if the small inline script runs; it draws the SVG on a canvas at twice the size and saves it. Nothing goes over the network.

## Data lifecycle

- The plugin writes **no file** on the server and makes **no network call** of its own. The only outgoing request is the model call, made by Open WebUI to the model configured in the instance.
- The generated HTML is stored by Open WebUI in the chat message (the message `embeds`), in the database.
- In Open WebUI 0.13.0, deleting a chat deletes its messages and their embeds (foreign key with `ON DELETE CASCADE`). Any chat retention or pruning policy therefore also cleans the diagrams.
- Each click **adds** an embed to the message. The screen shows the latest one; the database keeps the previous ones until the chat is deleted.

## Logs

The plugin logs one line per run, with metadata only:

```
flowchart: ok nodes=8 edges=9 dir=TB duration=6.2s
flowchart: no_process duration=3.1s
flowchart: schema invalide (tentative 1) : ...
flowchart: timeout duration=120.0s
flowchart: erreur <ExceptionClass> status=<code> duration=...
```

The validation error message describes the structure problem (for example a missing id), never the conversation content. To follow a test run:

```bash
docker logs -f --since 1m <open-webui-container> 2>&1 | grep --line-buffered flowchart
```

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| No button under the answers | Function inactive, not global, or not assigned to the model | Check the function state in Admin > Functions. |
| Function deactivated right after saving | Load error (incomplete paste) | Paste the file again, compare the SHA-256, read the server logs. |
| "Text too short" | Answer shorter than `MIN_TEXT_LENGTH` | Expected behaviour; lower the valve if needed. |
| "No process detected" | The model found no process in the text | Expected for descriptive answers. |
| "The model did not return a usable schema" | Invalid JSON twice in a row | Retry, or set `MODEL_ID` to a stronger model. |
| "Timed out" | Slow model or long text | Raise `TIMEOUT_SECONDS`, lower `MAX_INPUT_CHARS`, or use a faster model. |
| Download does nothing | Iframe sandbox without downloads allowed, or script blocked by a CSP | Check the iframe settings in Open WebUI; the SVG link does not need JavaScript. |
| Diagram appears again several times after a page reload | Each click adds an embed | Expected Open WebUI behaviour. |

## Test checklist

Run these after installing or updating:

1. **Linear process**: a 5-step installation procedure. Expect a vertical sequence from start to end.
2. **Decisions**: a text with "if ... otherwise". Expect diamonds and Yes/No labels.
3. **Loop**: a text with "repeat until". Expect a dashed edge on the side.
4. **No process**: a general knowledge answer. Expect the "no process" notification and nothing added.
5. **Short text**: fewer than 80 characters. Expect "text too short".
6. **Invalid JSON**: set `MODEL_ID` to a small model. Expect a valid diagram or one retry then a clean error.
7. **Theme**: switch the operating system between light and dark. Shapes keep readable colours.
8. **Downloads**: SVG and PNG, check the white background.
9. **Non-admin user**: the button must be visible and work.
10. **Reload**: the diagram is still there after reloading the page.

## Compatibility

- Developed and tested on **Open WebUI 0.13.0**.
- The header declares `required_open_webui_version: 0.11.0` because Actions can return inline `HTMLResponse` embeds since 0.11. Versions 0.11 and 0.12 were not tested.
- Python standard library only, plus `fastapi` and `pydantic`, both part of Open WebUI.

## Known limitations

- Text width is estimated from a character width table, not measured with the real font. Very long labels may come close to the shape borders.
- Graphs with many loops can show edge crossings.
- The in-chat theme follows the operating system preference, not the Open WebUI theme switch, which is not passed to the embed iframe.
- Open WebUI scrolls the page to the embed when it arrives; this behaviour is controlled by Open WebUI, not by the plugin.
