# Terminal IELTS

A terminal-style IELTS Reading practice application built with Python and
[Textual](https://textual.textualize.io/). Its question bank is extracted from a
complete local source snapshot of
[`sallowayma-git/IELTS-practice`](https://github.com/sallowayma-git/IELTS-practice).

## What it includes

- A complete, unmodified upstream source tree in `source/IELTS-practice/`
- A reproducible extractor for the generated reading exam assets
- Passage, question, option, and answer-key extraction
- Search plus part/frequency filtering
- Split-pane reading practice with every answer field visible together,
  scoring, corrections, and local attempt history
- Radio buttons for every source single-choice control (including TRUE/FALSE
  and YES/NO), with complete option text; checkboxes for genuine multi-select
- Dropdown selectors for matching, classification, sentence-ending, and
  heading questions, showing the complete label while recording its score value
- Sentence/notes completion rendered once in its original paragraph layout;
  a native editor highlights and fills each inline blank without duplicating it
- Word-bank summaries rendered once with inline Select slots instead of
  repeating the whole paragraph for every question
- Per-attempt start/end time, duration, attempted count, weighted accuracy,
  and backward-compatible history statistics
- A muted charcoal theme designed to look like an ordinary terminal workspace
- Keyboard-first controls in a Textual interface

The current extraction contains **234 reading passages and 3,143 questions**.
Every manifest entry has a matching source asset, and every extracted question
has a non-empty answer key.

The upstream question content is for personal learning and carries the source
project's copyright and distribution restrictions. Keep it local and do not
redistribute the extracted bank.

## Run

```bash
uv sync
uv run terminal-ielts
```

Useful keys:

- `/` focuses search
- `Enter` opens the selected passage or moves to the next answer field
- `Ctrl+Up` / `Ctrl+Down` move between answer fields
- `Ctrl+S` submits and scores the passage
- `Escape` returns to the library
- `Q` quits from the library

Attempts are appended to `practice_history.jsonl` in the directory where the
app is launched. Each new row uses history schema v2 and records elapsed time
with a monotonic timer.

View aggregate practice statistics (legacy history rows are also accepted):

```bash
uv run terminal-ielts history-stats
```

Inside a paragraph completion editor, `Tab` / `Shift+Tab` moves between blanks.

## Re-extract the bank

After updating or replacing the full source snapshot, rebuild the packaged JSON bank with:

```bash
uv run terminal-ielts extract
```

Inspect its coverage with:

```bash
uv run terminal-ielts stats
```

## Verify

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```
