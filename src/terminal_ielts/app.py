"""Textual terminal interface for IELTS reading practice."""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.message import Message
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
)

from .bank import answer_is_correct


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
        for slot in slots:
            slot_id = str(slot["slot_id"])
            question_id = str(slot["question_id"])
            number = str(question_by_id[question_id]["number"])
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
                label = f" {number}: {blank} "
                is_active = slot_id == self.active_slot_id
                input_widget = self.query_one(CompletionInput) if self.is_mounted else None
                if is_active and input_widget is not None and input_widget.has_focus:
                    rendered.append(label, style="bold reverse #c0c3c5 on #3a3d40")
                elif answer:
                    rendered.append(label, style="underline #b6babd")
                else:
                    rendered.append(label, style="underline #747a7f")
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
        self.numbers = {
            slot_id: str(question_by_id[str(self.slot_by_id[slot_id]["question_id"])]["number"])
            for slot_id in self.field_ids
        }
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
                label = f" {self.numbers[slot_id]}: {shown} "
                if slot_id == self.active_slot_id:
                    rendered.append(label, style="bold reverse #c0c3c5 on #3a3d40")
                elif answer:
                    rendered.append(label, style="underline #b6babd")
                else:
                    rendered.append(label, style="underline #747a7f")
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


class LibraryScreen(Screen[None]):
    BINDINGS = [
        Binding("/", "focus_search", "Search", show=True),
        Binding("enter", "open_selected", "Practice", show=True),
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
        table.add_columns("Part", "Frequency", "Passage", "Questions")
        self.apply_filters()
        table.focus()

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
                exam["category"],
                exam["frequency"].title(),
                exam["title"],
                str(len(exam["questions"])),
                key=exam["exam_id"],
            )
        self.query_one("#library-help", Static).update(
            f"{len(self.filtered)} shown  ·  ↑↓ select  ·  Enter practice  ·  / search  ·  R random"
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

    def action_quit(self) -> None:
        self.app.exit()


class PracticeScreen(Screen[None]):
    BINDINGS = [
        Binding("ctrl+up", "previous_answer", "Previous answer"),
        Binding("ctrl+down", "next_answer", "Next answer"),
        Binding("ctrl+s", "submit_exam", "Submit"),
        Binding("escape", "library", "Library"),
    ]

    def __init__(self, exam: dict[str, Any]) -> None:
        super().__init__()
        self.exam = exam
        self.answers: dict[str, str] = {}
        self.started_at = datetime.now().astimezone()
        self.started_monotonic = time.monotonic()
        self._submitted = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="practice"):
            with Horizontal(id="practice-topbar"):
                with Vertical():
                    yield Static(self.exam["category"] + "  /  " + self.exam["frequency"].upper(), classes="eyebrow")
                    yield Static(self.exam["title"], id="exam-title")
                yield Static("", id="progress")
            with Horizontal(id="workspace"):
                with Vertical(classes="pane", id="passage-pane"):
                    yield Static("READING PASSAGE", classes="pane-title")
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
                                                        yield RadioButton(option_display(option))
                                            elif is_multiple_selection(question) and question.get("options"):
                                                yield Static("Select all required answers", classes="answer-hint")
                                                with Vertical(id=f"answer-{index}", classes="answer-control answer-multi"):
                                                    for option_index, option in enumerate(question_option_items(question)):
                                                        yield Checkbox(
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
        first_answer = self.query(".answer-control").first(Widget)
        self.focus_control(first_answer)
        self.update_progress()

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
        self.query_one("#progress", Static).update(
            f"{len([value for value in self.answers.values() if value.strip()]):02d} / "
            f"{len(self.exam['questions']):02d} answered"
        )

    def focused_answer_index(self) -> int:
        focused = self.focused
        for index, control in enumerate(self.query(".answer-control")):
            if focused is control or (focused is not None and focused in control.query("*")):
                return index
        return 0

    def focus_answer(self, index: int) -> None:
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
        duration_seconds = max(0, round(time.monotonic() - self.started_monotonic))
        self._submitted = True
        self.app.record_attempt(self.exam, self.answers, self.started_at, duration_seconds)
        self.app.push_screen(ResultScreen(self.exam, self.answers, duration_seconds))

    def action_library(self) -> None:
        self.save_answers()
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
        }
        if event.button.id in actions:
            actions[event.button.id]()


class IELTSApp(App[None]):
    """Full-screen Textual IELTS practice application."""

    TITLE = "Terminal IELTS"
    SUB_TITLE = "Reading practice"
    CSS = """
    Screen {
        background: #151718; color: #b7bcc2;
        scrollbar-color: #3d4246; scrollbar-color-hover: #4c5256;
        scrollbar-color-active: #5a6065; scrollbar-background: #151718;
    }
    Header { background: #191b1d; color: #858b90; }
    Footer { background: #191b1d; color: #747a7f; }
    #library { padding: 2 4; }
    .eyebrow { color: #777e84; text-style: bold; height: 1; }
    #library-title { color: #c2c6c9; text-style: bold; height: 3; padding-top: 1; }
    #library-stats { color: #747a7f; height: 2; }
    #filters { height: 3; margin: 1 0; }
    #search { width: 1fr; margin-right: 1; border: tall #35383b; }
    #category { width: 18; margin-right: 1; }
    #frequency { width: 22; }
    #exam-table { height: 1fr; border: round #303336; background: #191b1d; }
    #exam-table > .datatable--header { background: #292c2e; color: #b5b9bc; text-style: bold; }
    #exam-table > .datatable--cursor { background: #35383b; color: #d0d2d4; }
    #library-help { height: 2; padding-top: 1; color: #666b6f; text-align: center; }
    #practice { padding: 1 2; }
    #practice-topbar { height: 4; padding: 0 1; }
    #exam-title { color: #c7c9cb; text-style: bold; height: 2; }
    #progress { width: 28; text-align: right; color: #858a8e; padding-top: 1; }
    #workspace { height: 1fr; }
    .pane { background: #191b1d; border: round #303336; margin: 0 1; padding: 0 1; }
    #passage-pane { width: 58%; }
    #question-pane { width: 42%; }
    .pane-title { color: #858b90; text-style: bold; height: 2; padding: 1 1 0 1; }
    #passage-scroll { background: #1d1f21; color: #b7bbbe; margin: 0 1 1 1; }
    #passage { padding: 1 4 3 4; color: #b7bbbe; }
    #passage MarkdownH1, #passage MarkdownH2, #passage MarkdownH3, #passage MarkdownH4 {
        color: #c5c8ca; text-style: bold; margin: 1 0;
    }
    #passage MarkdownParagraph { margin: 0 0 1 0; }
    #question-scroll { padding: 0 1 1 1; }
    .question-group { height: auto; margin-bottom: 2; }
    .group-title { height: auto; min-height: 1; color: #8e9498; text-style: bold; margin: 1 0; }
    .group-instructions { height: auto; color: #aeb2b5; background: #202224; padding: 1 2; margin-bottom: 1; }
    .question-card { height: auto; border-left: thick #3b3f42; background: #1d1f21; padding: 1 2; margin-bottom: 1; }
    .question-copy { height: auto; color: #c0c3c5; }
    .question-copy MarkdownH3 { color: #8e9498; background: transparent; text-style: bold; }
    .answer-hint { color: #777d81; height: auto; min-height: 1; margin-top: 1; }
    .answer-input { margin-top: 1; border: tall #393d40; }
    .answer-heading { margin-top: 1; color: #aeb2b5; background: #202224; }
    .answer-heading > SelectCurrent { border: tall #393d40; background: #202224; }
    .answer-radio { height: auto; background: transparent; margin-top: 1; padding: 0 1; border: tall #393d40; }
    .answer-radio:focus { border: tall #565b5f; background-tint: #ffffff 2%; }
    .answer-radio RadioButton { color: #aeb2b5; background: transparent; }
    .answer-radio > RadioButton > .toggle--button { color: #666c70; background: #25282a; }
    .answer-radio > RadioButton.-on > .toggle--button { color: #c0c3c5; background: #25282a; }
    .answer-radio:focus > RadioButton.-selected > .toggle--label {
        color: #c4c7c9; background: #303336; text-style: bold;
    }
    .answer-multi { height: auto; margin-top: 1; padding: 0 1; }
    .answer-multi Checkbox { color: #aeb2b5; background: transparent; }
    .answer-multi Checkbox.-on > .toggle--button { color: #c0c3c5; background: #25282a; }
    .completion-help { height: auto; color: #747a7f; margin: 0 0 1 0; }
    .completion-editor {
        height: auto; min-height: 5; color: #b9bdc0; background: #1d1f21;
        border: round #3b3f42; padding: 1 2; margin-bottom: 1;
    }
    .completion-editor:focus-within { border: round #5a6065; background: #202224; }
    .completion-text { height: auto; color: #b9bdc0; }
    .completion-entry { height: 3; margin-top: 1; }
    .completion-label { width: 14; height: 3; padding: 1 1; color: #858b90; }
    .completion-native-input { width: 1fr; border: tall #45494d; }
    .completion-native-input:focus { border: tall #5a6065; }
    .completion-native-select { width: 1fr; color: #aeb2b5; background: #202224; }
    .completion-native-select > SelectCurrent { border: tall #45494d; background: #202224; }
    .answer-select:focus > SelectCurrent, .completion-native-select:focus > SelectCurrent {
        border: tall #5a6065; background-tint: #ffffff 2%;
    }
    .answer-select > SelectOverlay > .option-list--option-highlighted,
    .completion-native-select > SelectOverlay > .option-list--option-highlighted {
        color: #c4c7c9; background: #34383b; text-style: bold;
    }
    .answer-input:focus, #search:focus { border: tall #5a6065; }
    #question-actions { height: 3; margin: 0 1 1 1; }
    #question-actions Button { margin-right: 1; min-width: 18; }
    Button { background: #292c2e; color: #b8bbbd; border: tall #3a3e41; }
    Button:hover { background: #34383b; }
    Button:focus { background: #34383b; color: #c5c8ca; border: tall #565b5f; }
    ResultScreen { align: center middle; background: #101112 75%; }
    #result-card { width: 72; height: 30; padding: 2 3; border: round #44494d; background: #1b1d1f; }
    #score { height: 4; padding-top: 1; color: #bfc2c4; text-style: bold; text-align: center; }
    #corrections { height: 1fr; margin: 1 0; border: round #35393c; padding: 1; }
    #close-result { width: 100%; }
    """

    def __init__(self, bank: dict[str, Any], history_path: Path | None = None) -> None:
        super().__init__()
        self.bank = bank
        self.exam_by_id = {exam["exam_id"]: exam for exam in bank["exams"]}
        self.history_path = history_path or Path.cwd() / "practice_history.jsonl"

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen(self.bank))

    def start_exam(self, exam_id: str) -> None:
        self.push_screen(PracticeScreen(self.exam_by_id[exam_id]))

    def record_attempt(
        self,
        exam: dict[str, Any],
        answers: dict[str, str],
        started_at: datetime,
        duration_seconds: int,
    ) -> None:
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
            "attempted": attempted,
            "correct": correct,
            "total": total,
            "accuracy": correct / total if total else 0.0,
            "answers": dict(answers),
        }
        try:
            import json

            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            self.notify(f"Could not save history: {error}", severity="warning")
