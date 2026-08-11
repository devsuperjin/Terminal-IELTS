from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from textual.widgets import Input, RadioButton, RadioSet, Select, Static

from terminal_ielts.app import (
    CompletionEditor,
    IELTSApp,
    InlineSelectEditor,
    LibraryScreen,
    PracticeScreen,
    heading_select_options,
    question_is_correct,
)
from terminal_ielts.bank import answer_is_correct, load_bank
from terminal_ielts.extractor import normalise_exam, parse_registry_file
from terminal_ielts.history import aggregate_history, read_history


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
        self.assertEqual(len(sentence_groups), 68)
        self.assertEqual(sum(group.get("response_mode") == "inline_text" for group in sentence_groups), 62)
        self.assertEqual(sum(group.get("subtype") == "ending_select" for group in sentence_groups), 6)
        self.assertEqual(
            sum(len(group.get("completion_slots", [])) for group in sentence_groups),
            347,
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


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_and_complete_answer_form_mount(self) -> None:
        bank = load_bank()
        exam = next(exam for exam in bank["exams"] if exam["exam_id"] == "p1-low-02")
        with tempfile.TemporaryDirectory() as directory:
            app = IELTSApp(bank, Path(directory) / "history.jsonl")
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
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["duration_seconds"], 125)
            self.assertEqual(saved["accuracy"], 1.0)
            self.assertEqual(saved["attempted"], len(exam["questions"]))

            with path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")
                handle.write(json.dumps({"correct": 1, "total": 2, "answers": {"q1": "x"}}) + "\n")
            records = read_history(path)
            stats = aggregate_history(records)
            self.assertEqual(len(records), 2)
            self.assertEqual(stats["timed_attempt_count"], 1)
            self.assertEqual(stats["total_duration_seconds"], 125)
            self.assertEqual(
                stats["accuracy"],
                (len(exam["questions"]) + 1) / (len(exam["questions"]) + 2),
            )


if __name__ == "__main__":
    unittest.main()
