# Flash Powerpoint: user and admin guide

This guide covers how the action works, the slide types, the configuration, checks after installation and troubleshooting.

## Contents

- [How it works](#how-it-works)
- [Slide types](#slide-types)
- [The figures guard](#the-figures-guard)
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

The action runs entirely inside the Open WebUI backend.

1. **Text selection.** The clicked answer is located by its message id (fallback: the last message). With `MESSAGE_COUNT` greater than 1, the preceding messages are included with their role. HTML blocks left by other plugins and `<details>` blocks are removed; the text is cut at `MAX_INPUT_CHARS`.
2. **Structure by the model.** The text is sent through Open WebUI's internal `generate_chat_completion`, on behalf of the user who clicked, wrapped in `<texte>` tags and declared as data. The model returns a JSON deck: a title, a subtitle and a list of slides, each with a `layout`, its content and speaker `notes`. If the text has no substance, the model answers `{"no_content": true}`.
3. **Validation.** `<think>` blocks are stripped, the first balanced JSON object is parsed and every slide is checked against its layout rules (see below). Texts are cleaned of Markdown residue and cut to fixed lengths. Invalid slides are dropped; if no usable slide remains, the model gets **one** retry with an error message written by the plugin.
4. **One layout, two renderings.** A single scene is computed: every element (shape, text box, chart) is positioned in inches on a 16:9 slide (13.333 x 7.5 in), and font sizes are fitted to their boxes by estimation. The scene is rendered twice:
   - into a `.pptx` with `python-pptx`, in a worker thread so the server stays responsive;
   - into HTML for the slider preview, using the same positions and sizes.
5. **Delivery.** The preview and the `.pptx` (embedded as a `data:` link) are returned as an inline `HTMLResponse`, displayed by Open WebUI as an embed.

## Slide types

| Layout | Content | Rules |
|---|---|---|
| Cover | Title, subtitle, date | Always generated first. |
| `bullets` | Title and 1 to 5 points | 1 to 3 points are shown as cards, 4 to 5 as a list. |
| `columns` | Two columns with a heading and 1 to 4 points each | If only one column is filled, the slide becomes a `bullets` slide. |
| `stats` | 1 to 4 key figures with a label, optional caption | Each figure must be found in the source text. |
| `chart` | 2 to 8 categories and values, unit, optional caption | Native clustered column chart, editable in PowerPoint. All values must be found in the source text. |
| `steps` | 2 to 5 numbered steps with a title and a short text | Horizontal timeline. |
| `message` | One strong sentence | Large quotation layout. |
| `closing` | "Takeaways" with 2 to 4 points | Always moved to the end; only one is kept. |

Text limits applied during validation: deck title 70 characters, subtitle 120, slide title 70, point 110 (100 in columns), column heading 40, figure 14, figure label 60, caption 160, step title 40, step text 120, key message 180, notes 700.

Design: white content slides with a vertical indigo to cyan accent strip, title with an accent bar, footer with the deck title and slide number; gradient cover and closing slides. Light theme only.

## The figures guard

Local models can invent figures. To prevent this:

- every number of the source text is collected, with its usual written variants (`1 240`, `1,240`, `12,5` and `12.5`);
- on a `stats` slide, a figure whose number is not found in the source is removed;
- on a `chart` slide, if any value is not found, the whole slide is removed;
- when slides are removed, the user gets a notification with the number of removed slides.

The guard checks that numbers exist in the source, not that they are used correctly. The deck remains a draft to review before it is presented.

## Installation and update

1. **Admin Panel > Functions**, create a new function (or open the existing one to update it).
2. Paste the content of `flash_powerpoint.py` and save. The function id is derived from the title (`flash_powerpoint`).
3. Enable it and mark it as **Global**, or attach it to specific models.
4. Verify the file you deployed:

   ```bash
   sha256sum flash_powerpoint.py
   ```

   Compare with the hash published in [CHANGELOG.md](../CHANGELOG.md).

`python-pptx` is shipped with Open WebUI. To check it in your container:

```bash
docker exec <open-webui-container> python3 -c "import pptx; print(pptx.__version__)"
```

If Open WebUI cannot load the module, it deactivates the function automatically. Check that the function is still active after saving.

## Configuration

| Valve | Default | Bounds / values | Effect |
|---|---|---|---|
| `priority` | `0` | integer | Order of the button among actions. |
| `MODEL_ID` | empty | model id | Model used to structure the deck. Empty = model of the conversation. The user must have access to this model. |
| `MAX_SLIDES` | `10` | 3 to 15 | Maximum number of slides after the cover. The model adapts the count to the text. |
| `SPEAKER_NOTES` | `true` | boolean | Writes the notes produced by the model into each slide (cover notes = subtitle). |
| `MESSAGE_COUNT` | `1` | 1 or more | Number of recent messages used. |
| `MAX_INPUT_CHARS` | `12000` | 500 minimum | Text size sent to the model. |
| `MIN_TEXT_LENGTH` | `150` | characters | Shorter texts are rejected with a notification, without calling the model. |
| `TIMEOUT_SECONDS` | `180` | 10 minimum | Timeout of each model call. |
| `LANGUAGE` | `fr` | `fr`, `en` (slides also `es`, `de`, `it`) | Language requested for the slides, and language of the interface messages, date and labels. Interface texts exist in French and English; other values fall back to English. |
| `SHOW_STATUS` | `true` | boolean | Shows progress in the chat status line. |

## What the user sees

- A notification "generation started", then a status line.
- A card with the deck title, the number of slides and the **Download PowerPoint** button.
- The slider: one slide at a time, arrows, dots, counter. Arrow keys work once the slider has focus; swipe and mouse wheel work too.
- A notification "generation finished", and a warning if slides were removed by the figures guard.

Without JavaScript (for example under a strict CSP), the slider is still a horizontal strip that scrolls slide by slide; only the arrows, dots and counter disappear.

The downloaded file name is derived from the deck title, for example `bilan-2025-du-service-de-radiotherapie.pptx` for the title "Bilan 2025 du service de radiothérapie". Document properties: title = deck title, author = "Flash Powerpoint".

## Data lifecycle

- The `.pptx` is built **in memory** and never written to disk. The plugin makes no network call of its own; the only outgoing request is the model call made by Open WebUI.
- The preview, with the `.pptx` embedded in base64, is stored by Open WebUI inside the chat message (message embeds). Measured size: about 120 KB for a 9-slide deck (61 KB `.pptx`).
- In Open WebUI 0.13.0, deleting a chat deletes its messages and embeds (`ON DELETE CASCADE`): any chat retention or pruning policy also cleans the decks.
- Each click **adds** an embed to the message; previous decks remain in the database until the chat is deleted.

## Logs

One line per run, metadata only:

```
flash_powerpoint: ok slides=9 dropped=0 size=61ko duration=24.3s
flash_powerpoint: no_content duration=4.0s
flash_powerpoint: structure invalide (tentative 1) : ...
flash_powerpoint: timeout duration=180.0s
flash_powerpoint: erreur <ExceptionClass> status=<code> duration=...
```

The validation message describes the structure problem, never the conversation content. To follow a test run:

```bash
docker logs -f --since 1m <open-webui-container> 2>&1 | grep --line-buffered flash_powerpoint
```

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| No button under the answers | Function inactive, not global, or not assigned to the model | Check the function in Admin > Functions. |
| Function deactivated right after saving | Load error | Paste the file again, compare the SHA-256, read the server logs. |
| "Text too short" | Answer shorter than `MIN_TEXT_LENGTH` | Expected behaviour. |
| "Not enough content" | The model found nothing to present | Expected for very thin answers. |
| "The model did not return a usable structure" | Invalid JSON or no valid slide, twice | Retry or set `MODEL_ID` to a stronger model. |
| "N slide(s) removed" | Figures not found in the text | Expected: the guard removed invented figures. |
| "Timed out" | Slow model or long text | Raise `TIMEOUT_SECONDS`, lower `MAX_INPUT_CHARS`, or use a faster model. |
| PowerPoint offers to repair the file | Unexpected | Open an issue with the plugin version, the PowerPoint version and the file if it contains no sensitive data. |
| Text slightly overflowing a box | Font metrics differ from the estimation (for example on macOS) | Shorten the text in PowerPoint; the boxes are editable. |
| Download does nothing | Iframe sandbox without downloads, or settings blocking `data:` links | Check the iframe settings in Open WebUI. |

## Test checklist

1. **Rich text**: an answer with figures, a procedure and a before/after comparison. Expect key figures, a chart, steps, columns and a closing slide.
2. **No figures**: a short explanatory paragraph. Expect no key-figures or chart slide (or a removal warning).
3. **Short text**: fewer than 150 characters. Expect "text too short".
4. **Long text**: more than 10,000 characters. Expect a deck, text cut at `MAX_INPUT_CHARS`.
5. **Small model**: set `MODEL_ID` to a small model. Expect a deck, or one retry then a clean error.
6. **Slider**: arrows, dots, keyboard, swipe.
7. **Download**: open the `.pptx` in PowerPoint; no repair prompt, chart editable (Edit Data), notes visible.
8. **Non-admin user**: button visible and working.
9. **Reload**: the preview is still there after reloading the page.

## Compatibility

- Developed and tested on **Open WebUI 0.13.0**.
- The header declares `required_open_webui_version: 0.11.0` because Actions can return inline `HTMLResponse` embeds since 0.11. Versions 0.11 and 0.12 were not tested.
- Tested with `python-pptx` 0.6.21 and 1.0.2. Files checked by reopening them with `python-pptx` and rendering them with LibreOffice.

## Known limitations

- Font sizes are fitted by estimation, without the real font metrics.
- Segoe UI is a Windows font; PowerPoint for Mac substitutes it.
- No photos or external icons: the design relies on typography, colours and shapes.
- The preview is faithful to the `.pptx` layout but line breaks can differ slightly, since the browser and PowerPoint do not use the same text engine.
- Open WebUI scrolls the page to the embed when it arrives; this is controlled by Open WebUI.
