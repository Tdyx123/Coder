"""Stable result contracts for generated-plan execution.

This module deliberately has no dependency on the batch CLI.  The child
runtime reports what it observed; the parent supplies process authority and
run identity before a result becomes part of an experiment summary.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union


_TERMINAL_STATUSES = frozenset(("succeeded", "failed", "skipped", "cancelled"))
_TASK_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def task_key_for_executable(executable_path: Path) -> str:
    """Return the stable, path-safe identity for a generated executable."""

    normalized_path = str(Path(executable_path).expanduser().resolve())
    return sha256(normalized_path.encode("utf-8")).hexdigest()


class ActionLedger:
    """Count logical plan actions independently from their attempts."""

    def __init__(self, planned_keys: Sequence[str] = ()) -> None:
        self._planned = {str(key) for key in planned_keys}
        self._started = set()
        self._terminal: Dict[str, Dict[str, Any]] = {}
        self._attempts = 0
        self._frozen = False
        self._lock = threading.Lock()

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("ActionLedger is frozen")

    def record_started(self, key: str) -> None:
        with self._lock:
            self._ensure_mutable()
            normalized = str(key)
            self._planned.add(normalized)
            self._started.add(normalized)

    def record_attempt(self) -> None:
        with self._lock:
            self._ensure_mutable()
            self._attempts += 1

    def record_terminal(
        self,
        key: str,
        status: str,
        *,
        ignored_for_legacy: bool = False,
    ) -> None:
        with self._lock:
            self._ensure_mutable()
            normalized = str(key)
            if status not in _TERMINAL_STATUSES:
                raise ValueError(f"unsupported action terminal status: {status!r}")
            if normalized in self._terminal:
                raise RuntimeError(f"action already has a terminal result: {normalized}")
            self._planned.add(normalized)
            self._started.add(normalized)
            self._terminal[normalized] = {
                "status": status,
                "ignored_for_legacy": bool(ignored_for_legacy),
            }

    def freeze(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_mutable()
            self._frozen = True
            counts = {status: 0 for status in _TERMINAL_STATUSES}
            ignored_failure_count = 0
            for terminal in self._terminal.values():
                status = terminal["status"]
                counts[status] += 1
                if status == "failed" and terminal["ignored_for_legacy"]:
                    ignored_failure_count += 1
            failed = counts["failed"]
            succeeded = counts["succeeded"]
            denominator = succeeded + failed
            return {
                "action_counts": {
                    "planned": len(self._planned),
                    "started": len(self._started),
                    "succeeded": succeeded,
                    "failed": failed,
                    "skipped": counts["skipped"],
                    "cancelled": counts["cancelled"],
                    "unexecuted": len(self._planned) - len(self._terminal),
                    "attempts": self._attempts,
                },
                "raw_action_sr": succeeded / denominator if denominator else None,
                "ignored_failure_count": ignored_failure_count,
            }


def normalize_output(value: Union[str, bytes, None]) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as handle:
        handle.write(payload)
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def _expected_identity() -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if os.environ.get("LAMMAP_RUN_ID"):
        result["run_id"] = os.environ["LAMMAP_RUN_ID"]
    if os.environ.get("LAMMAP_TASK_KEY"):
        result["task_key"] = os.environ["LAMMAP_TASK_KEY"]
    if os.environ.get("LAMMAP_ATTEMPT"):
        try:
            result["attempt"] = int(os.environ["LAMMAP_ATTEMPT"])
        except ValueError as exc:
            raise ValueError("LAMMAP_ATTEMPT must be an integer") from exc
    return result


def validate_result(
    value: Any,
    *,
    returncode: int,
    expected_identity: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate child data and apply the parent process outcome.

    A v2 result has to prove its identity.  A pre-v2 result remains explicitly
    legacy, but can be labelled with the parent identity for bookkeeping.
    """

    if not isinstance(value, Mapping):
        raise ValueError("runner metrics must be a JSON object")
    result = dict(value)
    expected_identity = (
        dict(expected_identity)
        if expected_identity is not None
        else _expected_identity()
    )
    is_v2 = result.get("metrics_schema_version") == 2
    if is_v2:
        if result.get("evaluation_version") != "fixed_goals_v2":
            raise ValueError("v2 result must use evaluation_version=fixed_goals_v2")
        if result.get("execution_policy") != "legacy":
            raise ValueError("v2 result must use execution_policy=legacy")
        for key, expected in expected_identity.items():
            if result.get(key) != expected:
                raise ValueError(f"v2 result {key} does not match parent identity")
    else:
        result.setdefault("evaluation_version", "legacy_v1")
        for key, expected in expected_identity.items():
            result.setdefault(key, expected)

    if returncode == 0:
        result["process_status"] = "completed"
        result["status"] = "success"
        result["timed_out"] = False
    elif returncode == 124:
        result["process_status"] = "timeout"
        result["execution_status"] = "timeout"
        result["evaluation_status"] = "incomplete"
        result["task_success"] = None
        result["gcr"] = None
        result["tc"] = None
        result["sr"] = None
        result["ru"] = None
        result["status"] = "timeout"
        result["timed_out"] = True
    else:
        result["process_status"] = "failed"
        result["execution_status"] = "failed"
        result["evaluation_status"] = "incomplete"
        result["task_success"] = None
        result["gcr"] = None
        result["tc"] = None
        result["sr"] = None
        result["ru"] = None
        result["status"] = "failed"
        result["timed_out"] = False
    return result


class RunResultStore:
    """File-backed attempt collection with conservative v2 grouped summaries."""

    def __init__(self, output_dir: Path, run_id: str) -> None:
        self.output_dir = Path(output_dir)
        self.run_id = str(run_id)
        self.attempts_dir = self.output_dir / "attempts"

    def record_attempt(
        self,
        task_key: str,
        attempt: int,
        result: Mapping[str, Any],
    ) -> Path:
        checked = dict(result)
        if checked.get("run_id") != self.run_id:
            raise ValueError("attempt run_id does not match store run_id")
        if checked.get("task_key") != str(task_key):
            raise ValueError("attempt task_key does not match record target")
        if checked.get("attempt") != int(attempt):
            raise ValueError("attempt number does not match record target")
        if not _TASK_KEY_PATTERN.fullmatch(str(task_key)):
            raise ValueError("task_key must be a SHA-256 executable path digest")
        if int(attempt) < 1:
            raise ValueError("attempt must be positive")
        target = self.attempts_dir / f"{task_key}.attempt-{int(attempt)}.json"
        atomic_write_json(target, checked)
        return target

    def rebuild_summary(self) -> Dict[str, Any]:
        results = []
        for path in sorted(self.attempts_dir.glob("*.json")) if self.attempts_dir.is_dir() else ():
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("run_id") != self.run_id:
                    continue
                results.append(candidate)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        grouped: Dict[tuple, Dict[str, Any]] = {}
        for result in results:
            key = (
                result.get("metrics_schema_version", 1),
                result.get("evaluation_version", "legacy_v1"),
                result.get("execution_policy", "legacy"),
                result.get("movement_mode", "step"),
            )
            group = grouped.setdefault(
                key,
                {
                    "metrics_schema_version": key[0],
                    "evaluation_version": key[1],
                    "execution_policy": key[2],
                    "movement_mode": key[3],
                    "task_count": 0,
                    "valid_evaluation_count": 0,
                    "task_success_count": 0,
                },
            )
            group["task_count"] += 1
            if result.get("evaluation_status") == "valid":
                group["valid_evaluation_count"] += 1
                if result.get("task_success") is True:
                    group["task_success_count"] += 1
        return {
            "run_id": self.run_id,
            "total_results": len(results),
            "success_count": sum(1 for result in results if result.get("task_success") is True),
            "failure_count": sum(1 for result in results if result.get("task_success") is False),
            "groups": [grouped[key] for key in sorted(grouped, key=repr)],
            "results": results,
        }
