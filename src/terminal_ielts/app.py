"""Textual terminal interface for IELTS reading practice."""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.content import Content, Span
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.message import Message
from textual.selection import Selection
from textual.style import Style
from textual.theme import Theme
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Markdown,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TextArea,
)
from textual.widgets._markdown import MarkdownBlock

from .bank import answer_is_correct
from .dictionary import E2CDictionary, normalise_word
from .history import DEFAULT_DATA_PATH, load_practice_data, write_practice_data


UBUNTU_GNOME_THEME = Theme(
    name="ubuntu-gnome",
    primary="#E95420",
    secondary="#77216F",
    accent="#E95420",
    warning="#C4A000",
    error="#CC0000",
    success="#4E9A06",
    foreground="#EEEEEC",
    background="#300A24",
    surface="#3A102D",
    panel="#451438",
    dark=True,
    luminosity_spread=0.1,
    variables={
        "border": "#5E2750",
        "border-blurred": "#451438",
        "footer-background": "#2C001E",
        "footer-key-foreground": "#E95420",
        "scrollbar": "#5E2750",
        "scrollbar-hover": "#77216F",
        "scrollbar-active": "#E95420",
        "scrollbar-background": "#2C001E",
        "scrollbar-background-hover": "#2C001E",
        "scrollbar-background-active": "#2C001E",
        "scrollbar-corner-color": "#2C001E",
        "input-selection-background": "#E95420 35%",
        "screen-selection-background": "#5E2750",
        "screen-selection-foreground": "#EEEEEC",
        "block-cursor-background": "#E95420",
        "block-cursor-foreground": "#FFFFFF",
        "markdown-h1-color": "#D3D7CF",
        "markdown-h2-color": "#D3D7CF",
        "markdown-h3-color": "#AEA79F",
        "markdown-h4-color": "#D3D7CF",
    },
)


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
CJK_BRACKET_RE = re.compile(
    r"\([^)]*[\u3400-\u4dbf\u4e00-\u9fff][^)]*\)"
    r"|（[^）]*[\u3400-\u4dbf\u4e00-\u9fff][^）]*）"
    r"|【[^】]*[\u3400-\u4dbf\u4e00-\u9fff][^】]*】"
)


def _find_executable(name: str) -> str | None:
    """Find an executable by name, also checking common absolute paths."""
    if path := shutil.which(name):
        return path
    for candidate in (
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/bin/{name}",
        f"/opt/bin/{name}",
        f"/snap/bin/{name}",
        f"{os.path.expanduser('~')}/.local/bin/{name}",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _clipboard_commands() -> list[list[str]]:
    """Return all available native system-clipboard commands."""
    commands: list[list[str]] = []
    if sys.platform == "darwin":
        if pbcopy := _find_executable("pbcopy"):
            commands.append([pbcopy])
        return commands
    if not sys.platform.startswith("linux"):
        return commands

    # Prefer the tool that matches the current session type first.
    if os.environ.get("WAYLAND_DISPLAY") and (wl_copy := _find_executable("wl-copy")):
        commands.append([wl_copy])
    if os.environ.get("DISPLAY"):
        if xclip := _find_executable("xclip"):
            commands.append([xclip, "-selection", "clipboard"])
        if xsel := _find_executable("xsel"):
            commands.append([xsel, "--clipboard", "--input"])

    # Also try any installed tool as a fallback, so a missing env var does not
    # silently prevent clipboard usage.
    if (wl_copy := _find_executable("wl-copy")) and [wl_copy] not in commands:
        commands.append([wl_copy])
    if (xclip := _find_executable("xclip")) and [xclip, "-selection", "clipboard"] not in commands:
        commands.append([xclip, "-selection", "clipboard"])
    if (xsel := _find_executable("xsel")) and [xsel, "--clipboard", "--input"] not in commands:
        commands.append([xsel, "--clipboard", "--input"])
    return commands


def native_clipboard_missing_hint() -> str:
    """Return a platform-specific install hint when no clipboard tool is found."""
    if sys.platform == "darwin":
        return "pbcopy is not available"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "install wl-clipboard (e.g. apt install wl-clipboard)"
    if os.environ.get("DISPLAY"):
        return "install xclip or xsel (e.g. apt install xclip)"
    return "no clipboard backend found"


def _clipboard_subprocess_env() -> dict[str, str]:
    """Build an subprocess environment for clipboard tools.

    Some launch contexts (systemd user units, .desktop files, etc.) strip
    Wayland/X11 variables.  Re-inject sensible defaults so wl-copy/xclip can
    find the display.
    """
    env = os.environ.copy()
    if sys.platform == "darwin":
        return env

    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

    if "WAYLAND_DISPLAY" not in env and "DISPLAY" not in env:
        xdg_runtime = env["XDG_RUNTIME_DIR"]
        for display in ("wayland-0", "wayland-1", "wayland-2"):
            if os.path.exists(os.path.join(xdg_runtime, display)):
                env["WAYLAND_DISPLAY"] = display
                break

    # If X11 is available but XAUTHORITY is missing, the XWayland socket is
    # usually enough for xclip/xsel; no further defaults are needed.
    return env


def _run_clipboard_command(command: list[str], text: str) -> bool:
    """Run a single clipboard command.  Return True if it succeeds."""
    try:
        # Use Popen and manually close stdin after writing.  wl-copy (and
        # some other clipboard tools) can hang when stdin is passed via
        # subprocess.run(input=...) because they wait for EOF in a way that
        # Python's communicate() does not satisfy.
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_clipboard_subprocess_env(),
        )
        if proc.stdin is not None:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        proc.wait(timeout=2)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def copy_to_native_clipboard(text: str) -> tuple[bool, str]:
    """Write text to a local system clipboard backend without invoking a shell.

    Returns a (success, error_message) tuple.  On Linux, multiple tools are
    attempted in case the first one fails.
    """
    commands = _clipboard_commands()
    if not commands:
        env_info = (
            f"PATH={os.environ.get('PATH', 'not set')}; "
            f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', 'unset')}; "
            f"DISPLAY={os.environ.get('DISPLAY', 'unset')}; "
            f"wl-copy={_find_executable('wl-copy') or 'not found'}; "
            f"xclip={_find_executable('xclip') or 'not found'}; "
            f"xsel={_find_executable('xsel') or 'not found'}"
        )
        return False, env_info

    for command in commands:
        if _run_clipboard_command(command, text):
            return True, ""
    return False, f"{' / '.join(' '.join(c) for c in commands)} failed"


def display_exam_title(title: str) -> str:
    """Return the source title's English display portion without mutating data."""
    cleaned = CJK_BRACKET_RE.sub(" ", str(title))
    cleaned = re.sub(r"^\s*\d{4}[\u3400-\u4dbf\u4e00-\u9fff]+\s*", "", cleaned)
    first_cjk = CJK_RE.search(cleaned)
    if first_cjk is not None:
        cleaned = cleaned[: first_cjk.start()]
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" \t-_—–:：")
    return cleaned or "Untitled passage"


@dataclass(frozen=True)
class PassageHighlightPart:
    block: MarkdownBlock
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class PassageHighlight:
    id: str
    parts: tuple[PassageHighlightPart, ...]


class WrappingRadioButton(RadioButton):
    """Radio button whose label grows vertically instead of being truncated."""

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        return Widget.get_content_height(self, container, viewport, width)


class WrappingCheckbox(Checkbox):
    """Checkbox whose label grows vertically instead of being truncated."""

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        return Widget.get_content_height(self, container, viewport, width)


def selection_character_span(text: str, selection: Selection) -> tuple[int, int]:
    """Convert Textual's logical line/column selection to character offsets."""
    lines = text.splitlines(keepends=True) or [""]

    def offset_to_index(offset: Any, default: int) -> int:
        if offset is None:
            return default
        line_index = max(0, min(int(offset.y), len(lines) - 1))
        line = lines[line_index]
        content_length = len(line.rstrip("\r\n"))
        column = max(0, min(int(offset.x), content_length))
        return sum(len(previous) for previous in lines[:line_index]) + column

    start = offset_to_index(selection.start, 0)
    end = offset_to_index(selection.end, len(text))
    return (start, end) if start <= end else (end, start)


def question_is_correct(question: dict[str, Any], user_answer: str) -> bool:
    return answer_is_correct(
        user_answer,
        question["answer"],
        positional=question.get("answer_mode") == "positional",
    )


def question_hint(question: dict[str, Any]) -> str:
    options = question.get("options", [])
    if options:
        if len(options) <= 5 and all(len(option) <= 20 for option in options):
            return "Allowed: " + " · ".join(options)
        return "Enter the option letter/number shown above"
    kind = str(question.get("kind", "")).replace("_", " ").title()
    return f"Answer type: {kind}" if kind else "Type your answer"


def is_multiple_selection(question: dict[str, Any]) -> bool:
    if question.get("response_mode") == "checkbox_many":
        return True
    parts = [part.strip() for part in re.split(r"\s*/\s*", question.get("answer", ""))]
    return len(parts) > 1 and all(re.fullmatch(r"[A-Za-z]", part) for part in parts)


def uses_radio_buttons(question: dict[str, Any]) -> bool:
    if question.get("response_mode") == "radio_one":
        return bool(question.get("options"))
    kind = str(question.get("kind", "")).casefold()
    choice_kind = kind in {
        "true_false_not_given",
        "yes_no_not_given",
        "single_choice",
        "multiple_choice",
        "multi_choice",
    }
    return choice_kind and bool(question.get("options")) and not is_multiple_selection(question)


def uses_heading_select(question: dict[str, Any], group: dict[str, Any]) -> bool:
    return bool(question.get("options")) and (
        group.get("subtype") == "heading_select"
        or "heading" in str(group.get("instructions", "")).casefold()
    )


def question_option_items(question: dict[str, Any]) -> list[dict[str, str]]:
    items = question.get("option_items", [])
    if items:
        return [
            {"value": str(item["value"]), "label": str(item.get("label", item["value"]))}
            for item in items
        ]
    return [{"value": str(option), "label": str(option)} for option in question.get("options", [])]


def option_display(item: dict[str, str]) -> str:
    value = item["value"]
    label = item["label"]
    normalised = value.casefold()
    if normalised in {"true", "false", "not given", "yes", "no"}:
        return value.upper()
    return value if label.casefold() == value.casefold() else f"{value} — {label}"


def select_options(question: dict[str, Any]) -> list[tuple[str, str]]:
    return [(option_display(item), item["value"]) for item in question_option_items(question)]


def heading_select_options(question: dict[str, Any]) -> list[tuple[str, str]]:
    """Return full heading labels paired with their scoreable roman-numeral value."""
    if question.get("option_items"):
        return select_options(question)
    choices: list[tuple[str, str]] = []
    for option in question.get("options", []):
        match = re.match(r"^\s*([ivxlcdm]+|[A-Za-z]|\d+)\s*[.)-]?\s*(.*)$", option, re.IGNORECASE)
        value = match.group(1).casefold() if match else option.strip()
        choices.append((option, value))
    return choices


def group_select_options(group: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (option_display(option), str(option["value"]))
        for option in group.get("option_bank", group.get("options", []))
    ]


def uses_inline_completion(group: dict[str, Any], questions: list[dict[str, Any]]) -> bool:
    template = str(group.get("completion_template", ""))
    slots = list(group.get("completion_slots", []))
    slotted_question_ids = {str(slot.get("question_id", "")) for slot in slots}
    return (
        group.get("response_mode") == "inline_text"
        and bool(template)
        and bool(slots)
        and all(question["id"] in slotted_question_ids for question in questions)
        and all(f"[[[{slot['slot_id']}]]]" in template for slot in slots)
    )


def uses_inline_select(group: dict[str, Any], questions: list[dict[str, Any]]) -> bool:
    template = str(group.get("completion_template", ""))
    slots = list(group.get("completion_slots", []))
    slotted_question_ids = {str(slot.get("question_id", "")) for slot in slots}
    return (
        group.get("response_mode") == "inline_select"
        and bool(group.get("option_bank"))
        and bool(template)
        and bool(slots)
        and all(question["id"] in slotted_question_ids for question in questions)
        and all(f"[[[{slot['slot_id']}]]]" in template for slot in slots)
    )


def group_type_label(group: dict[str, Any], questions: list[dict[str, Any]]) -> str:
    if group.get("subtype") == "heading_select":
        return "MATCHING HEADINGS"
    if group.get("subtype") == "inline_bank_select":
        return "WORD BANK COMPLETION"
    kind = str(group.get("kind", "question"))
    modes = {str(question.get("response_mode", "text")) for question in questions}
    if len(modes) > 1:
        return "MIXED QUESTION TYPES"
    if kind == "short_answer":
        return {
            "radio_one": "SINGLE CHOICE",
            "select_one": "SELECTION",
            "checkbox_many": "MULTIPLE CHOICE",
        }.get(next(iter(modes), "text"), "SHORT ANSWER")
    return kind.replace("_", " ").upper()


def completion_blank_label(
    template: str,
    token_start: int,
    source_number: str,
    display_number: str,
    shown: str,
) -> str:
    """Avoid repeating a question number already present beside an inline blank."""
    line_prefix = template[:token_start].rsplit("\n", 1)[-1]
    escaped_number = re.escape(source_number)
    number_starts_line = re.match(
        rf"^\s*(?:[-•]\s*)?{escaped_number}(?:[.)]?\s+)",
        line_prefix,
    )
    number_precedes_blank = re.search(
        rf"(?<!\w){escaped_number}(?:[.)])?\s*$",
        line_prefix,
    )
    prefix = "" if number_starts_line or number_precedes_blank else f"{display_number}: "
    return f" {prefix}{shown} "


class CompletionInput(Input):
    """Native input that reserves Tab navigation for inline completion slots."""

    class NextBlank(Message):
        pass

    class PreviousBlank(Message):
        pass

    class FocusChanged(Message):
        pass

    def on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.NextBlank())
        elif event.key == "shift+tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.PreviousBlank())

    def on_focus(self) -> None:
        self.post_message(self.FocusChanged())

    def on_blur(self) -> None:
        self.post_message(self.FocusChanged())


class CompletionEditor(Container):
    """Full original paragraph plus a native editor for its highlighted inline slot."""

    TOKEN_RE = re.compile(r"\[\[\[([^\]]+)\]\]\]")

    class AnswerChanged(Message):
        def __init__(self, editor: "CompletionEditor") -> None:
            super().__init__()
            self.editor = editor

        @property
        def control(self) -> "CompletionEditor":
            return self.editor

    def __init__(
        self,
        template: str,
        questions: list[dict[str, Any]],
        slots: list[dict[str, Any]],
        *,
        id: str,
        classes: str,
    ) -> None:
        self.template = template
        self.editor_key = id
        self.slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
        token_order = [match.group(1) for match in self.TOKEN_RE.finditer(template)]
        self.field_ids = [slot_id for slot_id in token_order if slot_id in self.slot_by_id]
        question_by_id = {question["id"]: question for question in questions}
        occurrence_count: dict[str, int] = {}
        for slot in slots:
            question_id = str(slot["question_id"])
            occurrence_count[question_id] = occurrence_count.get(question_id, 0) + 1
        self.numbers: dict[str, str] = {}
        self.source_numbers: dict[str, str] = {}
        for slot in slots:
            slot_id = str(slot["slot_id"])
            question_id = str(slot["question_id"])
            number = str(question_by_id[question_id]["number"])
            self.source_numbers[slot_id] = number
            if occurrence_count[question_id] > 1:
                number += chr(ord("a") + int(slot.get("occurrence", 0)))
            self.numbers[slot_id] = number
        self.answers = {slot_id: "" for slot_id in self.field_ids}
        self.active_index = 0
        self._syncing_input = False
        super().__init__(id=id, classes=classes)

    def compose(self) -> ComposeResult:
        yield Static("", id=f"{self.editor_key}-text", classes="completion-text")
        with Horizontal(classes="completion-entry"):
            yield Static("", id=f"{self.editor_key}-label", classes="completion-label")
            yield CompletionInput(
                placeholder="Type the missing word(s)…",
                id=f"{self.editor_key}-input",
                classes="completion-native-input",
            )

    def on_mount(self) -> None:
        self.refresh_template()
        self.sync_input()

    @property
    def active_question_id(self) -> str:
        return str(self.slot_by_id[self.field_ids[self.active_index]]["question_id"])

    @property
    def active_slot_id(self) -> str:
        return self.field_ids[self.active_index]

    @property
    def question_answers(self) -> dict[str, str]:
        grouped: dict[str, list[str]] = {}
        for slot_id in self.field_ids:
            question_id = str(self.slot_by_id[slot_id]["question_id"])
            grouped.setdefault(question_id, []).append(self.answers[slot_id])
        return {
            question_id: (" / ".join(values) if any(value.strip() for value in values) else "")
            for question_id, values in grouped.items()
        }

    def render_template(self) -> Text:
        rendered = Text()
        cursor = 0
        for match in self.TOKEN_RE.finditer(self.template):
            rendered.append(self.template[cursor : match.start()])
            slot_id = match.group(1)
            if slot_id not in self.answers:
                rendered.append("________", style="dim")
            else:
                answer = self.answers[slot_id]
                number = self.numbers[slot_id]
                blank = answer if answer else "________"
                label = completion_blank_label(
                    self.template,
                    match.start(),
                    self.source_numbers[slot_id],
                    number,
                    blank,
                )
                is_active = slot_id == self.active_slot_id
                input_widget = self.query_one(CompletionInput) if self.is_mounted else None
                if is_active and input_widget is not None and input_widget.has_focus:
                    rendered.append(label, style="bold #EEEEEC on #5E2750")
                elif answer:
                    rendered.append(label, style="underline #D3D7CF")
                else:
                    rendered.append(label, style="underline #AEA79F")
            cursor = match.end()
        rendered.append(self.template[cursor:])
        return rendered

    def refresh_template(self) -> None:
        self.query_one(f"#{self.editor_key}-text", Static).update(self.render_template())
        self.query_one(f"#{self.editor_key}-label", Static).update(f"Blank {self.numbers[self.active_slot_id]}")

    def sync_input(self) -> None:
        input_widget = self.query_one(CompletionInput)
        self._syncing_input = True
        input_widget.value = self.answers[self.active_slot_id]
        input_widget.cursor_position = len(input_widget.value)
        self._syncing_input = False
        self.refresh_template()

    def move_blank(self, offset: int) -> None:
        self.active_index = (self.active_index + offset) % len(self.field_ids)
        self.sync_input()
        self.query_one(CompletionInput).focus()

    def focus_input(self) -> None:
        self.query_one(CompletionInput).focus()

    @on(Input.Changed, ".completion-native-input")
    def input_changed(self, event: Input.Changed) -> None:
        if self._syncing_input:
            return
        self.answers[self.active_slot_id] = event.value
        self.refresh_template()
        self.post_message(self.AnswerChanged(self))

    @on(Input.Submitted, ".completion-native-input")
    @on(CompletionInput.NextBlank)
    def next_blank(self) -> None:
        self.move_blank(1)

    @on(CompletionInput.PreviousBlank)
    def previous_blank(self) -> None:
        self.move_blank(-1)

    @on(CompletionInput.FocusChanged)
    def focus_changed(self) -> None:
        self.refresh_template()


class CompletionSelect(Select[str]):
    """Native Select that keeps Tab navigation inside an inline option bank."""

    class NextBlank(Message):
        pass

    class PreviousBlank(Message):
        pass

    def on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.NextBlank())
        elif event.key == "shift+tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.PreviousBlank())


class InlineSelectEditor(Container):
    """Render a complete source paragraph and edit each option slot with Select."""

    TOKEN_RE = CompletionEditor.TOKEN_RE

    class AnswerChanged(Message):
        def __init__(self, editor: "InlineSelectEditor") -> None:
            super().__init__()
            self.editor = editor

    def __init__(
        self,
        template: str,
        questions: list[dict[str, Any]],
        slots: list[dict[str, Any]],
        options: list[dict[str, str]],
        *,
        id: str,
        classes: str,
    ) -> None:
        self.template = template
        self.editor_key = id
        self.slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
        self.field_ids = [
            match.group(1)
            for match in self.TOKEN_RE.finditer(template)
            if match.group(1) in self.slot_by_id
        ]
        question_by_id = {question["id"]: question for question in questions}
        self.source_numbers = {
            slot_id: str(question_by_id[str(self.slot_by_id[slot_id]["question_id"])]["number"])
            for slot_id in self.field_ids
        }
        self.numbers = dict(self.source_numbers)
        self.options = [
            {"value": str(option["value"]), "label": str(option.get("label", option["value"]))}
            for option in options
        ]
        self.label_by_value = {option["value"]: option_display(option) for option in self.options}
        self.answers = {slot_id: "" for slot_id in self.field_ids}
        self.active_index = 0
        self._syncing_select = False
        super().__init__(id=id, classes=classes)

    def compose(self) -> ComposeResult:
        yield Static("", id=f"{self.editor_key}-text", classes="completion-text")
        with Horizontal(classes="completion-entry"):
            yield Static("", id=f"{self.editor_key}-label", classes="completion-label")
            yield CompletionSelect(
                [(option_display(item), item["value"]) for item in self.options],
                prompt="Choose an option…",
                id=f"{self.editor_key}-select",
                classes="completion-native-select",
            )

    def on_mount(self) -> None:
        self.sync_select()

    @property
    def active_slot_id(self) -> str:
        return self.field_ids[self.active_index]

    @property
    def question_answers(self) -> dict[str, str]:
        return {
            str(self.slot_by_id[slot_id]["question_id"]): self.answers[slot_id]
            for slot_id in self.field_ids
        }

    def render_template(self) -> Text:
        rendered = Text()
        cursor = 0
        for match in self.TOKEN_RE.finditer(self.template):
            rendered.append(self.template[cursor : match.start()])
            slot_id = match.group(1)
            if slot_id in self.answers:
                answer = self.answers[slot_id]
                shown = answer if answer else "________"
                label = completion_blank_label(
                    self.template,
                    match.start(),
                    self.source_numbers[slot_id],
                    self.numbers[slot_id],
                    shown,
                )
                if slot_id == self.active_slot_id:
                    rendered.append(label, style="bold #EEEEEC on #5E2750")
                elif answer:
                    rendered.append(label, style="underline #D3D7CF")
                else:
                    rendered.append(label, style="underline #AEA79F")
            cursor = match.end()
        rendered.append(self.template[cursor:])
        return rendered

    def refresh_template(self) -> None:
        self.query_one(f"#{self.editor_key}-text", Static).update(self.render_template())
        self.query_one(f"#{self.editor_key}-label", Static).update(
            f"Blank {self.numbers[self.active_slot_id]}"
        )

    def sync_select(self) -> None:
        select = self.query_one(CompletionSelect)
        self._syncing_select = True
        value = self.answers[self.active_slot_id]
        select.value = value if value else Select.NULL
        self._syncing_select = False
        self.refresh_template()

    def move_blank(self, offset: int) -> None:
        self.active_index = (self.active_index + offset) % len(self.field_ids)
        self.sync_select()
        self.query_one(CompletionSelect).focus()

    def focus_select(self) -> None:
        self.query_one(CompletionSelect).focus()

    @on(Select.Changed, ".completion-native-select")
    def select_changed(self, event: Select.Changed) -> None:
        if self._syncing_select:
            return
        self.answers[self.active_slot_id] = "" if event.value is Select.NULL else str(event.value)
        self.refresh_template()
        self.post_message(self.AnswerChanged(self))

    @on(CompletionSelect.NextBlank)
    def next_blank(self) -> None:
        self.move_blank(1)

    @on(CompletionSelect.PreviousBlank)
    def previous_blank(self) -> None:
        self.move_blank(-1)


class ResultScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, exam: dict[str, Any], answers: dict[str, str], duration_seconds: int) -> None:
        super().__init__()
        self.exam = exam
        self.answers = answers
        self.duration_seconds = duration_seconds

    def compose(self) -> ComposeResult:
        questions = self.exam["questions"]
        attempted = sum(bool(self.answers.get(question["id"], "").strip()) for question in questions)
        correct = sum(
            question_is_correct(question, self.answers.get(question["id"], ""))
            for question in questions
        )
        percent = round(correct / len(questions) * 100) if questions else 0
        missed = [
            f"{question['number']}. yours: {self.answers.get(question['id']) or '—'}  |  answer: {question['answer']}"
            for question in questions
            if not question_is_correct(question, self.answers.get(question["id"], ""))
        ]
        details = "\n".join(missed) if missed else "Perfect score — no corrections needed."
        with Container(id="result-card"):
            yield Static("PRACTICE COMPLETE", classes="eyebrow")
            yield Static(f"{correct} / {len(questions)}", id="score")
            minutes, seconds = divmod(self.duration_seconds, 60)
            yield Static(f"{percent}% correct · {attempted} attempted · {minutes:02d}:{seconds:02d}")
            with VerticalScroll(id="corrections"):
                yield Static(details)
            yield Button("Return to question", id="close-result")

    @on(Button.Pressed, "#close-result")
    def close_result(self) -> None:
        self.dismiss()


class NotesScreen(ModalScreen[None]):
    """Focused multi-line notes editor for one reading passage."""

    AUTO_FOCUS = "#notes-editor"
    BINDINGS = [
        Binding("ctrl+s", "save_note", "Save note", priority=True),
        Binding("escape", "cancel_note", "Cancel", priority=True),
    ]

    def __init__(self, exam_id: str, title: str, initial_text: str) -> None:
        super().__init__()
        self.exam_id = exam_id
        self.title = title
        self.initial_text = initial_text

    def compose(self) -> ComposeResult:
        with Container(id="notes-card"):
            yield Static("TAKE NOTES", classes="eyebrow")
            yield Static(self.title, id="notes-title")
            yield TextArea(
                self.initial_text,
                id="notes-editor",
                placeholder="Write notes for this passage…",
                soft_wrap=True,
                show_line_numbers=False,
                tab_behavior="focus",
            )
            with Horizontal(id="notes-actions"):
                yield Button("Save note", id="save-note")
                yield Button("Cancel", id="cancel-note")
        yield Footer()

    def on_mount(self) -> None:
        editor = self.query_one("#notes-editor", TextArea)
        last_row = editor.document.line_count - 1
        editor.move_cursor((last_row, len(editor.document[last_row])))

    def action_save_note(self) -> None:
        note = self.query_one("#notes-editor", TextArea).text
        if self.app.save_note(self.exam_id, note):
            self.dismiss()

    def action_cancel_note(self) -> None:
        self.dismiss()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-note":
            self.action_save_note()
        elif event.button.id == "cancel-note":
            self.action_cancel_note()


class DictionaryScreen(ModalScreen[None]):
    """Keyboard-only English-to-Chinese dictionary lookup."""

    AUTO_FOCUS = "#dictionary-query"
    BINDINGS = [Binding("escape", "dismiss", "Close", priority=True)]

    def __init__(self, dictionary: E2CDictionary, initial_query: str = "") -> None:
        super().__init__()
        self.dictionary = dictionary
        self.initial_query = initial_query

    def compose(self) -> ComposeResult:
        with Container(id="dictionary-card"):
            yield Static("ENGLISH → CHINESE DICTIONARY", classes="eyebrow")
            yield Input(
                value=self.initial_query,
                placeholder="Enter one English word…",
                id="dictionary-query",
                select_on_focus=True,
            )
            with VerticalScroll(id="dictionary-results"):
                yield Static("", id="dictionary-word", markup=False)
                yield Static(
                    "Type one English word and press Enter.",
                    id="dictionary-meaning",
                    markup=False,
                )
            yield Static("Enter lookup  ·  Esc close", id="dictionary-help")

    def on_mount(self) -> None:
        if self.initial_query:
            self.lookup_word(self.initial_query)

    def lookup_word(self, query: str) -> None:
        word = self.query_one("#dictionary-word", Static)
        meaning = self.query_one("#dictionary-meaning", Static)
        try:
            result = self.dictionary.lookup(query)
        except OSError as error:
            word.update("Dictionary unavailable")
            meaning.update(str(error))
            return

        if not result.word:
            word.update("Invalid query")
            meaning.update("Enter one English word only.")
        elif result.found:
            word.update(result.word)
            meaning.update(result.meaning or "No definition is available.")
        else:
            word.update(result.word)
            suggestion_text = (
                "\n\nSuggestions: " + "  ·  ".join(result.suggestions)
                if result.suggestions
                else ""
            )
            meaning.update("Word not found in the local dictionary." + suggestion_text)

        self.query_one("#dictionary-results", VerticalScroll).scroll_home(animate=False)

    @on(Input.Submitted, "#dictionary-query")
    def query_submitted(self, event: Input.Submitted) -> None:
        self.lookup_word(event.value)
        event.stop()


class LibraryScreen(Screen[None]):
    BINDINGS = [
        Binding("/", "focus_search", "Search", show=True),
        Binding("enter", "open_selected", "Practice", show=True),
        Binding(
            "f4,ctrl+d",
            "open_dictionary",
            "Dictionary",
            key_display="F4 / Ctrl+D",
            priority=True,
        ),
        Binding("r", "random_exam", "Random", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, bank: dict[str, Any]) -> None:
        super().__init__()
        self.bank = bank
        self.filtered: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        stats = self.bank["stats"]
        yield Header(show_clock=True)
        with Container(id="library"):
            yield Static("IELTS READING // TERMINAL PRACTICE", classes="eyebrow")
            yield Static("Question Bank", id="library-title")
            yield Static(
                f"{stats['exam_count']} passages  ·  {stats['question_count']} questions  ·  source-backed",
                id="library-stats",
            )
            with Horizontal(id="filters"):
                yield Input(placeholder="Search title or exam id…", id="search")
                yield Select(
                    [("All parts", "all")] + [(value, value) for value in stats["categories"]],
                    value="all",
                    id="category",
                    allow_blank=False,
                )
                yield Select(
                    [("All frequencies", "all")] + [(value.title(), value) for value in stats["frequencies"]],
                    value="all",
                    id="frequency",
                    allow_blank=False,
                )
            yield DataTable(id="exam-table", cursor_type="row", zebra_stripes=True)
            yield Static("↑↓ select  ·  Enter practice  ·  / search  ·  R random", id="library-help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#exam-table", DataTable)
        table.add_columns("Done", "Part", "Frequency", "Passage", "Questions")
        self.apply_filters()
        table.focus()

    def on_screen_resume(self) -> None:
        """Refresh completion marks after returning from a submitted practice."""
        if self.is_mounted:
            self.apply_filters()

    def apply_filters(self) -> None:
        search = self.query_one("#search", Input).value.casefold().strip()
        category = self.query_one("#category", Select).value
        frequency = self.query_one("#frequency", Select).value
        self.filtered = [
            exam
            for exam in self.bank["exams"]
            if (not search or search in f"{exam['title']} {exam['exam_id']}".casefold())
            and (category == "all" or exam["category"] == category)
            and (frequency == "all" or exam["frequency"] == frequency)
        ]
        table = self.query_one("#exam-table", DataTable)
        table.clear()
        for exam in self.filtered:
            table.add_row(
                "✓" if exam["exam_id"] in self.app.completed_exam_ids else "",
                exam["category"],
                exam["frequency"].title(),
                display_exam_title(exam["title"]),
                str(len(exam["questions"])),
                key=exam["exam_id"],
            )
        self.query_one("#library-help", Static).update(
            f"{len(self.filtered)} shown  ·  {len(self.app.completed_exam_ids)} practiced ✓  ·  "
            "↑↓ select  ·  Enter practice  ·  / search  ·  R random"
        )

    @on(Input.Changed, "#search")
    @on(Select.Changed)
    def filters_changed(self) -> None:
        self.apply_filters()

    @on(DataTable.RowSelected, "#exam-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.start_exam(str(event.row_key.value))

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_open_selected(self) -> None:
        table = self.query_one("#exam-table", DataTable)
        if table.row_count:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            self.app.start_exam(str(row_key.value))

    def action_random_exam(self) -> None:
        if self.filtered:
            self.app.start_exam(random.choice(self.filtered)["exam_id"])

    def action_open_dictionary(self) -> None:
        self.app.open_dictionary()

    def action_quit(self) -> None:
        self.app.exit()


class PracticeScreen(Screen[None]):
    NARROW_WORKSPACE_WIDTH = 120

    BINDINGS = [
        Binding("f2", "toggle_pane", "Switch view"),
        Binding(
            "ctrl+x",
            "highlight_selection",
            "Highlight",
            priority=True,
        ),
        Binding(
            "f4,ctrl+d",
            "open_dictionary",
            "Dictionary",
            key_display="F4 / Ctrl+D",
            priority=True,
        ),
        Binding("ctrl+n", "take_notes", "Take notes", priority=True),
        Binding("ctrl+up", "previous_answer", "Previous answer"),
        Binding("ctrl+down", "next_answer", "Next answer"),
        Binding("ctrl+s", "submit_exam", "Submit"),
        Binding("escape", "library", "Library"),
    ]

    def __init__(self, exam: dict[str, Any], saved_progress: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.exam = exam
        self.saved_progress = saved_progress or {}
        valid_question_ids = {str(question["id"]) for question in exam["questions"]}
        saved_answers = (
            self.saved_progress.get("answers")
            if isinstance(self.saved_progress.get("answers"), dict)
            else {}
        )
        self.answers: dict[str, str] = {
            str(question_id): str(answer)
            for question_id, answer in saved_answers.items()
            if str(question_id) in valid_question_ids
        }
        try:
            started_at = datetime.fromisoformat(str(self.saved_progress.get("started_at", "")))
            self.started_at = started_at.astimezone()
        except (TypeError, ValueError):
            self.started_at = datetime.now().astimezone()
        try:
            self.elapsed_before_resume = max(0, int(self.saved_progress.get("elapsed_seconds", 0)))
        except (TypeError, ValueError):
            self.elapsed_before_resume = 0
        self.started_monotonic = time.monotonic()
        self._submitted = False
        self._frozen_duration_seconds: int | None = None
        self._clock_timer: Timer | None = None
        self._progress_save_timer: Timer | None = None
        self._last_persisted_elapsed = self.elapsed_before_resume
        self._restoring_progress = True
        self._narrow_mode = False
        self._narrow_pane = "passage"
        self._last_answer_index = 0
        self.article_highlights: list[PassageHighlight] = []
        self._pending_highlight_parts: tuple[PassageHighlightPart, ...] = ()
        self._article_base_content: dict[MarkdownBlock, Content] = {}
        self._next_highlight_id = 1

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="practice"):
            with Horizontal(id="practice-topbar"):
                with Vertical():
                    yield Static(self.exam["category"] + "  /  " + self.exam["frequency"].upper(), classes="eyebrow")
                    yield Static(display_exam_title(self.exam["title"]), id="exam-title")
                yield Static("", id="progress")
            with Horizontal(id="workspace"):
                with Vertical(classes="pane", id="passage-pane"):
                    yield Static("READING PASSAGE", classes="pane-title")
                    with Horizontal(id="passage-tools"):
                        yield Static(
                            "Select text, then click Highlight · Ctrl+X applies",
                            id="highlight-help",
                        )
                        yield Button(
                            "Highlight",
                            id="highlight-selection",
                            classes="highlight-action",
                            disabled=True,
                        )
                        yield Button(
                            "Undo highlight",
                            id="undo-highlight",
                            classes="highlight-action",
                            disabled=True,
                        )
                        yield Button(
                            "Clear",
                            id="clear-highlights",
                            classes="highlight-action",
                            disabled=True,
                        )
                    with VerticalScroll(id="passage-scroll"):
                        yield Markdown(self.exam["passage"], id="passage")
                with Vertical(classes="pane", id="question-pane"):
                    yield Static("ALL QUESTIONS", classes="pane-title")
                    with VerticalScroll(id="question-scroll"):
                        for group in self.exam["groups"]:
                            group_questions = [
                                question
                                for question in self.exam["questions"]
                                if question["group_id"] == group["id"]
                            ]
                            if not group_questions:
                                continue
                            inline_completion = uses_inline_completion(group, group_questions)
                            inline_selection = uses_inline_select(group, group_questions)
                            with Container(classes="question-group"):
                                yield Static(
                                    f"QUESTIONS {group_questions[0]['number']}–{group_questions[-1]['number']}  ·  "
                                    f"{group_type_label(group, group_questions)}",
                                    classes="group-title",
                                )
                                if group.get("instructions") and not inline_completion and not inline_selection:
                                    yield Markdown(group["instructions"], classes="group-instructions")
                                if inline_completion:
                                    yield Static(
                                        "Type directly in the highlighted blank · Tab / Shift+Tab moves between blanks",
                                        classes="completion-help",
                                    )
                                    yield CompletionEditor(
                                        group["completion_template"],
                                        group_questions,
                                        group["completion_slots"],
                                        id=f"completion-{group['id']}",
                                        classes="answer-control completion-editor",
                                    )
                                elif inline_selection:
                                    yield Static(
                                        "Choose in the original paragraph · Tab / Shift+Tab moves between blanks",
                                        classes="completion-help",
                                    )
                                    yield InlineSelectEditor(
                                        group["completion_template"],
                                        group_questions,
                                        group["completion_slots"],
                                        group["option_bank"],
                                        id=f"inline-select-{group['id']}",
                                        classes="answer-control completion-editor inline-select-editor",
                                    )
                                else:
                                    for index, question in enumerate(self.exam["questions"]):
                                        if question["group_id"] != group["id"]:
                                            continue
                                        with Container(classes="question-card", id=f"question-card-{index}"):
                                            yield Markdown(
                                                f"### {question['number']}\n\n{question['prompt']}",
                                                classes="question-copy",
                                            )
                                            if (
                                                group.get("kind") == "sentence_completion"
                                                and group.get("response_mode") == "select_one"
                                                and group.get("option_bank", group.get("options"))
                                            ):
                                                yield Static("Select the correct ending", classes="answer-hint")
                                                yield Select(
                                                    group_select_options(group),
                                                    prompt=f"Select for {question['number']}",
                                                    id=f"answer-{index}",
                                                    classes="answer-control answer-heading answer-select",
                                                )
                                            elif uses_heading_select(question, group):
                                                yield Static("Select a heading", classes="answer-hint")
                                                yield Select(
                                                    heading_select_options(question),
                                                    prompt=f"Heading for {question['number']}",
                                                    id=f"answer-{index}",
                                                    classes="answer-control answer-heading answer-select",
                                                )
                                            elif question.get("response_mode") == "select_one" and question.get("options"):
                                                yield Static("Select one", classes="answer-hint")
                                                yield Select(
                                                    select_options(question),
                                                    prompt=f"Select for {question['number']}",
                                                    id=f"answer-{index}",
                                                    classes="answer-control answer-heading answer-select",
                                                )
                                            elif uses_radio_buttons(question):
                                                yield Static("Select one", classes="answer-hint")
                                                with RadioSet(id=f"answer-{index}", classes="answer-control answer-radio"):
                                                    for option in question_option_items(question):
                                                        yield WrappingRadioButton(option_display(option))
                                            elif is_multiple_selection(question) and question.get("options"):
                                                yield Static("Select all required answers", classes="answer-hint")
                                                with Vertical(id=f"answer-{index}", classes="answer-control answer-multi"):
                                                    for option_index, option in enumerate(question_option_items(question)):
                                                        yield WrappingCheckbox(
                                                            option_display(option),
                                                            id=f"answer-{index}-option-{option_index}",
                                                            classes="multi-option",
                                                        )
                                            else:
                                                yield Static(question_hint(question), classes="answer-hint")
                                                yield Input(
                                                    placeholder=f"Answer for question {question['number']}…",
                                                    id=f"answer-{index}",
                                                    classes="answer-control answer-input",
                                                )
                    with Horizontal(id="question-actions"):
                        yield Button("Submit all answers", id="submit")
                        yield Button("Library", id="library-button")
        yield Footer()

    def on_mount(self) -> None:
        self.restore_saved_answers()
        self._update_workspace_layout(self.size.width)
        if self._narrow_mode:
            self.set_focus(None)
        else:
            controls = list(self.query(".answer-control"))
            if controls:
                self.focus_control(controls[self._last_answer_index])
        self.refresh_practice_status()
        self._clock_timer = self.set_interval(1.0, self.update_clock, name="practice-elapsed-clock")
        self.call_after_refresh(self.finish_progress_restore)

    def finish_progress_restore(self) -> None:
        self._restoring_progress = False
        self.persist_progress()

    def on_unmount(self) -> None:
        self.stop_timers()

    def on_resize(self, event: events.Resize) -> None:
        self._update_workspace_layout(event.size.width)

    def _update_workspace_layout(self, width: int) -> None:
        """Use one switchable full-width pane when two readable columns no longer fit."""
        was_narrow = self._narrow_mode
        self._narrow_mode = width < self.NARROW_WORKSPACE_WIDTH
        self.set_class(self._narrow_mode, "narrow-workspace")
        if not self.is_mounted:
            return
        if was_narrow == self._narrow_mode:
            return
        if self._narrow_mode and self._narrow_pane == "passage":
            self._last_answer_index = self.focused_answer_index()
        self._apply_pane_visibility()
        self.refresh_bindings()

    def _apply_pane_visibility(self) -> None:
        passage_pane = self.query_one("#passage-pane", Vertical)
        question_pane = self.query_one("#question-pane", Vertical)
        show_passage = self._narrow_pane == "passage"
        passage_pane.display = not self._narrow_mode or show_passage
        question_pane.display = not self._narrow_mode or not show_passage
        self.refresh(layout=True)

    def show_narrow_pane(self, pane: str, *, focus_content: bool = True) -> None:
        if pane not in {"passage", "questions"}:
            raise ValueError(f"Unknown practice pane: {pane}")
        if pane == "passage":
            self._last_answer_index = self.focused_answer_index()
            self.persist_progress()
        self._narrow_pane = pane
        self._apply_pane_visibility()
        if not self._narrow_mode or not focus_content:
            return
        if pane == "passage":
            self.set_focus(None)
        else:
            controls = list(self.query(".answer-control"))
            if controls:
                answer = controls[max(0, min(self._last_answer_index, len(controls) - 1))]
                self.call_after_refresh(self.focus_control, answer)

    def action_toggle_pane(self) -> None:
        if not self._narrow_mode:
            return
        next_pane = "questions" if self._narrow_pane == "passage" else "passage"
        self.show_narrow_pane(next_pane)

    def action_take_notes(self) -> None:
        if not self._submitted:
            self.persist_progress()
        self.app.push_screen(
            NotesScreen(
                self.exam["exam_id"],
                display_exam_title(self.exam["title"]),
                self.app.note_for_exam(self.exam["exam_id"]),
            )
        )

    def selected_passage_word(self) -> str:
        """Return one selected passage word, rejecting other or multi-word selections."""
        if not self.selections:
            return ""
        passage = self.query_one("#passage", Markdown)
        if any(
            widget is not passage and passage not in widget.ancestors
            for widget in self.selections
        ):
            return ""
        return normalise_word(self.get_selected_text())

    def action_open_dictionary(self) -> None:
        self.app.open_dictionary(self.selected_passage_word())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_pane":
            return self._narrow_mode
        if action == "highlight_selection":
            # Let Input / TextArea retain their native Ctrl+X when there is no
            # selected passage range waiting to be highlighted, or while the
            # user is actively editing an answer.
            return bool(self._pending_highlight_parts) and not isinstance(
                self.focused, (Input, TextArea)
            )
        return super().check_action(action, parameters)

    @on(events.TextSelected)
    def passage_text_selected(self) -> None:
        """Cache a passage selection until Highlight or Ctrl+X is activated."""
        if not self.selections:
            self._pending_highlight_parts = ()
            self._update_highlight_controls()
            return
        passage = self.query_one("#passage", Markdown)
        if any(
            widget is not passage and passage not in widget.ancestors
            for widget in self.selections
        ):
            self._pending_highlight_parts = ()
            self.clear_selection()
            self._update_highlight_controls()
            return

        parts: list[PassageHighlightPart] = []
        for widget, selection in self.selections.items():
            if not isinstance(widget, MarkdownBlock):
                continue
            base_content = self._article_base_content.get(widget)
            if base_content is None:
                content = widget.content
                if not isinstance(content, Content):
                    continue
                base_content = content
                self._article_base_content[widget] = base_content
            start, end = selection_character_span(base_content.plain, selection)
            quote = base_content.plain[start:end]
            if quote.strip():
                parts.append(PassageHighlightPart(widget, start, end, quote))

        if not parts:
            self._pending_highlight_parts = ()
            self.clear_selection()
            self._update_highlight_controls()
            return
        self._pending_highlight_parts = tuple(parts)
        self._update_highlight_controls()

    @on(events.MouseDown, "#highlight-selection")
    def highlight_button_mouse_down(self) -> None:
        """Apply before Textual clears the native selection on mouse-up."""
        self.action_highlight_selection()

    def action_highlight_selection(self) -> None:
        """Commit the cached passage selection as a persistent highlight."""
        if not self._pending_highlight_parts:
            return
        highlight = PassageHighlight(
            id=f"highlight-{self._next_highlight_id}",
            parts=self._pending_highlight_parts,
        )
        self._next_highlight_id += 1
        self.article_highlights.append(highlight)
        self._pending_highlight_parts = ()
        self.clear_selection()
        self._render_article_highlights()

    @on(events.Click)
    def remove_clicked_article_highlight(self, event: events.Click) -> None:
        """Clicking a persistent highlight removes that complete drag selection."""
        highlight_id = event.style.meta.get("highlight_id")
        if highlight_id is None:
            return
        self.remove_article_highlight(str(highlight_id))
        event.stop()

    def _render_article_highlights(self) -> None:
        spans_by_block: dict[MarkdownBlock, list[Span]] = {
            block: [] for block in self._article_base_content
        }
        for highlight in self.article_highlights:
            highlight_style = Style.parse("on #5E2750") + Style.from_meta(
                {"highlight_id": highlight.id}
            )
            for part in highlight.parts:
                spans_by_block.setdefault(part.block, []).append(
                    Span(part.start, part.end, highlight_style)
                )
        for block, base_content in self._article_base_content.items():
            spans = spans_by_block.get(block, [])
            block.set_content(base_content.add_spans(spans) if spans else base_content)
        self._update_highlight_controls()

    def _update_highlight_controls(self) -> None:
        count = len(self.article_highlights)
        status = (
            "Selection ready · click Highlight or press Ctrl+X"
            if self._pending_highlight_parts
            else "Select text, then click Highlight · Ctrl+X applies"
        )
        if count:
            status += f"  ·  {count} saved"
        self.query_one("#highlight-help", Static).update(status)
        self.query_one("#highlight-selection", Button).disabled = not bool(
            self._pending_highlight_parts
        )
        self.query_one("#undo-highlight", Button).disabled = count == 0
        self.query_one("#clear-highlights", Button).disabled = count == 0

    def remove_article_highlight(self, highlight_id: str) -> None:
        remaining = [
            highlight
            for highlight in self.article_highlights
            if highlight.id != highlight_id
        ]
        if len(remaining) == len(self.article_highlights):
            return
        self.article_highlights = remaining
        self._render_article_highlights()

    def undo_article_highlight(self) -> None:
        if not self.article_highlights:
            return
        self.article_highlights.pop()
        self._render_article_highlights()

    def clear_article_highlights(self) -> None:
        if not self.article_highlights and not self._pending_highlight_parts:
            self.clear_selection()
            return
        self.article_highlights.clear()
        self._pending_highlight_parts = ()
        self.clear_selection()
        self._render_article_highlights()

    def focus_control(self, control: Widget) -> None:
        if isinstance(control, CompletionEditor):
            control.focus_input()
        elif isinstance(control, InlineSelectEditor):
            control.focus_select()
        elif control.can_focus:
            control.focus()
        else:
            focusable = next((widget for widget in control.query("*") if widget.can_focus), None)
            if focusable is not None:
                focusable.focus()

    def restore_saved_answers(self) -> None:
        """Hydrate every native control, including occurrence-level inline slots."""
        slot_values = (
            self.saved_progress.get("slot_values")
            if isinstance(self.saved_progress.get("slot_values"), dict)
            else {}
        )
        inline_question_ids: set[str] = set()
        for editor in self.query(CompletionEditor):
            slots_by_question: dict[str, list[str]] = {}
            for slot_id in editor.field_ids:
                question_id = str(editor.slot_by_id[slot_id]["question_id"])
                slots_by_question.setdefault(question_id, []).append(slot_id)
                inline_question_ids.add(question_id)
            for question_id, slot_ids in slots_by_question.items():
                flattened = self.answers.get(question_id, "")
                fallback_parts = re.split(r"\s+/\s+", flattened, maxsplit=len(slot_ids) - 1)
                for occurrence, slot_id in enumerate(slot_ids):
                    if slot_id in slot_values:
                        editor.answers[slot_id] = str(slot_values[slot_id])
                    elif occurrence < len(fallback_parts):
                        editor.answers[slot_id] = fallback_parts[occurrence]
            editor.sync_input()

        for editor in self.query(InlineSelectEditor):
            for slot_id in editor.field_ids:
                question_id = str(editor.slot_by_id[slot_id]["question_id"])
                inline_question_ids.add(question_id)
                value = str(slot_values.get(slot_id, self.answers.get(question_id, "")))
                if value in editor.label_by_value:
                    editor.answers[slot_id] = value
            editor.sync_select()

        for index, question in enumerate(self.exam["questions"]):
            question_id = str(question["id"])
            if question_id in inline_question_ids or question_id not in self.answers:
                continue
            value = self.answers[question_id]
            control = self.query_one(f"#answer-{index}")
            if isinstance(control, Input):
                control.value = value
            elif isinstance(control, Select):
                if value in control._legal_values:
                    control.value = value
            elif isinstance(control, RadioSet):
                option_index = next(
                    (
                        option_index
                        for option_index, option in enumerate(question.get("options", []))
                        if str(option).casefold() == value.casefold()
                    ),
                    -1,
                )
                if option_index >= 0:
                    control.query(RadioButton)[option_index].value = True
            else:
                selected = {
                    part.strip().casefold()
                    for part in re.split(r"\s*/\s*", value)
                    if part.strip()
                }
                for option_index, checkbox in enumerate(control.query(Checkbox)):
                    checkbox.value = str(question["options"][option_index]).casefold() in selected

    def slot_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for editor in self.query(CompletionEditor):
            values.update(editor.answers)
        for editor in self.query(InlineSelectEditor):
            values.update(editor.answers)
        return values

    def elapsed_seconds(self) -> int:
        if self._frozen_duration_seconds is not None:
            return self._frozen_duration_seconds
        return self.elapsed_before_resume + max(0, int(time.monotonic() - self.started_monotonic))

    def refresh_practice_status(self) -> None:
        elapsed = self.elapsed_seconds()
        minutes, seconds = divmod(elapsed, 60)
        answered = sum(bool(value.strip()) for value in self.answers.values())
        self.query_one("#progress", Static).update(
            f"{minutes:02d}:{seconds:02d}  ·  {answered:02d} / "
            f"{len(self.exam['questions']):02d} answered"
        )

    def update_clock(self) -> None:
        if self._submitted:
            return
        self.refresh_practice_status()
        if self.elapsed_seconds() - self._last_persisted_elapsed >= 10:
            self.persist_progress()

    def stop_timers(self) -> None:
        if self._clock_timer is not None:
            self._clock_timer.stop()
            self._clock_timer = None
        if self._progress_save_timer is not None:
            self._progress_save_timer.stop()
            self._progress_save_timer = None

    def schedule_progress_save(self) -> None:
        if self._restoring_progress or self._submitted:
            return
        if self._progress_save_timer is not None:
            self._progress_save_timer.stop()
        self._progress_save_timer = self.set_timer(
            0.4,
            self.persist_progress,
            name="practice-progress-save",
        )

    def persist_progress(self) -> None:
        if self._restoring_progress or self._submitted or not self.is_mounted:
            return
        if self._progress_save_timer is not None:
            self._progress_save_timer.stop()
            self._progress_save_timer = None
        self.save_answers()
        elapsed = self.elapsed_seconds()
        if self.app.save_progress(
            self.exam,
            self.answers,
            self.slot_values(),
            self.started_at,
            elapsed,
        ):
            self._last_persisted_elapsed = elapsed

    def save_answers(self) -> None:
        inline_question_ids: set[str] = set()
        for editor in self.query(CompletionEditor):
            self.answers.update(editor.question_answers)
            inline_question_ids.update(editor.question_answers)
        for editor in self.query(InlineSelectEditor):
            self.answers.update(editor.question_answers)
            inline_question_ids.update(editor.question_answers)
        for index, question in enumerate(self.exam["questions"]):
            if question["id"] in inline_question_ids:
                continue
            control = self.query_one(f"#answer-{index}")
            if isinstance(control, Input):
                value = control.value
            elif isinstance(control, Select):
                value = "" if control.value is Select.NULL else str(control.value)
            elif isinstance(control, RadioSet):
                pressed_index = control.pressed_index
                value = question["options"][pressed_index] if pressed_index >= 0 else ""
            else:
                selected = [
                    question["options"][option_index]
                    for option_index, checkbox in enumerate(control.query(Checkbox))
                    if checkbox.value
                ]
                value = " / ".join(selected)
            self.answers[question["id"]] = value

    def update_progress(self) -> None:
        self.save_answers()
        self.refresh_practice_status()
        self.schedule_progress_save()

    def focused_answer_index(self) -> int:
        focused = self.focused
        for index, control in enumerate(self.query(".answer-control")):
            if focused is control or (focused is not None and focused in control.query("*")):
                return index
        return 0

    def focus_answer(self, index: int) -> None:
        if self._narrow_mode and self._narrow_pane != "questions":
            self.show_narrow_pane("questions", focus_content=False)
        controls = list(self.query(".answer-control"))
        bounded = max(0, min(index, len(controls) - 1))
        self.focus_control(controls[bounded])

    def action_previous_answer(self) -> None:
        self.focus_answer(self.focused_answer_index() - 1)

    def action_next_answer(self) -> None:
        self.focus_answer(self.focused_answer_index() + 1)

    def action_submit_exam(self) -> None:
        if self._submitted:
            self.notify("This attempt has already been saved", severity="warning")
            return
        self.save_answers()
        duration_seconds = self.elapsed_seconds()
        if not self.app.record_attempt(
            self.exam,
            self.answers,
            self.started_at,
            duration_seconds,
        ):
            self.persist_progress()
            return
        self._frozen_duration_seconds = duration_seconds
        self._submitted = True
        self.stop_timers()
        self.refresh_practice_status()
        self.app.push_screen(ResultScreen(self.exam, self.answers, duration_seconds))

    def action_library(self) -> None:
        self.persist_progress()
        self.app.pop_screen()

    @on(Input.Changed, ".answer-input")
    @on(Select.Changed, ".answer-heading")
    @on(RadioSet.Changed, ".answer-radio")
    @on(Checkbox.Changed, ".multi-option")
    def answer_changed(self) -> None:
        self.update_progress()

    @on(CompletionEditor.AnswerChanged)
    @on(InlineSelectEditor.AnswerChanged)
    def completion_answer_changed(self) -> None:
        self.update_progress()

    @on(Input.Submitted, ".answer-input")
    def answer_submitted(self) -> None:
        self.action_next_answer()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "submit": self.action_submit_exam,
            "library-button": self.action_library,
            "highlight-selection": self.action_highlight_selection,
            "undo-highlight": self.undo_article_highlight,
            "clear-highlights": self.clear_article_highlights,
        }
        if event.button.id in actions:
            actions[event.button.id]()


class IELTSApp(App[None]):
    """Full-screen Textual IELTS practice application."""

    TITLE = "Terminal IELTS"
    SUB_TITLE = "Reading practice"
    CSS = """
    Screen {
        background: $background; color: $foreground;
        scrollbar-color: $scrollbar; scrollbar-color-hover: $scrollbar-hover;
        scrollbar-color-active: $scrollbar-active; scrollbar-background: $scrollbar-background;
    }
    Header { background: $background-darken-1; color: $text-muted; }
    Footer { background: $footer-background; color: $text-muted; }
    #library { padding: 2 4; }
    .eyebrow { color: $text-muted; text-style: bold; height: 1; }
    #library-title { color: $foreground; text-style: bold; height: 3; padding-top: 1; }
    #library-stats { color: $text-muted; height: 2; }
    #filters { height: 3; margin: 1 0; }
    #search { width: 1fr; margin-right: 1; border: tall $border-blurred; }
    #category { width: 18; margin-right: 1; }
    #frequency { width: 22; }
    #exam-table { height: 1fr; border: round $border-blurred; background: $surface; }
    #exam-table > .datatable--header { background: $panel; color: $text-muted; text-style: bold; }
    #exam-table > .datatable--cursor { background: $secondary-background; color: $foreground; }
    #library-help { height: 2; padding-top: 1; color: $text-muted; text-align: center; }
    #practice { padding: 1 2; }
    #practice-topbar { height: 4; padding: 0 1; }
    #exam-title { color: $foreground; text-style: bold; height: 2; }
    #progress { width: 28; text-align: right; color: $text-muted; padding-top: 1; }
    #workspace { height: 1fr; }
    .pane { background: $background-darken-1; border: round $border-blurred; margin: 0 1; padding: 0 1; }
    #passage-pane { width: 58%; }
    #question-pane { width: 42%; }
    .pane-title { color: $text-muted; text-style: bold; height: 2; padding: 1 1 0 1; }
    #passage-tools { height: 3; margin: 0 1; }
    #highlight-help { width: 1fr; height: 3; padding: 1; color: $text-muted; }
    #passage-tools .highlight-action { min-width: 9; margin-left: 1; }
    #passage-scroll { background: $surface; color: $foreground; margin: 0 1 1 1; }
    #passage { padding: 1 4 3 4; color: $foreground; }
    #passage MarkdownH1, #passage MarkdownH2, #passage MarkdownH3, #passage MarkdownH4 {
        color: $foreground; text-style: bold; margin: 1 0;
    }
    #passage MarkdownParagraph { margin: 0 0 1 0; }
    #question-scroll { padding: 0 1 1 1; }
    .question-group { height: auto; margin-bottom: 2; }
    .group-title { height: auto; min-height: 1; color: $text-secondary; text-style: bold; margin: 1 0; }
    .group-instructions { height: auto; color: $text-muted; background: $panel; padding: 1 2; margin-bottom: 1; }
    .question-card { height: auto; border-left: thick $border; background: $surface; padding: 1 2; margin-bottom: 1; }
    .question-copy { height: auto; color: $foreground; }
    .question-copy MarkdownH3 { color: $text-secondary; background: transparent; text-style: bold; }
    .answer-hint { color: $text-muted; height: auto; min-height: 1; margin-top: 1; }
    .answer-input { margin-top: 1; border: tall $border-blurred; }
    .answer-heading { margin-top: 1; color: $text-muted; background: $panel; }
    .answer-heading > SelectCurrent { border: tall $border-blurred; background: $panel; }
    .answer-radio { height: auto; background: transparent; margin-top: 1; padding: 0 1; border: tall $border-blurred; }
    .answer-radio:focus { border: tall $primary; background-tint: $primary 4%; }
    .answer-radio RadioButton {
        width: 1fr; height: auto; color: $text-muted; background: transparent;
        text-wrap: wrap; text-overflow: fold;
    }
    .answer-radio > RadioButton > .toggle--button { color: $text-muted; background: $panel; }
    .answer-radio > RadioButton.-on > .toggle--button { color: $foreground; background: $secondary-background; }
    .answer-radio:focus > RadioButton.-selected > .toggle--label {
        color: $foreground; background: $secondary-background; text-style: bold;
    }
    .answer-multi { height: auto; margin-top: 1; padding: 0 1; }
    .answer-multi Checkbox {
        width: 1fr; height: auto; color: $text-muted; background: transparent;
        text-wrap: wrap; text-overflow: fold;
    }
    .answer-multi Checkbox.-on > .toggle--button { color: $foreground; background: $secondary-background; }
    .completion-help { height: auto; color: $text-muted; margin: 0 0 1 0; }
    .completion-editor {
        height: auto; min-height: 5; color: $foreground; background: $surface;
        border: round $border-blurred; padding: 1 2; margin-bottom: 1;
    }
    .completion-editor:focus-within { border: round $primary; background: $panel; }
    .completion-text { height: auto; color: $foreground; }
    .completion-entry { height: 3; margin-top: 1; }
    .completion-label { width: 14; height: 3; padding: 1 1; color: $text-muted; }
    .completion-native-input { width: 1fr; border: tall $border; }
    .completion-native-input:focus { border: tall $primary; }
    .completion-native-select { width: 1fr; color: $text-muted; background: $panel; }
    .completion-native-select > SelectCurrent { border: tall $border; background: $panel; }
    .answer-select:focus > SelectCurrent, .completion-native-select:focus > SelectCurrent {
        border: tall $primary; background-tint: $primary 4%;
    }
    .answer-select > SelectOverlay > .option-list--option-highlighted,
    .completion-native-select > SelectOverlay > .option-list--option-highlighted {
        color: $foreground; background: $secondary-background; text-style: bold;
    }
    .answer-input:focus, #search:focus { border: tall $primary; }
    #question-actions { height: 3; margin: 0 1 1 1; }
    #question-actions Button { margin-right: 1; min-width: 18; }
    PracticeScreen.narrow-workspace #practice { padding: 1; }
    PracticeScreen.narrow-workspace #workspace {
        layout: horizontal; overflow-x: hidden; overflow-y: hidden;
    }
    PracticeScreen.narrow-workspace .pane {
        width: 100%; height: 1fr; min-height: 8; margin: 0;
    }
    PracticeScreen.narrow-workspace #passage-pane,
    PracticeScreen.narrow-workspace #question-pane { width: 100%; }
    PracticeScreen.narrow-workspace #passage { padding: 1 2 3 2; }
    Button { background: $panel; color: $foreground; border: tall $border; }
    Button:hover { background: $secondary-background; }
    Button:focus { background: $secondary-background; color: $foreground; border: tall $primary; }
    NotesScreen { align: center middle; background: $background-darken-2 78%; }
    #notes-card {
        width: 90%; max-width: 100; min-width: 36; height: 75%; min-height: 16;
        padding: 1 2; border: round $border; background: $surface;
    }
    #notes-title { height: 2; color: $foreground; text-style: bold; }
    #notes-editor {
        height: 1fr; margin: 1 0; border: round $border-blurred;
        background: $background-darken-1; color: $foreground;
    }
    #notes-editor:focus { border: round $primary; }
    #notes-actions { height: 3; }
    #notes-actions Button { min-width: 14; margin-right: 1; }
    DictionaryScreen { align: center middle; background: $background-darken-2 78%; }
    #dictionary-card {
        width: 92%; max-width: 88; height: 75%; max-height: 30;
        padding: 1 2; border: round $border; background: $surface;
    }
    #dictionary-query {
        height: 3; margin: 1 0; border: tall $border-blurred;
        background: $background-darken-1; color: $foreground;
    }
    #dictionary-query:focus { border: tall $primary; }
    #dictionary-results {
        height: 1fr; padding: 1; border: round $border-blurred;
        background: $background-darken-1; color: $foreground;
    }
    #dictionary-word {
        width: 1fr; height: auto; min-height: 1; margin-bottom: 1;
        color: $foreground; text-style: bold; text-wrap: wrap;
    }
    #dictionary-meaning {
        width: 1fr; height: auto; min-height: 1; color: $foreground; text-wrap: wrap;
    }
    #dictionary-help {
        height: 2; padding-top: 1; color: $text-muted; text-align: right;
    }
    ResultScreen { align: center middle; background: $background-darken-2 78%; }
    #result-card { width: 72; height: 30; padding: 2 3; border: round $border; background: $surface; }
    #score { height: 4; padding-top: 1; color: $foreground; text-style: bold; text-align: center; }
    #corrections { height: 1fr; margin: 1 0; border: round $border-blurred; padding: 1; }
    #close-result { width: 100%; }
    """

    def __init__(self, bank: dict[str, Any], history_path: Path | None = None) -> None:
        super().__init__()
        self.register_theme(UBUNTU_GNOME_THEME)
        self.theme = UBUNTU_GNOME_THEME.name
        self.bank = bank
        self.exam_by_id = {exam["exam_id"]: exam for exam in bank["exams"]}
        self.dictionary = E2CDictionary()
        self.data_path = history_path or DEFAULT_DATA_PATH
        self.history_path = self.data_path
        if history_path is None and not self.data_path.exists():
            legacy_path = Path.cwd() / "practice_history.jsonl"
            self.practice_data = (
                load_practice_data(legacy_path)
                if legacy_path.exists()
                else load_practice_data(self.data_path)
            )
        else:
            self.practice_data = load_practice_data(self.data_path)

    def copy_to_clipboard(self, text: str) -> tuple[bool, str]:
        """Copy through Textual, plus native macOS/Linux clipboard backends.

        Returns (success, error_message).
        """
        super().copy_to_clipboard(text)
        if self.is_web or self.is_headless:
            return True, ""
        return copy_to_native_clipboard(text)

    @property
    def completed_exam_ids(self) -> set[str]:
        return {
            str(record.get("exam_id"))
            for record in self.practice_data.get("attempts", [])
            if isinstance(record, dict) and record.get("exam_id")
        }

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen(self.bank))

    def start_exam(self, exam_id: str) -> None:
        progress = self.practice_data.get("progress", {}).get(exam_id)
        self.push_screen(PracticeScreen(self.exam_by_id[exam_id], progress))

    def open_dictionary(self, initial_query: str = "") -> None:
        self.push_screen(DictionaryScreen(self.dictionary, initial_query))

    def _write_practice_data(self, data: dict[str, Any]) -> bool:
        try:
            write_practice_data(self.data_path, data)
        except OSError as error:
            self.notify(f"Could not save practice data: {error}", severity="warning")
            return False
        self.practice_data = data
        return True

    def note_for_exam(self, exam_id: str) -> str:
        note = self.practice_data.get("notes", {}).get(exam_id, {})
        if isinstance(note, str):
            return note
        return str(note.get("text", "")) if isinstance(note, dict) else ""

    def save_note(self, exam_id: str, text: str) -> bool:
        notes = dict(self.practice_data.get("notes", {}))
        if text.strip():
            notes[exam_id] = {
                "schema_version": 1,
                "text": text,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "bank_commit": str(self.bank.get("source", {}).get("commit", "")),
            }
        else:
            notes.pop(exam_id, None)
        data = dict(self.practice_data)
        data["notes"] = notes
        return self._write_practice_data(data)

    def save_progress(
        self,
        exam: dict[str, Any],
        answers: dict[str, str],
        slot_values: dict[str, str],
        started_at: datetime,
        elapsed_seconds: int,
    ) -> bool:
        progress = dict(self.practice_data.get("progress", {}))
        progress[exam["exam_id"]] = {
            "schema_version": 1,
            "exam_id": exam["exam_id"],
            "started_at": started_at.isoformat(timespec="seconds"),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": max(0, int(elapsed_seconds)),
            "answers": dict(answers),
            "slot_values": dict(slot_values),
            "bank_commit": str(self.bank.get("source", {}).get("commit", "")),
        }
        data = dict(self.practice_data)
        data.update(
            {
                "attempts": list(self.practice_data.get("attempts", [])),
                "progress": progress,
            }
        )
        return self._write_practice_data(data)

    def record_attempt(
        self,
        exam: dict[str, Any],
        answers: dict[str, str],
        started_at: datetime,
        duration_seconds: int,
    ) -> bool:
        correct = sum(
            question_is_correct(question, answers.get(question["id"], ""))
            for question in exam["questions"]
        )
        total = len(exam["questions"])
        attempted = sum(bool(answers.get(question["id"], "").strip()) for question in exam["questions"])
        completed_at = datetime.now().astimezone()
        record = {
            "schema_version": 2,
            "started_at": started_at.isoformat(timespec="seconds"),
            "completed_at": completed_at.isoformat(timespec="seconds"),
            "duration_seconds": max(0, int(duration_seconds)),
            "exam_id": exam["exam_id"],
            "title": exam["title"],
            "category": exam["category"],
            "frequency": exam["frequency"],
            "attempted": attempted,
            "correct": correct,
            "total": total,
            "accuracy": correct / total if total else 0.0,
            "answers": dict(answers),
            "question_results": [
                {
                    "question_id": question["id"],
                    "number": question["number"],
                    "kind": question.get("kind", ""),
                    "user_answer": answers.get(question["id"], ""),
                    "expected_answer": question["answer"],
                    "attempted": bool(answers.get(question["id"], "").strip()),
                    "correct": question_is_correct(question, answers.get(question["id"], "")),
                }
                for question in exam["questions"]
            ],
        }
        progress = dict(self.practice_data.get("progress", {}))
        progress.pop(exam["exam_id"], None)
        data = dict(self.practice_data)
        data.update(
            {
                "attempts": [*self.practice_data.get("attempts", []), record],
                "progress": progress,
            }
        )
        return self._write_practice_data(data)
