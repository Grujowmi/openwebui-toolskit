# Flash Word: user and admin guide

This guide covers how the action works, the supported Markdown, the document layout, the configuration, checks after installation and troubleshooting.

## Contents

- [How it works](#how-it-works)
- [Supported Markdown](#supported-markdown)
- [Document title](#document-title)
- [Document layout](#document-layout)
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

The action runs entirely inside the Open WebUI backend and **never calls a model**.

1. **Text selection.** The clicked answer is located by its message id (fallback: the last message). With `MESSAGE_COUNT` greater than 1, the preceding messages are exported too, each introduced by a "Question" or "Answer" label. The previous user message is also read to propose a title.
2. **Cleanup.** `<details>` blocks (reasoning, tool calls), `<think>` blocks, HTML blocks left by other plugins and characters that are not valid in XML are removed.
3. **Parsing.** The Markdown is parsed into blocks (headings, paragraphs, lists, tables, code, quotes, rules), then into inline runs (bold, italic, code, links).
4. **Rendering.** The same parsed blocks are rendered twice:
   - into a `.docx` with `python-docx`, in a worker thread;
   - into an HTML preview shown in the chat card.
5. **Delivery.** The card, with the `.docx` embedded as a `data:` link, is returned as an inline `HTMLResponse` and displayed by Open WebUI as an embed.

## Supported Markdown

| Element | Support |
|---|---|
| Headings `#` to `######` | Mapped to Word Heading 1 to 4. Levels are shifted so that the highest level used becomes Heading 1. |
| Paragraphs | A single line break inside a paragraph is kept as a line break. |
| Bold, italic, bold italic, strikethrough | `**`, `__`, `*`, `_`, `***`, `~~`. Underscores inside words (`file_name.py`) and isolated asterisks (`5 * 3`) are kept as text. |
| Inline code | Consolas, light grey background. |
| Links | `[text](url)`, `<url>` and bare `https://` addresses. Only `http`, `https` and `mailto` become clickable; other schemes are shown as plain text. |
| Bulleted and numbered lists | `-`, `*`, `+`, `•`, `1.`, `1)`. Two levels; deeper items are shown at level 2. Numbering restarts for each list and keeps the start number. |
| Task lists | `- [ ]` and `- [x]` become ☐ and ☒. |
| Tables | Pipe tables with alignment (`:--`, `:-:`, `--:`), escaped pipes (`\|`), pipes inside inline code, `<br>` inside cells. Missing cells are filled, extra cells are dropped. |
| Code blocks | Fenced with ``` or ~~~, indentation preserved. |
| Quotes | `>` lines, including lists inside the quote. |
| Horizontal rules | `---`, `***`, `___`. |
| Images | Not downloaded: shown as "[Image : caption]" in italics. |
| HTML | `<br>` becomes a line break; other tags are removed, their text is kept. HTML entities are decoded. |
| LaTeX, Mermaid | Kept as raw text (Mermaid stays a code block). |

## Document title

The title is chosen in this order:

1. the first heading, if the answer starts with one (it is then removed from the body to avoid a duplicate);
2. otherwise the user's question, if it fits on one line and is at most 120 characters;
3. otherwise the first sentence of the answer, if it is at most 90 characters;
4. otherwise "Document".

The file name is derived from the title, for example `procedure-d-accueil-d-un-nouvel-arrivant.docx`.

## Document layout

| Item | Value |
|---|---|
| Page | A4, margins 2 cm (top 2.2 cm) |
| Body | Calibri 11 pt, line spacing 1.15, dark grey |
| Title | 24 pt bold, indigo underline |
| Headings | Heading 1: 18 pt, Heading 2: 14 pt indigo, Heading 3: 12 pt, Heading 4: 11 pt; kept with the next paragraph |
| Tables | Full text width, columns sized from their content, indigo header row repeated on each page, alternating rows, light borders, 10 pt |
| Code | Consolas 9.5 pt, light grey background, accent border on the left |
| Quotes | Accent border on the left, italic, slate grey |
| Header / footer | Title and date, bottom border / "Page X / Y" (`HEADER_FOOTER`) |
| Properties | Title, author "Flash Word", language `fr-FR` or `en-GB` (also used for spell checking) |

## Installation and update

1. **Admin Panel > Functions**, create a new function (or open the existing one to update it).
2. Paste the content of `flash_word.py` and save. The function id is derived from the title (`flash_word`).
3. Enable it and mark it as **Global**, or attach it to specific models.
4. Verify the file you deployed:

   ```bash
   sha256sum flash_word.py
   ```

   Compare with the hash published in [CHANGELOG.md](../CHANGELOG.md).

`python-docx` is shipped with Open WebUI. To check it in your container:

```bash
docker exec <open-webui-container> python3 -c "import importlib.metadata as m; print(m.version('python-docx'))"
```

If Open WebUI cannot load the module, it deactivates the function automatically. Check that the function is still active after saving.

## Configuration

| Valve | Default | Effect |
|---|---|---|
| `priority` | `0` | Order of the button among actions. |
| `MESSAGE_COUNT` | `1` | Number of recent messages exported. Above 1, each message is introduced by a "Question" or "Answer" label and the title comes from the question. |
| `HEADER_FOOTER` | `true` | Adds the header (title, date) and the footer (Page X / Y). |
| `MIN_TEXT_LENGTH` | `20` | Shorter texts show "Nothing to export". |
| `LANGUAGE` | `fr` | `fr` or `en`: interface messages, date format, labels and document language. Other values fall back to English. |
| `SHOW_STATUS` | `true` | Shows progress in the chat status line. |

## What the user sees

- A notification "generation started", then "generation finished" almost immediately.
- A card with the document title, a summary (sections, tables, words) and the **Download Word document** button.
- Under it, a preview of the document on a white page, at most 440 px high with its own scroll bar and a fade at the bottom. Links are shown but not clickable in the preview; they are clickable in the `.docx`.

Without JavaScript the card works the same way: it contains no script.

## Data lifecycle

- No model is called and the plugin makes no network call: the conversation text never leaves the Open WebUI server.
- The `.docx` is built **in memory** and never written to disk.
- The card, with the `.docx` embedded in base64, is stored by Open WebUI inside the chat message (message embeds): about 60 KB for a one-page document with a table.
- In Open WebUI 0.13.0, deleting a chat deletes its messages and embeds (`ON DELETE CASCADE`): any chat retention or pruning policy also cleans the documents.
- Each click **adds** an embed to the message; previous cards remain in the database until the chat is deleted.

## Logs

One line per run, metadata only:

```
flash_word: ok sections=4 tables=1 words=152 size=38ko duration=0.21s
flash_word: erreur <ExceptionClass> duration=...
```

To follow a test run:

```bash
docker logs -f --since 1m <open-webui-container> 2>&1 | grep --line-buffered flash_word
```

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| No button under the answers | Function inactive, not global, or not assigned to the model | Check the function in Admin > Functions. |
| Function deactivated right after saving | Load error | Paste the file again, compare the SHA-256, read the server logs. |
| "Nothing to export" | Answer shorter than `MIN_TEXT_LENGTH` | Expected behaviour. |
| "Generation failed" | Unexpected error | Read the `flash_word: erreur` line, open an issue with the exception class and, if possible, a non-sensitive Markdown sample that reproduces it. |
| Word offers to repair the file | Unexpected | Open an issue with the plugin version, the Word version and a non-sensitive sample. |
| A Markdown element appears as raw text | Not supported (LaTeX, Mermaid, deeper nesting) | See [Supported Markdown](#supported-markdown). |
| Download does nothing | Iframe sandbox without downloads, or settings blocking `data:` links | Check the iframe settings in Open WebUI. |
| The page jumps when the card appears | Open WebUI scrolls to new embeds | Controlled by Open WebUI; the fixed-height preview keeps the card in view. |

## Test checklist

1. **Procedure with a table**: headings, a numbered list with sub-items, a table. Check the navigation pane, the numbering and the table header.
2. **Edge cases**: `file_name.py`, `5 * 3`, a link with parentheses, a `javascript:` link (must not be clickable), a table with a missing cell.
3. **Code block** with indentation and `<tags>`: shown as is.
4. **Reasoning model**: the `<think>` content must not appear in the document.
5. **Short answer**: fewer than 20 characters gives "Nothing to export".
6. **Two messages** (`MESSAGE_COUNT` = 2): "Question" and "Answer" labels.
7. **Word**: opens without repair prompt; header, footer and page numbers present.
8. **Non-admin user**: button visible and working.
9. **Reload**: the card is still there after reloading the page.

## Compatibility

- Developed and tested on **Open WebUI 0.13.0**.
- The header declares `required_open_webui_version: 0.11.0` because Actions can return inline `HTMLResponse` embeds since 0.11. Versions 0.11 and 0.12 were not tested.
- Tested with `python-docx` 0.8.11 and 1.2.0. The generated XML was checked against the element order of the WordprocessingML schema, the files were reopened with `python-docx` and rendered with LibreOffice.

## Known limitations

- LaTeX and Mermaid are not rendered.
- Images are not downloaded.
- Lists deeper than two levels are flattened to level 2.
- Setext headings (text underlined with `===`) are not recognised; `---` under a line is read as a horizontal rule.
