from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from terminal_ielts.cli import build_parser, print_dictionary_entry
from terminal_ielts.dictionary import (
    E2CDictionary,
    default_dictionary_path,
    normalise_word,
)


class DictionaryTests(unittest.TestCase):
    def write_dictionary(self, directory: str, contents: str) -> Path:
        path = Path(directory) / "E2Cdictionary.js"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_normalise_word_accepts_one_punctuated_token_only(self) -> None:
        self.assertEqual(normalise_word("  $Abacus  "), "abacus")
        self.assertEqual(normalise_word("“Fish-hooks,”"), "fish-hooks")
        self.assertEqual(normalise_word("can't"), "can't")
        self.assertEqual(normalise_word("two words"), "")
        self.assertEqual(normalise_word("word/phrase"), "")
        self.assertEqual(normalise_word("中文"), "")

    def test_lookup_loads_whole_file_in_memory_and_suggests_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_dictionary(
                directory,
                """wordMeaning={
$apple:"苹果",
$application:"应用",
$apply:"申请",
$repeat:"旧释义",
$repeat:"新释义",
};
""",
            )
            dictionary = E2CDictionary(path)
            self.assertTrue(dictionary.loaded)
            self.assertEqual(dictionary.entry_count, 4)

            invalid = dictionary.lookup("two words")
            self.assertEqual(invalid.word, "")

            found = dictionary.lookup("Apple,")
            self.assertTrue(found.found)
            self.assertEqual(found.word, "apple")
            self.assertEqual(found.meaning, "苹果")
            self.assertTrue(dictionary.loaded)
            self.assertEqual(dictionary.lookup("repeat").meaning, "新释义")

            missing = dictionary.lookup("app", suggestion_limit=2)
            self.assertFalse(missing.found)
            self.assertEqual(missing.suggestions, ("apple", "application"))

    def test_cli_parses_data_path_and_prints_hit_or_miss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_dictionary(
                directory,
                '$apple:"苹果",\n$application:"应用",\n',
            )
            args = build_parser().parse_args(
                ["dictionary", "apple", "--data", str(path)]
            )
            self.assertEqual(args.command, "dictionary")
            self.assertEqual(args.word, "apple")
            self.assertEqual(args.data, path)

            output = io.StringIO()
            with redirect_stdout(output):
                status = print_dictionary_entry("Apple,", path)
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue(), "apple: 苹果\n")

            error = io.StringIO()
            with redirect_stderr(error):
                status = print_dictionary_entry("app", path)
            self.assertEqual(status, 1)
            self.assertIn('No dictionary entry found for "app".', error.getvalue())
            self.assertIn("Suggestions: apple, application", error.getvalue())

    def test_environment_can_override_dictionary_path(self) -> None:
        with patch.dict(
            "terminal_ielts.dictionary.os.environ",
            {"TERMINAL_IELTS_DICTIONARY": "~/custom-dictionary.js"},
            clear=True,
        ):
            self.assertEqual(
                default_dictionary_path(),
                Path("~/custom-dictionary.js").expanduser(),
            )

    def test_bundled_dictionary_is_fully_loaded_in_memory(self) -> None:
        dictionary = E2CDictionary()
        self.assertTrue(dictionary.loaded)
        self.assertEqual(dictionary.entry_count, 103_812)
        self.assertEqual(dictionary.lookup("dictionary").meaning, "n.字典 词典")


if __name__ == "__main__":
    unittest.main()
