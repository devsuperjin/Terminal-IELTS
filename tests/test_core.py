from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Input, RadioButton, RadioSet, Select, Static, TextArea

from terminal_ielts.app import (
    CompletionEditor,
    DictionaryScreen,
    IELTSApp,
    InlineSelectEditor,
    LibraryScreen,
    NotesScreen,
    PracticeScreen,
    ResultScreen,
    UBUNTU_GNOME_THEME,
    _clipboard_commands,
    copy_to_native_clipboard,
    display_exam_title,
    heading_select_options,
    question_is_correct,
)
from terminal_ielts.bank import answer_is_correct, load_bank
from terminal_ielts.dictionary import E2CDictionary
from terminal_ielts.extractor import normalise_exam, parse_registry_file
from terminal_ielts.history import (
    DEFAULT_DATA_PATH,
    aggregate_history,
    read_history,
    write_practice_data,
)


SAMPLE_PAYLOAD = {
    "examId": "sample-p1",
    "meta": {"title": "Sample passage", "category": "P1", "frequency": "high"},
    "passage": {"blocks": [{"html": "<p><b>A</b> Passage text.</p>"}]},
    "questionGroups": [
        {
            "groupId": "g1",
            "kind": "true_false_not_given",
            "questionIds": ["q1"],
            "bodyHtml": "<div><h4>Questions 1</h4><p>Choose TRUE, FALSE or NOT GIVEN.</p>"
            "<div class='tfng-item'><p><b>1</b> A sample statement.</p>"
            "<label><input type='radio' name='q1' value='TRUE'>TRUE</label>"
            "<label><input type='radio' name='q1' value='FALSE'>FALSE</label></div></div>",
        }
    ],
    "answerKey": {"q1": "TRUE"},
    "questionOrder": ["q1"],
    "questionDisplayMap": {"q1": "1"},
}


class ExtractionTests(unittest.TestCase):
    def test_bundled_bank_has_complete_real_source_coverage(self) -> None:
        bank = load_bank()
        self.assertEqual(bank["stats"]["exam_count"], 234)
        self.assertEqual(bank["stats"]["question_count"], 3143)
        self.assertEqual(len(bank["exams"]), 234)
        self.assertTrue(all(exam["passage"] for exam in bank["exams"]))
        self.assertTrue(
            all(question["prompt"] and question["answer"] for exam in bank["exams"] for question in exam["questions"])
        )

    def test_display_titles_are_english_only_without_mutating_source_data(self) -> None:
        bank = load_bank()
        cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
        titles = {exam["exam_id"]: exam["title"] for exam in bank["exams"]}
        self.assertEqual(display_exam_title(titles["p1-high-01"]), "A Brief History of Tea")
        self.assertEqual(display_exam_title(titles["p2-low-87"]), "Speaking of Nothing [Pretest]")
        self.assertEqual(display_exam_title(titles["p3-medium-169"]), "Music Language We All Speak")
        self.assertTrue(all(display_exam_title(title) for title in titles.values()))
        self.assertFalse(any(cjk.search(display_exam_title(title)) for title in titles.values()))
        self.assertIn("茶叶简史", titles["p1-high-01"])

    def test_normalise_exam_extracts_passage_prompt_options_and_answer(self) -> None:
        exam = normalise_exam(SAMPLE_PAYLOAD)
        self.assertEqual(exam["exam_id"], "sample-p1")
        self.assertIn("Passage text", exam["passage"])
        self.assertIn("**A**", exam["passage"])
        self.assertIn("sample statement", exam["questions"][0]["prompt"])
        self.assertEqual(exam["questions"][0]["options"], ["TRUE", "FALSE"])
        self.assertEqual(exam["questions"][0]["answer"], "TRUE")

    def test_parse_registry_file_rejects_non_registry_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.js"
            path.write_text("const nope = true;", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_registry_file(path)

    def test_answer_scoring_is_case_and_whitespace_insensitive(self) -> None:
        self.assertTrue(answer_is_correct(" not   given ", "NOT GIVEN"))
        self.assertTrue(answer_is_correct("colour", "color / colour"))
        self.assertTrue(answer_is_correct("four", "4/four"))
        self.assertTrue(answer_is_correct("G, C, E", "C / E / G"))
        self.assertFalse(answer_is_correct("C", "C / E / G"))
        self.assertFalse(answer_is_correct("false", "TRUE"))
        self.assertTrue(
            answer_is_correct(
                "breathing / eating",
                "breathing / eating",
                positional=True,
            )
        )
        self.assertFalse(
            answer_is_correct("breathing", "breathing / eating", positional=True)
        )

    def test_every_option_question_has_a_typed_control_and_scoreable_value(self) -> None:
        bank = load_bank()
        option_questions = [
            question
            for exam in bank["exams"]
            for question in exam["questions"]
            if question.get("options")
        ]
        self.assertGreater(len(option_questions), 2100)
        self.assertTrue(
            all(
                question["response_mode"] in {"radio_one", "checkbox_many", "select_one"}
                for question in option_questions
            )
        )
        for question in option_questions:
            values = {value.casefold() for value in question["options"]}
            expected_parts = [
                part.strip().casefold()
                for part in re.split(r"\s*/\s*", question["answer"])
            ]
            self.assertTrue(all(part in values for part in expected_parts), question)
            self.assertTrue(all(item["label"].strip() for item in question["option_items"]))

    def test_mixed_legacy_group_does_not_leak_word_bank_into_radio_questions(self) -> None:
        exam = next(exam for exam in load_bank()["exams"] if exam["exam_id"] == "p3-medium-162")
        by_number = {question["number"]: question for question in exam["questions"]}
        self.assertEqual(by_number["27"]["response_mode"], "radio_one")
        self.assertIn("universality", by_number["27"]["option_items"][0]["label"])
        self.assertNotIn("correct", by_number["27"]["option_items"][0]["label"])
        self.assertEqual(by_number["32"]["response_mode"], "select_one")
        self.assertEqual(by_number["32"]["option_items"][0], {"value": "A", "label": "correct"})
        self.assertEqual(by_number["37"]["response_mode"], "radio_one")
        self.assertEqual(by_number["37"]["options"], ["YES", "NO", "NOT GIVEN"])

        second_exam = next(
            exam for exam in load_bank()["exams"] if exam["exam_id"] == "p3-medium-183"
        )
        second_by_number = {question["number"]: question for question in second_exam["questions"]}
        self.assertEqual(second_by_number["32"]["response_mode"], "radio_one")
        self.assertIn("Anxiety", second_by_number["32"]["option_items"][0]["label"])
        self.assertIn("tell one colour", second_by_number["34"]["option_items"][0]["label"])

    def test_matching_and_completion_schema_has_no_overlap_or_lost_slots(self) -> None:
        bank = load_bank()
        sentence_groups = [
            group
            for exam in bank["exams"]
            for group in exam["groups"]
            if group["kind"] == "sentence_completion"
        ]
        self.assertEqual(len(sentence_groups), 82)
        self.assertEqual(sum(group.get("response_mode") == "inline_text" for group in sentence_groups), 76)
        self.assertEqual(sum(group.get("subtype") == "ending_select" for group in sentence_groups), 6)
        self.assertEqual(
            sum(len(group.get("completion_slots", [])) for group in sentence_groups),
            428,
        )
        for exam in bank["exams"]:
            owned = [question_id for group in exam["groups"] for question_id in group["question_ids"]]
            self.assertEqual(len(owned), len(set(owned)), exam["exam_id"])
            for group in exam["groups"]:
                if group["kind"] not in {"matching", "classification"}:
                    continue
                matching_questions = [
                    question
                    for question in exam["questions"]
                    if question["group_id"] == group["id"]
                ]
                self.assertTrue(
                    all(question["response_mode"] == "select_one" for question in matching_questions),
                    (exam["exam_id"], group["id"]),
                )
                if group.get("response_mode") != "inline_select":
                    prompts = [question["prompt"] for question in matching_questions]
                    self.assertTrue(
                        all(len(prompt) <= 500 for prompt in prompts),
                        (exam["exam_id"], group["id"]),
                    )
                    self.assertEqual(
                        len(prompts),
                        len(set(prompts)),
                        (exam["exam_id"], group["id"]),
                    )
                if group.get("subtype") == "heading_select":
                    self.assertTrue(
                        all(len(question["prompt"]) <= 100 for question in matching_questions),
                        (exam["exam_id"], group["id"]),
                    )
        positional = {
            (exam["exam_id"], question["number"]): question["slot_answers"]
            for exam in bank["exams"]
            for question in exam["questions"]
            if question.get("answer_mode") == "positional"
        }
        self.assertEqual(
            positional,
            {
                ("p3-low-83", "34"): ["inspiration", "elaboration"],
                ("p3-low-187", "33"): ["breathing", "eating"],
            },
        )

    def test_mislabelled_short_answer_notes_are_inline_sentence_completion(self) -> None:
        exam = next(exam for exam in load_bank()["exams"] if exam["exam_id"] == "p1-high-194")
        group = next(group for group in exam["groups"] if group["id"] == "group-2")
        self.assertEqual(group["source_kind"], "short_answer")
        self.assertEqual(group["kind"], "sentence_completion")
        self.assertEqual(group["response_mode"], "inline_text")
        self.assertIn("Woollen cloth manufacture", group["completion_template"])
        self.assertIn("[[[q6:0]]]", group["completion_template"])
        self.assertEqual(
            {slot["question_id"] for slot in group["completion_slots"]},
            {f"q{number}" for number in range(6, 14)},
        )


class AppTests(unittest.IsolatedAsyncioTestCase):
    def test_native_clipboard_backends_cover_macos_wayland_and_x11(self) -> None:
        with patch("terminal_ielts.app.sys.platform", "darwin"), patch(
            "terminal_ielts.app._find_executable", return_value="/usr/bin/pbcopy"
        ):
            self.assertEqual(_clipboard_commands(), [["/usr/bin/pbcopy"]])

        def linux_executable(command: str) -> str | None:
            return {"wl-copy": "/usr/bin/wl-copy", "xclip": "/usr/bin/xclip"}.get(command)

        with patch("terminal_ielts.app.sys.platform", "linux"), patch.dict(
            "terminal_ielts.app.os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True
        ), patch("terminal_ielts.app._find_executable", side_effect=linux_executable):
            self.assertEqual(
                _clipboard_commands(),
                [["/usr/bin/wl-copy"], ["/usr/bin/xclip", "-selection", "clipboard"]],
            )

        with patch("terminal_ielts.app.sys.platform", "linux"), patch.dict(
            "terminal_ielts.app.os.environ", {"DISPLAY": ":0"}, clear=True
        ), patch("terminal_ielts.app._find_executable", side_effect=linux_executable):
            self.assertEqual(
                _clipboard_commands(),
                [
                    ["/usr/bin/xclip", "-selection", "clipboard"],
                    ["/usr/bin/wl-copy"],
                ],
            )

        with patch(
            "terminal_ielts.app._clipboard_commands",
            return_value=[["/usr/bin/pbcopy"]],
        ), patch("terminal_ielts.app.subprocess.Popen") as popen:
            process = popen.return_value
            process.returncode = 0
            success, error = copy_to_native_clipboard("selected text\nsecond line")
            self.assertTrue(success)
            self.assertEqual(error, "")
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], ["/usr/bin/pbcopy"])
            process.stdin.write.assert_called_once_with(
                b"selected text\nsecond line"
            )
            process.stdin.close.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=2)

    async def test_dictionary_opens_from_library_and_selected_passage_word(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            dictionary_path = Path(directory) / "E2Cdictionary.js"
            dictionary_path.write_text('$maori:"毛利人的",\n', encoding="utf-8")
            app = IELTSApp(bank, Path(directory) / "history.jsonl")
            app.dictionary = E2CDictionary(dictionary_path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("f4")
                await pilot.pause()
                self.assertIsInstance(app.screen, DictionaryScreen)
                self.assertTrue(app.screen.query_one("#dictionary-query", Input).has_focus)

                await pilot.press("escape")
                await pilot.pause()
                app.start_exam("p1-low-02")
                await pilot.pause()
                practice = app.screen
                paragraph = next(
                    paragraph
                    for paragraph in practice.query("#passage MarkdownParagraph")
                    if "maori" in paragraph.content.plain.casefold()
                )
                text = paragraph.content.plain
                start = text.casefold().index("maori")
                await pilot.mouse_down(paragraph, offset=(start, 0))
                await pilot.hover(paragraph, offset=(start + len("Maori"), 0))
                await pilot.mouse_up(paragraph, offset=(start + len("Maori"), 0))
                await pilot.pause()
                self.assertEqual(practice.selected_passage_word(), "maori")

                await pilot.press("ctrl+d")
                await pilot.pause()
                self.assertIsInstance(app.screen, DictionaryScreen)
                self.assertEqual(
                    app.screen.query_one("#dictionary-query", Input).value,
                    "maori",
                )
                self.assertEqual(
                    str(app.screen.query_one("#dictionary-word", Static).render()),
                    "maori",
                )

    async def test_passage_selection_requires_explicit_highlight_and_can_be_removed(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "history.jsonl")
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app.start_exam("p1-low-02")
                await pilot.pause()
                screen = app.screen
                paragraph = screen.query("#passage MarkdownParagraph").first()
                base_content = paragraph.content

                await pilot.mouse_down(paragraph, offset=(0, 0))
                await pilot.hover(paragraph, offset=(12, 0))
                await pilot.mouse_up(paragraph, offset=(12, 0))
                await pilot.pause()
                self.assertEqual(screen.article_highlights, [])
                self.assertTrue(screen._pending_highlight_parts)
                self.assertFalse(
                    screen.query_one("#highlight-selection", Button).disabled
                )
                selected = screen.get_selected_text()
                self.assertEqual(selected, screen._pending_highlight_parts[0].quote)
                self.assertEqual(app.clipboard, "")
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertEqual(app.clipboard, selected)

                await pilot.click("#highlight-selection")
                await pilot.pause()
                self.assertEqual(len(screen.article_highlights), 1)
                self.assertFalse(screen._pending_highlight_parts)
                self.assertTrue(
                    screen.query_one("#highlight-selection", Button).disabled
                )
                highlight_id = screen.article_highlights[0].id
                self.assertTrue(
                    any(
                        getattr(span.style, "meta", {}).get("highlight_id") == highlight_id
                        for span in paragraph.content.spans
                    )
                )

                await pilot.resize_terminal(96, 40)
                await pilot.pause()
                self.assertEqual(screen.article_highlights[0].id, highlight_id)
                self.assertTrue(
                    any(
                        getattr(span.style, "meta", {}).get("highlight_id") == highlight_id
                        for span in paragraph.content.spans
                    )
                )
                await pilot.resize_terminal(140, 40)
                await pilot.pause()

                await pilot.click(paragraph, offset=(3, 0))
                await pilot.pause()
                self.assertEqual(screen.article_highlights, [])
                self.assertEqual(paragraph.content, base_content)

                for index, (start, end) in enumerate(((0, 8), (15, 24))):
                    await pilot.mouse_down(paragraph, offset=(start, 0))
                    await pilot.hover(paragraph, offset=(end, 0))
                    await pilot.mouse_up(paragraph, offset=(end, 0))
                    await pilot.pause()
                    self.assertEqual(len(screen.article_highlights), index)
                    if index == 0:
                        await pilot.click("#highlight-selection")
                    else:
                        await pilot.press("ctrl+x")
                    await pilot.pause()
                self.assertEqual(len(screen.article_highlights), 2)
                screen.undo_article_highlight()
                self.assertEqual(len(screen.article_highlights), 1)
                screen.clear_article_highlights()
                self.assertEqual(screen.article_highlights, [])
                self.assertEqual(paragraph.content, base_content)

    async def test_ctrl_x_keeps_native_input_cut_even_with_pending_passage_selection(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "state.json")
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.start_exam("p1-high-211")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, PracticeScreen)

                paragraph = screen.query("#passage MarkdownParagraph").first()
                await pilot.mouse_down(paragraph, offset=(0, 0))
                await pilot.hover(paragraph, offset=(8, 0))
                await pilot.mouse_up(paragraph, offset=(8, 0))
                await pilot.pause()
                self.assertTrue(screen._pending_highlight_parts)

                answer_input = screen.query_one("#answer-4", Input)
                answer_input.value = "cut this answer"
                answer_input.focus()
                answer_input.select_all()
                await pilot.press("ctrl+x")
                await pilot.pause()

                self.assertEqual(answer_input.value, "")
                self.assertEqual(screen.article_highlights, [])

    async def test_practice_workspace_switches_single_pane_when_narrow(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "history.jsonl")
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app.start_exam("p1-low-02")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, PracticeScreen)
                passage_pane = screen.query_one("#passage-pane")
                question_pane = screen.query_one("#question-pane")
                self.assertFalse(screen.has_class("narrow-workspace"))
                self.assertFalse(screen.check_action("toggle_pane", ()))
                self.assertTrue(passage_pane.display)
                self.assertTrue(question_pane.display)
                self.assertEqual(passage_pane.region.y, question_pane.region.y)
                self.assertGreater(question_pane.region.x, passage_pane.region.x)

                first_radio = screen.query(RadioSet).first(RadioSet)
                first_radio.query(RadioButton).first(RadioButton).value = True
                await pilot.resize_terminal(96, 40)
                await pilot.pause()
                self.assertTrue(screen.has_class("narrow-workspace"))
                self.assertTrue(screen.check_action("toggle_pane", ()))
                self.assertTrue(passage_pane.display)
                self.assertFalse(question_pane.display)
                self.assertGreater(screen.query_one("#passage-scroll").max_scroll_y, 0)
                self.assertTrue(first_radio.query(RadioButton).first(RadioButton).value)

                toggle_key = next(
                    key for key in screen.query("FooterKey") if getattr(key, "key", None) == "f2"
                )
                self.assertFalse(toggle_key.has_class("-disabled"))
                await pilot.click(toggle_key)
                await pilot.pause()
                self.assertFalse(passage_pane.display)
                self.assertTrue(question_pane.display)
                self.assertIs(screen.query_one(f"#{first_radio.id}", RadioSet), first_radio)
                self.assertTrue(first_radio.query(RadioButton).first(RadioButton).value)

                await pilot.press("f2")
                await pilot.pause()
                self.assertTrue(passage_pane.display)
                self.assertFalse(question_pane.display)
                await pilot.press("f2")
                await pilot.pause()

                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                self.assertFalse(screen.has_class("narrow-workspace"))
                self.assertFalse(screen.check_action("toggle_pane", ()))
                self.assertTrue(passage_pane.display)
                self.assertTrue(question_pane.display)
                self.assertTrue(first_radio.query(RadioButton).first(RadioButton).value)

                await pilot.resize_terminal(119, 40)
                await pilot.pause()
                self.assertTrue(screen.has_class("narrow-workspace"))
                self.assertTrue(screen.check_action("toggle_pane", ()))
                self.assertFalse(passage_pane.display)
                self.assertTrue(question_pane.display)

                await pilot.resize_terminal(120, 40)
                await pilot.pause()
                self.assertFalse(screen.has_class("narrow-workspace"))
                self.assertTrue(passage_pane.display)
                self.assertTrue(question_pane.display)
                self.assertEqual(passage_pane.region.y, question_pane.region.y)

    async def test_library_and_complete_answer_form_mount(self) -> None:
        bank = load_bank()
        exam = next(exam for exam in bank["exams"] if exam["exam_id"] == "p1-low-02")
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "history.jsonl")
            self.assertEqual(app.theme, "ubuntu-gnome")
            self.assertIs(app.get_theme("ubuntu-gnome"), UBUNTU_GNOME_THEME)
            self.assertEqual(app.current_theme.background, "#300A24")
            self.assertEqual(app.current_theme.primary, "#E95420")
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, LibraryScreen)
                app.start_exam(exam["exam_id"])
                await pilot.pause()
                self.assertIsInstance(app.screen, PracticeScreen)
                self.assertEqual(len(app.screen.query(".answer-control")), len(exam["questions"]))
                self.assertGreater(len(app.screen.query(RadioSet)), 0)
                self.assertEqual(len(app.screen.query("#next")), 0)
                first_radio = app.screen.query(RadioSet).first(RadioSet)
                first_radio.query(RadioButton).first(RadioButton).value = True
                await pilot.pause()
                self.assertIn("01", str(app.screen.query_one("#progress", Static).render()))
                app.screen.save_answers()
                radio_index = int(first_radio.id.removeprefix("answer-"))
                radio_question = exam["questions"][radio_index]
                self.assertEqual(app.screen.answers[radio_question["id"]], radio_question["options"][0])

                app.screen.action_library()
                await pilot.pause()
                heading_exam = next(item for item in bank["exams"] if item["exam_id"] == "p1-high-118")
                app.start_exam(heading_exam["exam_id"])
                await pilot.pause()
                self.assertEqual(
                    len(app.screen.query(".answer-control")),
                    len(heading_exam["questions"]),
                )
                self.assertGreater(len(app.screen.query(".answer-heading")), 0)
                self.assertGreater(len(app.screen.query(Select)), 0)
                self.assertGreater(len(app.screen.query(RadioSet)), 0)
                heading_select = app.screen.query(".answer-heading").first(Select)
                heading_index = int(heading_select.id.removeprefix("answer-"))
                heading_question = heading_exam["questions"][heading_index]
                heading_value = heading_select_options(heading_question)[0][1]
                heading_select.value = heading_value
                app.screen.save_answers()
                self.assertEqual(app.screen.answers[heading_question["id"]], heading_value)

    async def test_progress_resumes_timer_and_submit_marks_library(self) -> None:
        bank = load_bank()
        exam = next(item for item in bank["exams"] if item["exam_id"] == "p1-low-02")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            app = IELTSApp(bank, path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.start_exam(exam["exam_id"])
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, PracticeScreen)
                screen.elapsed_before_resume = 65
                screen.started_monotonic = time.monotonic()
                first_radio = screen.query(RadioSet).first(RadioSet)
                radio_index = int(first_radio.id.removeprefix("answer-"))
                radio_question = exam["questions"][radio_index]
                first_radio.query(RadioButton).first(RadioButton).value = True
                await pilot.pause()
                screen.persist_progress()
                screen.refresh_practice_status()
                self.assertIn("01:05", str(screen.query_one("#progress", Static).render()))
                draft_store = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    draft_store["progress"][exam["exam_id"]]["answers"][radio_question["id"]],
                    radio_question["options"][0],
                )
                self.assertGreaterEqual(
                    draft_store["progress"][exam["exam_id"]]["elapsed_seconds"],
                    65,
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                screen.action_library()
                await pilot.pause()

            resumed_app = IELTSApp(bank, path)
            async with resumed_app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                resumed_app.start_exam(exam["exam_id"])
                await pilot.pause()
                resumed = resumed_app.screen
                self.assertIsInstance(resumed, PracticeScreen)
                restored_radio = resumed.query(RadioSet).first(RadioSet)
                self.assertTrue(restored_radio.query(RadioButton).first(RadioButton).value)
                self.assertGreaterEqual(resumed.elapsed_seconds(), 65)
                resumed.action_submit_exam()
                await pilot.pause()
                self.assertIsInstance(resumed_app.screen, ResultScreen)
                self.assertTrue(resumed._submitted)
                self.assertIsNone(resumed._clock_timer)
                submitted_store = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn(exam["exam_id"], submitted_store["progress"])
                self.assertEqual(submitted_store["attempts"][-1]["exam_id"], exam["exam_id"])
                self.assertGreaterEqual(submitted_store["attempts"][-1]["duration_seconds"], 65)

                resumed_app.pop_screen()
                await pilot.pause()
                resumed.action_library()
                await pilot.pause()
                self.assertIsInstance(resumed_app.screen, LibraryScreen)
                row = resumed_app.screen.query_one("#exam-table").get_row(exam["exam_id"])
                self.assertEqual(str(row[0]), "✓")

    async def test_take_notes_shortcut_persists_and_survives_submit(self) -> None:
        bank = load_bank()
        exam = next(item for item in bank["exams"] if item["exam_id"] == "p1-high-211")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            app = IELTSApp(bank, path)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                library_row = app.screen.query_one("#exam-table").get_row(exam["exam_id"])
                self.assertEqual(str(library_row[3]), "Ahead of its time")
                app.start_exam(exam["exam_id"])
                await pilot.pause()
                practice = app.screen
                self.assertIsInstance(practice, PracticeScreen)
                self.assertEqual(
                    str(practice.query_one("#exam-title", Static).render()),
                    "Ahead of its time",
                )
                answer_input = practice.query_one("#answer-4", Input)
                answer_input.focus()
                await pilot.press("ctrl+n")
                await pilot.pause()
                self.assertIsInstance(app.screen, NotesScreen)
                editor = app.screen.query_one("#notes-editor", TextArea)
                self.assertTrue(editor.has_focus)
                editor.load_text("Key vocabulary\n第二行笔记")
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertIs(app.screen, practice)
                self.assertFalse(practice._submitted)
                self.assertEqual(app.note_for_exam(exam["exam_id"]), "Key vocabulary\n第二行笔记")

                await pilot.press("ctrl+n")
                await pilot.pause()
                self.assertEqual(
                    app.screen.query_one("#notes-editor", TextArea).text,
                    "Key vocabulary\n第二行笔记",
                )
                app.screen.query_one("#notes-editor", TextArea).load_text("discard this")
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.note_for_exam(exam["exam_id"]), "Key vocabulary\n第二行笔记")

                practice.action_submit_exam()
                await pilot.pause()
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(stored["schema_version"], 4)
                self.assertEqual(
                    stored["notes"][exam["exam_id"]]["text"],
                    "Key vocabulary\n第二行笔记",
                )
                self.assertNotIn(exam["exam_id"], stored["progress"])

    async def test_mixed_choices_and_inline_editors_mount_with_correct_widgets(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "history.jsonl")
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                app.start_exam("p3-medium-162")
                await pilot.pause()
                first_radio = app.screen.query_one("#answer-0", RadioSet)
                first_label = first_radio.query(RadioButton).first(RadioButton).label.plain
                self.assertIn("universality", first_label)
                self.assertNotIn("correct", first_label)
                self.assertEqual(len(app.screen.query(InlineSelectEditor)), 1)
                self.assertIsInstance(app.screen.query_one("#answer-10"), RadioSet)

                app.screen.action_library()
                await pilot.pause()
                app.start_exam("p3-low-187")
                await pilot.pause()
                editor = app.screen.query(CompletionEditor).first(CompletionEditor)
                self.assertIn("q7:0", editor.field_ids)
                self.assertIn("q7:1", editor.field_ids)
                self.assertEqual(editor.question_answers["q7"], "")
                editor.answers["q7:0"] = "breathing"
                editor.answers["q7:1"] = "eating"
                app.screen.save_answers()
                question = next(question for question in app.screen.exam["questions"] if question["id"] == "q7")
                self.assertEqual(app.screen.answers["q7"], "breathing / eating")
                self.assertTrue(question_is_correct(question, app.screen.answers["q7"]))
                app.screen.persist_progress()
                app.screen.action_library()
                await pilot.pause()
                app.start_exam("p3-low-187")
                await pilot.pause()
                restored_editor = app.screen.query(CompletionEditor).first(CompletionEditor)
                self.assertEqual(restored_editor.answers["q7:0"], "breathing")
                self.assertEqual(restored_editor.answers["q7:1"], "eating")

    async def test_reclassified_notes_render_once_without_duplicate_question_numbers(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "state.json")
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                app.start_exam("p1-high-194")
                await pilot.pause()
                editor = app.screen.query(CompletionEditor).first(CompletionEditor)
                rendered = editor.render_template().plain
                self.assertIn("Woollen cloth manufacture", rendered)
                self.assertNotIn("6  6:", rendered)
                self.assertEqual(len(app.screen.query(CompletionEditor)), 1)

                app.screen.action_library()
                await pilot.pause()
                app.start_exam("p1-high-227")
                await pilot.pause()
                unnumbered_source = app.screen.query(CompletionEditor).first(CompletionEditor)
                self.assertIn("8: ________", unnumbered_source.render_template().plain)

    async def test_long_radio_labels_wrap_in_narrow_terminal(self) -> None:
        bank = load_bank()
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "state.json")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.pause()
                app.start_exam("p3-medium-22")
                await pilot.pause()
                radio_set = app.screen.query_one("#answer-13", RadioSet)
                first_option = radio_set.query(RadioButton).first(RadioButton)
                self.assertIn("bigger than originally thought", first_option.label.plain)
                self.assertGreater(first_option.size.height, 1)

    async def test_saved_progress_hydrates_input_select_radio_and_checkbox(self) -> None:
        bank = load_bank()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        drafts = {
            "p1-high-105": {
                "exam_id": "p1-high-105",
                "started_at": now,
                "elapsed_seconds": 7,
                "answers": {"q1": "TRUE"},
            },
            "p1-high-211": {
                "exam_id": "p1-high-211",
                "started_at": now,
                "elapsed_seconds": 7,
                "answers": {"q5": "restored words"},
            },
            "p1-high-01": {
                "exam_id": "p1-high-01",
                "started_at": now,
                "elapsed_seconds": 8,
                "answers": {"q1": "i"},
            },
            "p1-medium-57": {
                "exam_id": "p1-medium-57",
                "started_at": now,
                "elapsed_seconds": 9,
                "answers": {"q11": "C / E / G"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_practice_data(path, {"attempts": [], "progress": drafts})
            app = IELTSApp(bank, path)
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                app.start_exam("p1-high-105")
                await pilot.pause()
                self.assertTrue(
                    app.screen.query_one("#answer-0", RadioSet)
                    .query(RadioButton)
                    .first(RadioButton)
                    .value
                )

                app.screen.action_library()
                await pilot.pause()
                app.start_exam("p1-high-211")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#answer-4", Input).value, "restored words")

                app.screen.action_library()
                await pilot.pause()
                app.start_exam("p1-high-01")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#answer-0", Select).value, "i")

                app.screen.action_library()
                await pilot.pause()
                app.start_exam("p1-medium-57")
                await pilot.pause()
                selected = [
                    checkbox.value
                    for checkbox in app.screen.query_one("#answer-10").query(Checkbox)
                ]
                self.assertEqual(
                    [index for index, value in enumerate(selected) if value],
                    [2, 4, 6],
                )


class HistoryTests(unittest.TestCase):
    def test_timed_attempt_schema_and_backward_compatible_aggregation(self) -> None:
        bank = load_bank()
        exam = next(exam for exam in bank["exams"] if exam["exam_id"] == "p1-low-02")
        answers = {question["id"]: question["answer"] for question in exam["questions"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            app = IELTSApp(bank, path)
            app.record_attempt(
                exam,
                answers,
                datetime.now().astimezone(),
                125,
            )
            store = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(store["schema_version"], 4)
            saved = store["attempts"][0]
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["duration_seconds"], 125)
            self.assertEqual(saved["accuracy"], 1.0)
            self.assertEqual(saved["attempted"], len(exam["questions"]))
            self.assertEqual(len(saved["question_results"]), len(exam["questions"]))
            self.assertNotIn(exam["exam_id"], store["progress"])

            legacy_path = Path(directory) / "legacy.jsonl"
            legacy_path.write_text(
                "not-json\n"
                + json.dumps({"correct": 1, "total": 2, "answers": {"q1": "x"}})
                + "\n",
                encoding="utf-8",
            )
            records = [*read_history(path), *read_history(legacy_path)]
            stats = aggregate_history(records)
            self.assertEqual(len(records), 2)
            self.assertEqual(stats["timed_attempt_count"], 1)
            self.assertEqual(stats["total_duration_seconds"], 125)
            self.assertEqual(
                stats["accuracy"],
                (len(exam["questions"]) + 1) / (len(exam["questions"]) + 2),
            )

    def test_default_store_is_in_home_directory(self) -> None:
        self.assertEqual(DEFAULT_DATA_PATH, Path.home() / ".terminal_ielts.json")


if __name__ == "__main__":
    unittest.main()
