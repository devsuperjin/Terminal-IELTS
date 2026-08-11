"""Backward-compatible practice history loading and aggregate statistics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _count(value: Any, *, maximum: int | None = None) -> int:
    try:
        parsed = max(0, int(value))
    except (TypeError, ValueError):
        parsed = 0
    return min(parsed, maximum) if maximum is not None else parsed


def normalise_history_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalise both legacy rows and schema-v2 timed attempts."""
    answers = record.get("answers") if isinstance(record.get("answers"), dict) else {}
    total = _count(record.get("total"))
    correct = _count(record.get("correct"), maximum=total)
    attempted_fallback = sum(bool(str(value).strip()) for value in answers.values())
    attempted = _count(record.get("attempted", attempted_fallback), maximum=total)
    duration_raw = record.get("duration_seconds")
    duration = None if duration_raw is None else _count(duration_raw)
    return {
        "schema_version": _count(record.get("schema_version", 1)),
        "exam_id": str(record.get("exam_id", "")),
        "title": str(record.get("title", "")),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "duration_seconds": duration,
        "attempted": attempted,
        "correct": correct,
        "total": total,
        "accuracy": correct / total if total else 0.0,
        "answers": dict(answers),
    }


def read_history(path: Path) -> list[dict[str, Any]]:
    """Read JSONL history, ignoring malformed or non-object rows."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(normalise_history_record(value))
    return records


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
