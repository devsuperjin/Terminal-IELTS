"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .app import IELTSApp
from .bank import bundled_bank_path, load_bank
from .dictionary import E2CDictionary
from .extractor import extract_question_bank, write_question_bank
from .history import DEFAULT_DATA_PATH, aggregate_history, read_history


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = PROJECT_ROOT / "source" / "IELTS-practice"


def source_commit(source: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        provenance = source.parent / "IELTS-practice.commit"
        if provenance.exists():
            return provenance.read_text(encoding="utf-8").strip()
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Terminal-style IELTS reading practice")
    parser.add_argument("--bank", type=Path, help="use a custom extracted JSON bank")
    subparsers = parser.add_subparsers(dest="command")
    extract = subparsers.add_parser("extract", help="rebuild the JSON bank from the downloaded source")
    extract.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    extract.add_argument("--output", type=Path, default=bundled_bank_path())
    subparsers.add_parser("stats", help="print extracted bank statistics")
    history_stats = subparsers.add_parser("history-stats", help="print practice timing and accuracy statistics")
    history_stats.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_DATA_PATH,
        help="practice store or legacy JSONL path (default: ~/.terminal_ielts.json)",
    )
    dictionary = subparsers.add_parser(
        "dictionary",
        help="look up an English word in the local English-Chinese dictionary",
    )
    dictionary.add_argument("word", help="English word to look up")
    dictionary.add_argument(
        "--data",
        type=Path,
        help=(
            "use a custom E2Cdictionary.js file "
            "(default: bundled data or TERMINAL_IELTS_DICTIONARY)"
        ),
    )
    return parser


def print_dictionary_entry(word: str, data_path: Path | None = None) -> int:
    """Print one local dictionary result and return a command exit status."""
    requested_path = data_path.expanduser() if data_path else None
    try:
        dictionary = E2CDictionary(requested_path)
        result = dictionary.lookup(word)
    except FileNotFoundError:
        display_path = requested_path or "the bundled data path"
        print(f"Dictionary file not found: {display_path}", file=sys.stderr)
        print(
            "Use --data PATH or set TERMINAL_IELTS_DICTIONARY to its location.",
            file=sys.stderr,
        )
        return 2
    except UnicodeError as error:
        print(
            f"Could not read dictionary as UTF-8 ({requested_path or 'bundled data'}): {error}",
            file=sys.stderr,
        )
        return 2
    except OSError as error:
        print(
            f"Could not read dictionary ({requested_path or 'bundled data'}): {error}",
            file=sys.stderr,
        )
        return 2

    if result.found:
        print(f"{result.word}: {result.meaning}")
        return 0

    display_word = result.word or word.strip()
    print(f'No dictionary entry found for "{display_word}".', file=sys.stderr)
    if result.suggestions:
        print(f"Suggestions: {', '.join(result.suggestions)}", file=sys.stderr)
    else:
        print("No matching suggestions were found.", file=sys.stderr)
    return 1


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "extract":
        bank = extract_question_bank(args.source.resolve(), source_commit(args.source.resolve()))
        write_question_bank(bank, args.output.resolve())
        print(
            f"Extracted {bank['stats']['exam_count']} exams and "
            f"{bank['stats']['question_count']} questions to {args.output.resolve()}"
        )
        return

    if args.command == "history-stats":
        print(json.dumps(aggregate_history(read_history(args.history)), indent=2, ensure_ascii=False))
        return

    if args.command == "dictionary":
        status = print_dictionary_entry(args.word, args.data)
        if status:
            raise SystemExit(status)
        return

    bank = load_bank(args.bank)
    if args.command == "stats":
        print(json.dumps(bank["stats"], indent=2, ensure_ascii=False))
        return
    IELTSApp(bank).run()
