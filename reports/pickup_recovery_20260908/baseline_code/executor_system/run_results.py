"""Stable result contracts for generated-plan execution.

This module deliberately has no dependency on the batch CLI.  The child
runtime reports what it observed; the parent supplies process authority and
run identity before a result becomes part of an experiment summary.
"""

from __future__ import annotations

import json
import math
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
    """Count logical plan actions independently from their attempts.

    ``record_terminal(started=False)`` records scheduler skips/cancellations
    before admission without fabricating either a start or an attempt.
    """

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
        started: bool = True,
    ) -> None:
        with self._lock:
            self._ensure_mutable()
            normalized = str(key)
            if status not in _TERMINAL_STATUSES:
                raise ValueError(f"unsupported action terminal status: {status!r}")
            if normalized in self._terminal:
                raise RuntimeError(f"action already has a terminal result: {normalized}")
            if not started and status not in {'skipped', 'cancelled'}:
                raise ValueError('unstarted terminal actions must be skipped or cancelled')
            self._planned.add(normalized)
            if started:
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
    """Replace only after a complete, standard JSON file has reached disk."""
    path = Path(path)
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True,
                         allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


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
    # Includes nested metrics; JSON's default permissive NaN parser is unsafe.
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("runner metrics must contain finite JSON values") from exc
    version = result.get("metrics_schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported metrics_schema_version")
    # These labels become dictionary keys during grouping. Validate before
    # completion is committed or an attempt can replace an earlier result.
    grouping_labels = {
        "movement_mode": ("step", ("step", "teleport")),
        "execution_policy": ("legacy", ("legacy", "strict")),
        "evaluation_version": ("legacy_v1", ("legacy_v1", "fixed_goals_v2", "atomic_goals_v3")),
    }
    for field, (default, allowed) in grouping_labels.items():
        label = result.get(field, default)
        if not isinstance(label, str) or label not in allowed:
            raise ValueError(f"unsupported {field}: expected one of {allowed}")
    scheduler = result.get("scheduler_version", 1)
    if type(scheduler) is not int or scheduler not in (1, 2):
        raise ValueError("unsupported scheduler_version")
    is_v2 = version == 2
    if is_v2:
        if result.get("evaluation_version") not in {"fixed_goals_v2", "atomic_goals_v3"}:
            raise ValueError("v2 result must use a supported evaluation version")
        if result.get("execution_policy") not in {"legacy", "strict"}:
            raise ValueError("v2 result requires an explicit execution_policy")
        if result["execution_policy"] == "strict" and scheduler != 2:
            raise ValueError("strict v2 result requires scheduler_version=2")
        for key, expected in expected_identity.items():
            if result.get(key) != expected:
                raise ValueError(f"v2 result {key} does not match parent identity")
        if returncode == 0:
            _validate_completed_v2(result)
    else:
        result["evaluation_version"] = "legacy_v1"
        if returncode == 0 and result.get("task_success") is not None:
            if (result.get("evaluation_status") != "valid" or type(result.get("task_success")) is not bool
                    or type(result.get("sr")) not in (int, float) or result["sr"] not in (0, 1)
                    or result["task_success"] != bool(result["sr"])):
                raise ValueError("legacy task_success is inconsistent with evaluation/sr")
        for key, expected in expected_identity.items():
            result[key] = expected

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
        result["satisfied_goal_count"] = None
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
        result["satisfied_goal_count"] = None
        result["status"] = "failed"
        result["timed_out"] = False
    return result


def _validate_completed_v2(result: Mapping[str, Any]) -> None:
    if (not isinstance(result.get("run_id"), str) or not result["run_id"]
            or not isinstance(result.get("task_key"), str)
            or not _TASK_KEY_PATTERN.fullmatch(result["task_key"])
            or type(result.get("attempt")) is not int or result["attempt"] < 1):
        raise ValueError("invalid v2 run identity")
    if result.get("execution_status") not in {"completed", "partial", "failed", "timeout", "cancelled"}:
        raise ValueError("invalid v2 execution_status")
    counts = result.get("action_counts")
    names = ("planned", "started", "succeeded", "failed", "skipped", "cancelled", "unexecuted", "attempts")
    if not isinstance(counts, Mapping) or any(type(counts.get(name)) is not int or counts[name] < 0 for name in names):
        raise ValueError("v2 requires nonnegative integer action_counts")
    terminal = sum(counts[name] for name in ("succeeded", "failed", "skipped", "cancelled"))
    minimum_started = (counts['succeeded'] + counts['failed']
                       if result.get('scheduler_version') == 2 else terminal)
    if (terminal + counts["unexecuted"] != counts["planned"]
            or not minimum_started <= counts["started"] <= counts["planned"]
            or counts["attempts"] < counts["started"]):
        raise ValueError("inconsistent logical action counts")
    denominator = counts["succeeded"] + counts["failed"]
    raw = result.get("raw_action_sr")
    if denominator:
        if type(raw) not in (int, float) or not math.isclose(raw, counts["succeeded"] / denominator):
            raise ValueError("raw_action_sr is inconsistent with action counts")
    elif raw is not None:
        raise ValueError("raw_action_sr must be null without terminal actions")
    ignored = result.get("ignored_failure_count")
    if type(ignored) is not int or not 0 <= ignored <= counts["failed"]:
        raise ValueError("invalid ignored_failure_count")
    if ((result["execution_status"] == "completed" and counts["failed"])
            or (result["execution_status"] == "partial" and not counts["failed"])):
        raise ValueError("execution_status is inconsistent with action failures")
    evaluation_status = result.get("evaluation_status")
    if evaluation_status not in {"valid", "invalid", "incomplete"}:
        raise ValueError("invalid v2 evaluation_status")
    if evaluation_status == "valid":
        allowed_execution = {"completed", "partial"}
        if result.get("scheduler_version") == 2:
            if result.get("execution_quiescent") is not True:
                raise ValueError("valid scheduler2 evaluation requires quiescent execution")
            allowed_execution.add("failed")
        if result.get("execution_status") not in allowed_execution:
            raise ValueError("valid evaluation requires completed execution")
        for name in ("gcr", "tc", "sr", "ru"):
            value = result.get(name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"valid evaluation requires finite {name}")
        if result["sr"] not in (0, 1) or result["tc"] not in (0, 1):
            raise ValueError("sr and tc must be binary")
        if not 0 <= result["gcr"] <= 1:
            raise ValueError("gcr must be between zero and one")
        if type(result.get("task_success")) is not bool or result["task_success"] != bool(result["sr"]):
            raise ValueError("task_success must equal bool(sr)")
        original, satisfied = result.get("original_goal_count"), result.get("satisfied_goal_count")
        denominator = result.get("atomic_goal_count") if result.get("evaluation_version") == "atomic_goals_v3" else original
        if (type(original) is not int or original < 0 or type(denominator) is not int
                or denominator < original or type(satisfied) is not int or not 0 <= satisfied <= denominator):
            raise ValueError("invalid goal counts")
        if result.get("evaluation_version") == "atomic_goals_v3":
            subgoals = result.get("subgoal_results")
            if not isinstance(subgoals, list) or len(subgoals) != denominator:
                raise ValueError("atomic goal count requires matching subgoal_results")
            parent_indices = set()
            for index, subgoal in enumerate(subgoals):
                if not isinstance(subgoal, Mapping):
                    raise ValueError("invalid atomic subgoal evidence")
                parent_index = subgoal.get("original_goal_index")
                subgoal_index = subgoal.get("subgoal_index")
                if (type(parent_index) is not int or not 0 <= parent_index < original
                        or type(subgoal_index) is not int or subgoal_index != index
                        or subgoal.get("status") not in {"satisfied", "unsatisfied"}):
                    raise ValueError("invalid atomic subgoal identity or status")
                parent_indices.add(parent_index)
            if parent_indices != set(range(original)):
                raise ValueError("atomic subgoals must cover every original goal")
            if sum(subgoal["status"] == "satisfied" for subgoal in subgoals) != satisfied:
                raise ValueError("satisfied count is inconsistent with subgoal evidence")
        expected_gcr = satisfied / denominator if denominator else 1.0
        if not math.isclose(result["gcr"], expected_gcr, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("gcr is inconsistent with fixed goal counts")
        if result["tc"] != int(satisfied == denominator):
            raise ValueError("tc is inconsistent with fixed goal counts")
        if result["sr"] != int(result["tc"] == 1 and result["ru"] == 1):
            raise ValueError("sr is inconsistent with tc/ru")
    elif any(result.get(name) is not None for name in
             ("task_success", "gcr", "tc", "sr", "ru", "satisfied_goal_count")):
        raise ValueError("unreliable evaluation must not contain final metrics")


class ResultStorageError(RuntimeError):
    """Durable evidence could not be saved; the batch must fail explicitly."""


class RunResultStore:
    """Each completed attempt is authoritative; summaries are replaceable views."""

    def __init__(self, output_dir: Path, run_id: str) -> None:
        self.output_dir = Path(output_dir)
        self.run_id = str(run_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.run_id):
            raise ValueError("run_id must be a safe path component")
        self.run_dir = self.output_dir / "runs" / self.run_id
        self.summary_path: Optional[Path] = None
        self.summary_metadata: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._attempt_locks = {}
        self.progress = None

    def attempt_dir(self, task_key: str, attempt: int) -> Path:
        if not _TASK_KEY_PATTERN.fullmatch(str(task_key)):
            raise ValueError("task_key must be a SHA-256 executable path digest")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        return self.run_dir / task_key / f"attempt_{attempt}"

    def start_attempt(self, task_key: str, attempt: int) -> Path:
        directory = self.attempt_dir(task_key, attempt)
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    def _checked_attempt(self, task_key, attempt, result):
        identity = dict(run_id=self.run_id, task_key=task_key, attempt=attempt)
        if any(type(result.get(key)) is not type(value) or result.get(key) != value
               for key, value in identity.items()):
            raise ValueError("attempt identity does not match storage path")
        returncode = result.get("returncode")
        if returncode is None:
            returncode = {"completed": 0, "timeout": 124}.get(result.get("process_status"), 1)
        if type(returncode) is not int:
            raise ValueError("returncode must be an integer")
        checked = validate_result(result, returncode=returncode, expected_identity=identity)
        # Parent rejection of zero-exit malformed metrics must remain failed.
        if result.get("process_status") in {"failed", "cancelled"}:
            checked.update(process_status=result["process_status"], status=result.get("status", "failed"),
                           execution_status=result.get("execution_status", "failed"),
                           evaluation_status="incomplete", task_success=None,
                           gcr=None, tc=None, sr=None, ru=None, satisfied_goal_count=None)
        checked["returncode"] = returncode
        return checked

    def record_attempt(self, task_key: str, attempt: int, result: Mapping[str, Any]) -> Path:
        directory = self.attempt_dir(task_key, attempt)
        with self._lock:
            attempt_lock = self._attempt_locks.setdefault((task_key, attempt), threading.Lock())
        with attempt_lock:
            normalized = dict(result)
            for stream in ("stdout", "stderr"):
                if stream in normalized:
                    normalized[stream] = normalize_output(normalized[stream])
            checked = self._checked_attempt(task_key, attempt, normalized)
            target = directory / "result.json"
            if target.exists():
                raise FileExistsError(f"completed attempt already exists: {target}")
            directory.mkdir(parents=True, exist_ok=True)
            # Sync the full file-backed output before publishing completion.
            # Existing logs can include undecodable bytes: never rewrite them.
            for stream in ("stdout", "stderr"):
                log = directory / f"{stream}.log"
                keep = stream == "stderr" or stream in checked or checked.get("status") != "success"
                if not log.exists() and keep:
                    with log.open("x", encoding="utf-8") as handle:
                        handle.write(checked.get(stream, ""))
                        handle.flush()
                        os.fsync(handle.fileno())
                elif log.exists():
                    with log.open("rb") as handle:
                        os.fsync(handle.fileno())
                if log.exists():
                    checked[f"{stream}_path"] = str(log)
            checked["storage_status"] = "completed"
            atomic_write_json(target, checked)
            return target

    def write_summary(self, run_status: str, **extra: Any) -> Dict[str, Any]:
        with self._lock:
            summary = {**self.summary_metadata, **self.rebuild_summary(),
                       "run_status": run_status, **extra}
            if self.summary_path is not None:
                atomic_write_json(self.summary_path, summary)
            return summary

    def rebuild_summary(self) -> Dict[str, Any]:
        with self._lock:
            latest: Dict[str, Dict[str, Any]] = {}
            histories: Dict[str, list] = {}
            interrupted = []
            directories = sorted(self.run_dir.glob("*/attempt_*"))
            for directory in directories:
                task_key = directory.parent.name
                try:
                    attempt = int(directory.name.removeprefix("attempt_"))
                    if self.attempt_dir(task_key, attempt) != directory:
                        continue
                except (ValueError, TypeError):
                    continue
                path = directory / "result.json"
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(candidate, dict) or candidate.get("storage_status") != "completed":
                        raise ValueError("attempt completion was not committed by parent")
                    checked = self._checked_attempt(task_key, attempt, candidate)
                except (ValueError, TypeError, UnicodeError, OSError) as exc:
                    interrupted.append(dict(run_id=self.run_id, task_key=task_key, attempt=attempt,
                                            status="interrupted", error=str(exc), attempt_dir=str(directory)))
                    continue
                histories.setdefault(task_key, []).append(checked)
                if task_key not in latest or attempt > latest[task_key]["attempt"]:
                    latest[task_key] = checked
            results = []
            for task_key in sorted(latest):
                result = dict(latest[task_key])
                history = sorted(histories[task_key], key=lambda value: value["attempt"])
                result["attempt_count"] = len(history)
                result["timed_out_attempt_count"] = sum(bool(r.get("timed_out")) for r in history)
                result["attempts"] = [dict(attempt=r["attempt"], status=r.get("status"),
                    timed_out=bool(r.get("timed_out")), returncode=r.get("returncode"),
                    result_path=str(self.attempt_dir(task_key, r["attempt"]) / "result.json")) for r in history]
                results.append(result)
            results.sort(key=lambda result: (str(result.get("executable_path", "")), result["task_key"]))
            grouped: Dict[tuple, Dict[str, Any]] = {}
            for result in results:
                key = (result.get("metrics_schema_version", 1), result["evaluation_version"],
                       result.get("execution_policy", "legacy"), result.get("movement_mode", "step"),
                       result.get("scheduler_version", 1))
                group = grouped.setdefault(key, dict(metrics_schema_version=key[0], evaluation_version=key[1],
                    execution_policy=key[2], movement_mode=key[3], scheduler_version=key[4], task_count=0, total_task_count=0,
                    valid_evaluation_count=0, task_success_count=0))
                group["task_count"] += 1
                group["total_task_count"] += 1
                if result.get("evaluation_status") == "valid":
                    group["valid_evaluation_count"] += 1
                    group["task_success_count"] += int(result.get("task_success") is True)
            groups = [grouped[key] for key in sorted(grouped, key=repr)]
            return dict(run_id=self.run_id, run_dir=str(self.run_dir), total_results=len(results),
                success_count=sum(r.get("process_status") == "completed" for r in results),
                failure_count=sum(r.get("process_status") in {"failed", "cancelled"} for r in results),
                timeout_count=sum(r.get("process_status") == "timeout" for r in results),
                groups=groups, result_groups=groups, results=results, interrupted_attempts=interrupted,
                timeout_retry_tasks=[r.get("executable_path", r["task_key"]) for r in results
                                     if r["timed_out_attempt_count"]])


class RunProgress:
    """Small scheduler-owned state; publication never touches durable results."""

    def __init__(self, path, **metadata):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._active = set()
        self._retry = set()
        self._terminal = set()
        self._state = dict(protocol_version=1, phase="running", attempts_started=0,
            attempts_persisted=0, success_count=0, failure_count=0, timeout_count=0,
            valid_evaluation_count=0, task_success_count=0, storage_error=None, **metadata)

    def started(self, key):
        with self._lock:
            self._active.add(key)
            self._retry.discard(key)
            self._state["attempts_started"] += 1

    def finished(self, key, result, *, retry):
        with self._lock:
            self._active.discard(key)
            self._state["attempts_persisted"] += 1
            if retry:
                self._retry.add(key)
            elif key not in self._terminal:
                self._terminal.add(key)
                self._retry.discard(key)
                status = result.get("process_status")
                counter = {"completed": "success_count", "timeout": "timeout_count"}.get(status, "failure_count")
                self._state[counter] += 1
                if result.get("evaluation_status") == "valid":
                    self._state["valid_evaluation_count"] += 1
                    self._state["task_success_count"] += int(result.get("task_success") is True)

    def storage_failed(self, key, error):
        with self._lock:
            self._active.discard(key)
            self._state["storage_error"] = str(error)

    def publish(self):
        if self.path is None:
            return
        # Serialize publishers so an older heartbeat cannot overwrite a phase change.
        with self._publish_lock:
            from datetime import datetime, timezone
            with self._lock:
                snapshot = dict(self._state, running_tasks=len(self._active),
                    completed_tasks=len(self._terminal), pending_retry_tasks=len(self._retry),
                    updated_at=datetime.now(timezone.utc).isoformat())
            try:
                atomic_write_json(self.path, snapshot)
            except (OSError, ValueError) as exc:
                # Progress is a replaceable view, not evidence of task completion.
                with self._lock:
                    self._state["progress_error"] = str(exc)

    def phase(self, value, **extra):
        with self._lock:
            self._state.update(phase=value, **extra)
        self.publish()

    def start(self):
        self.publish()
        if self.path is not None:
            self._thread = threading.Thread(target=self._heartbeat, name="run-progress", daemon=True)
            self._thread.start()

    def _heartbeat(self):
        while not self._stop.wait(1):
            self.publish()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
