# Terminal IELTS

A terminal-style IELTS Reading practice application built with Python and
[Textual](https://textual.textualize.io/). Its question bank is extracted from a
complete local source snapshot of
[`sallowayma-git/IELTS-practice`](https://github.com/sallowayma-git/IELTS-practice).

## Screenshots

### Question bank

![Terminal IELTS question bank](docs/images/library.svg)

### Split-pane practice

![Terminal IELTS split-pane reading practice](docs/images/practice.svg)

### Take notes

![Terminal IELTS take-notes editor](docs/images/take-notes.svg)

## What it includes

- A complete, unmodified upstream source tree in `source/IELTS-practice/`
- A reproducible extractor for the generated reading exam assets
- Passage, question, option, and answer-key extraction
- Search plus part/frequency filtering
- English-only passage titles in the library and practice header
- Split-pane reading practice with every answer field visible together,
  scoring, corrections, and local attempt history
- Responsive practice layout: below 120 columns, one full-width pane is shown
  at a time and the existing bottom Footer entry (or F2) switches Reading/Questions
- Persistent passage highlights created by selecting text and then clicking
  Highlight or pressing Ctrl+X, with click-to-remove, undo-last, and clear-all controls
- Ctrl+C retains its native behavior and copies only the current text selection
- A fully local English-to-Chinese dictionary, available from practice with
  F4 / Ctrl+D or directly from the command line
- Per-passage notes opened with Ctrl+N and saved independently of submissions
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
- A muted Ubuntu/GNOME Terminal-style aubergine theme, enabled by default;
  Ubuntu orange is reserved for keyboard focus rather than large bright areas
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
- `F2` switches Reading/Questions in narrow terminals (the Footer entry is clickable)
- `Ctrl+X` highlights the current passage selection; without a pending passage
  selection, answer fields keep their native cut behavior
- `Ctrl+C` copies the current text selection
- `F4` or `Ctrl+D` opens the local dictionary (`Fn+F4` may be required on macOS)
- `Ctrl+N` opens Take Notes; inside the editor, `Ctrl+S` saves and `Escape` cancels
- `Ctrl+Up` / `Ctrl+Down` move between answer fields
- `Ctrl+S` submits and scores the passage
- `Escape` returns to the library
- `Q` quits from the library
- `Ctrl+P` opens the command palette, including Textual's theme selector

Practice data is loaded from and atomically saved to `~/.terminal_ielts.json`.
The single file contains submitted attempts plus unfinished progress. It stores
answers, inline blank values, active practice time, detailed question results,
accuracy, and per-passage notes; reopening an unfinished passage restores it without counting time
spent outside the practice screen. Submitted passages display `✓` in the library.
Legacy `practice_history.jsonl` in the launch directory is imported when the new
default store does not yet exist, and `history-stats --history` still accepts a
legacy JSONL path.

View aggregate practice statistics (legacy history rows are also accepted):

```bash
uv run terminal-ielts history-stats
```

Inside a paragraph completion editor, `Tab` / `Shift+Tab` moves between blanks.

## Local dictionary

The included `data/E2Cdictionary.js` provides offline English-to-Chinese
lookups; no network request is made. During practice, press `F4` or `Ctrl+D`,
enter one English word, then press `Enter` to look it up. Press `Escape` to
close the dictionary and return to the passage.

The same dictionary is available without launching the Textual interface:

```bash
uv run terminal-ielts dictionary apple
uv run terminal-ielts dictionary woollen
```

A missing word shows nearby prefix suggestions when available. To query a
different compatible dictionary file, pass `--data` or set
`TERMINAL_IELTS_DICTIONARY`:

```bash
uv run terminal-ielts dictionary appl --data /path/to/E2Cdictionary.js
TERMINAL_IELTS_DICTIONARY=/path/to/E2Cdictionary.js \
  uv run terminal-ielts dictionary dictionary
```

Although the original data file is described as JSON, it uses a JavaScript
object wrapper. Terminal IELTS follows its `$word:"meaning"` record format as
UTF-8 text and never executes the file as code. Missing or unreadable files
produce a clear command-line error.

## Clipboard

Clipboard integration uses native system commands:

- macOS: `pbcopy`
- Linux Wayland: `wl-copy`
- Linux X11: `xclip` or `xsel`

On Linux, install the helper that matches your session type:

```bash
# Wayland (most modern GNOME/KDE desktops)
sudo apt install wl-clipboard

# X11
sudo apt install xclip
```

If no native helper is installed, the app shows a warning such as
`Clipboard unavailable: install wl-clipboard ...` and falls back to the
terminal's OSC 52 clipboard support, which only works in terminals that
support it (e.g. Kitty, Alacritty).

When launching the app from a `.desktop` file, systemd user unit, or other
context that strips environment variables, `wl-copy` may not find the
Wayland socket.  The app now re-injects sensible defaults for
`XDG_RUNTIME_DIR` and `WAYLAND_DISPLAY` automatically; if you still have
issues, launch it from the terminal where `wl-copy` works, or export them
before launching:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export WAYLAND_DISPLAY=wayland-0
uv run terminal-ielts
```

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
