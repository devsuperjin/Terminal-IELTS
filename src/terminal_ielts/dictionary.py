"""In-memory lookup for the bundled English-to-Chinese dictionary."""

from __future__ import annotations

import bisect
import os
import re
import sysconfig
from dataclasses import dataclass
from pathlib import Path


DICTIONARY_FILENAME = "E2Cdictionary.js"
ENTRY_RE = re.compile(r'^\s*\$([^:\r\n]+):"(.*)",?\s*$', re.MULTILINE)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'-]*")


def default_dictionary_path() -> Path:
    """Locate the dictionary in a checkout or an installed wheel."""
    if configured := os.environ.get("TERMINAL_IELTS_DICTIONARY"):
        return Path(configured).expanduser()

    checkout_path = Path(__file__).resolve().parents[2] / "data" / DICTIONARY_FILENAME
    if checkout_path.is_file():
        return checkout_path

    installed_path = Path(sysconfig.get_path("data")) / DICTIONARY_FILENAME
    if installed_path.is_file():
        return installed_path

    return checkout_path


def normalise_word(value: str) -> str:
    """Normalise one lookup token without attempting unreliable stemming."""
    stripped = str(value).strip().removeprefix("$")
    match = WORD_RE.search(stripped)
    if match is None or stripped[: match.start()].strip(".,;:!?()[]{}\"“”‘’ "):
        return ""
    trailing = stripped[match.end() :]
    if trailing.strip(".,;:!?()[]{}\"“”‘’ "):
        return ""
    return match.group(0).casefold()


@dataclass(frozen=True)
class DictionaryResult:
    """Result of one dictionary lookup."""

    query: str
    word: str
    meaning: str | None
    suggestions: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.meaning is not None


class E2CDictionary:
    """An in-memory index over the local JavaScript-style word map."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_dictionary_path()
        source = self.path.read_text(encoding="utf-8")
        self._entries = {
            word.casefold(): meaning
            for word, meaning in ENTRY_RE.findall(source)
        }
        self._sorted_words = sorted(self._entries)

    @property
    def loaded(self) -> bool:
        return True

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def lookup(self, query: str, *, suggestion_limit: int = 6) -> DictionaryResult:
        """Look up one word and offer prefix suggestions for a miss."""
        word = normalise_word(query)
        if not word:
            return DictionaryResult(query=str(query), word="", meaning=None)

        meaning = self._entries.get(word)
        if meaning is not None:
            return DictionaryResult(query=str(query), word=word, meaning=meaning)

        start = bisect.bisect_left(self._sorted_words, word)
        suggestions: list[str] = []
        for candidate in self._sorted_words[start:]:
            if not candidate.startswith(word):
                break
            suggestions.append(candidate)
            if len(suggestions) >= suggestion_limit:
                break
        return DictionaryResult(
            query=str(query),
            word=word,
            meaning=None,
            suggestions=tuple(suggestions),
        )
