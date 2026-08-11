"""Persistent practice data, legacy history loading, and aggregate statistics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


STORE_SCHEMA_VERSION = 4
DEFAULT_DATA_PATH = Path.home() / ".terminal_ielts.json"


def _count(value: Any, *, maximum: int | None = None) -> int:
    try:
        parsed = max(0, int(value))
    except (TypeError, ValueError):
        parsed = 0
    return min(parsed, maximum) if maximum is not None else parsed


def empty_practice_data() -> dict[str, Any]:
    """Return a new empty single-file practice store."""
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "attempts": [],
        "progress": {},
        "notes": {},
    }


def _normalise_progress(progress: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(progress, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in progress.items():
        if not isinstance(value, dict):
            continue
        exam_id = str(value.get("exam_id") or key)
        if not exam_id:
            continue
        answers = value.get("answers") if isinstance(value.get("answers"), dict) else {}
        slot_values = value.get("slot_values") if isinstance(value.get("slot_values"), dict) else {}
        cleaned[exam_id] = {
            "schema_version": _count(value.get("schema_version", 1)),
            "exam_id": exam_id,
            "started_at": value.get("started_at"),
            "updated_at": value.get("updated_at"),
            "elapsed_seconds": _count(value.get("elapsed_seconds")),
            "answers": {str(question_id): str(answer) for question_id, answer in answers.items()},
            "slot_values": {str(slot_id): str(answer) for slot_id, answer in slot_values.items()},
            "bank_commit": str(value.get("bank_commit", "")),
        }
    return cleaned


def _normalise_notes(notes: Any) -> dict[str, dict[str, Any]]:
    """Normalise passage notes while accepting the early string shorthand."""
    if not isinstance(notes, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in notes.items():
        exam_id = str(key)
        if not exam_id:
            continue
        if isinstance(value, str):
            text = value
            updated_at = None
            bank_commit = ""
        elif isinstance(value, dict):
            text = str(value.get("text", ""))
            updated_at = value.get("updated_at")
            bank_commit = str(value.get("bank_commit", ""))
        else:
            continue
        if not text:
            continue
        cleaned[exam_id] = {
            "schema_version": 1,
            "text": text,
            "updated_at": updated_at,
            "bank_commit": bank_commit,
        }
    return cleaned


def load_practice_data(path: Path) -> dict[str, Any]:
    """Load the current store, a legacy JSON object/list, or legacy JSONL rows."""
    if not path.exists():
        return empty_practice_data()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return empty_practice_data()
    if not text.strip():
        return empty_practice_data()

    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        value = None

    attempts: list[dict[str, Any]] = []
    progress: dict[str, dict[str, Any]] = {}
    notes: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict) and (
        "attempts" in value or "progress" in value or "notes" in value
    ):
        raw_attempts = value.get("attempts", [])
        if isinstance(raw_attempts, list):
            attempts = [dict(record) for record in raw_attempts if isinstance(record, dict)]
        progress = _normalise_progress(value.get("progress"))
        notes = _normalise_notes(value.get("notes"))
    elif isinstance(value, dict):
        attempts = [dict(value)]
    elif isinstance(value, list):
        attempts = [dict(record) for record in value if isinstance(record, dict)]
    else:
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                attempts.append(dict(record))

    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "attempts": attempts,
        "progress": progress,
        "notes": notes,
    }


def write_practice_data(path: Path, data: dict[str, Any]) -> None:
    """Atomically write the single-file store with user-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": STORE_SCHEMA_VERSION,
        "attempts": [
            dict(record) for record in data.get("attempts", []) if isinstance(record, dict)
        ],
        "progress": _normalise_progress(data.get("progress")),
        "notes": _normalise_notes(data.get("notes")),
    }
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def normalise_history_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalise legacy rows and timed attempt records."""
    answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
    total = _count(record.get("total"))
    correct = _count(record.get("correct"), maximum=total)
    attempted_fallback = sum(bool(str(value).strip()) for value in answers.values())
    attempted = _count(record.get("attempted", attempted_fallback), maximum=total)
    duration_raw = record.get("duration_seconds")
    duration = None if duration_raw is None else _count(duration_raw)
    question_results = (
        [dict(result) for result in record["question_results"] if isinstance(result, dict)]
        if isinstance(record.get("question_results"), list)
        else []
    )
    return {
        "schema_version": _count(record.get("schema_version", 1)),
        "exam_id": str(record.get("exam_id", "")),
        "title": str(record.get("title", "")),
        "category": str(record.get("category", "")),
        "frequency": str(record.get("frequency", "")),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "duration_seconds": duration,
        "attempted": attempted,
        "correct": correct,
        "total": total,
        "accuracy": correct / total if total else 0.0,
        "answers": {str(question_id): str(answer) for question_id, answer in answers.items()},
        "question_results": question_results,
    }


def read_history(path: Path) -> list[dict[str, Any]]:
    """Read attempts from the current store or any supported legacy format."""
    data = load_practice_data(path)
    return [normalise_history_record(record) for record in data["attempts"]]


def aggregate_history(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate using weighted accuracy and only known timing values."""
    correct = sum(record["correct"] for record in records)
    total = sum(record["total"] for record in records)
    attempted = sum(record["attempted"] for record in records)
    durations = [
        record["duration_seconds"]
        for record in records
        if record.get("duration_seconds") is not None
    ]
    return {
        "attempt_count": len(records),
        "attempted_questions": attempted,
        "correct_questions": correct,
        "total_questions": total,
        "accuracy": correct / total if total else 0.0,
        "completion_rate": attempted / total if total else 0.0,
        "timed_attempt_count": len(durations),
        "total_duration_seconds": sum(durations),
        "average_duration_seconds": round(sum(durations) / len(durations)) if durations else None,
    }
