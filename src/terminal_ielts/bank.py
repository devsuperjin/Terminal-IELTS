"""Question-bank loading and answer scoring."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any


def bundled_bank_path() -> Path:
    return Path(str(files("terminal_ielts").joinpath("data/questions.json")))


def load_bank(path: Path | None = None) -> dict[str, Any]:
    target = path or bundled_bank_path()
    if not target.exists():
        raise FileNotFoundError(
            f"Question bank not found at {target}. Run `terminal-ielts extract` first."
        )
    return json.loads(target.read_text(encoding="utf-8"))


def normalise_answer(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def answer_is_correct(user_answer: str, expected: str, *, positional: bool = False) -> bool:
    raw_parts = [item.strip() for item in re.split(r"\s*/\s*", expected)]
    if positional:
        user_parts = [item.strip() for item in re.split(r"\s*/\s*", user_answer)]
        return len(user_parts) == len(raw_parts) and all(
            normalise_answer(user) == normalise_answer(answer)
            for user, answer in zip(user_parts, raw_parts, strict=True)
        )
    if len(raw_parts) > 1 and all(re.fullmatch(r"[A-Za-z]", item) for item in raw_parts):
        user_parts = {
            normalise_answer(item)
            for item in re.split(r"[\s,;/]+", user_answer)
            if item.strip()
        }
        return user_parts == {normalise_answer(item) for item in raw_parts}
    accepted = [normalise_answer(item) for item in raw_parts]
    return normalise_answer(user_answer) in accepted
