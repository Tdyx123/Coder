#!/usr/bin/env python3
"""Run generated Python executables concurrently and collect metrics."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _path in (_SCRIPTS_DIR, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.append(_path_str)

from executor_system.action_plan import (  # noqa: E402
    ACTION_FAILED,
    ACTION_SUCCESS,
    Action,
    ActionResult,
    ExecutionLogger,
    FAILURE_RETRY,
    FAILURE_SKIP,
    FAILURE_WAIT_AND_RETRY,
    PlanLoader,
    PlanValidator,
    ROBOT_EXECUTING,
    ROBOT_FINISHED_STAGE,
    ROBOT_ACTION_FAILED,
    ROBOT_ACTION_SUCCESS,
    StagePlan,
    WorldState,
    action_allows_failure_retry,
)
from executor_system.executor import Executor, PhaseCoordinator  # noqa: E402
from executor_system.runtime import is_pickup_object_clip_error  # noqa: E402
from baseline_converters import pddlrun  # noqa: E402


DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_RETRIES = 2
GPU_CLEANUP_PROCESS_SUFFIX = "0d69f666c7f282e54abfe58f1e917"
IGNORED_FAILURE_ACTION_TYPES = {"Teleport", "TeleportObjectToHand"}
BASE_LINE_CHOICES = ("LaMMA-P", "SMART-LLM")


def failure_ignored_for_ratio(action: Action, exc: BaseException) -> bool:
    if action.action_type in IGNORED_FAILURE_ACTION_TYPES:
        return True
    return (
        action.action_type == "PickupObject"
        and is_pickup_object_clip_error(exc)
    )


class PlanExecutionTimeout(TimeoutError):
    """Raised when the cooperative task-plan deadline is reached."""


def write_result_json(path: Path, result: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def action_success_rate(executed_actions: int, failed_actions: int) -> float:
    return (
        (executed_actions - failed_actions) / executed_actions
        if executed_actions
        else 1.0
    )


def normalize_result_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    result.pop("exec_rate", None)
    executed_actions = int(result.get("executed_actions", 0) or 0)
    failed_actions = int(result.get("failed_actions", 0) or 0)
    result["action_sr"] = action_success_rate(executed_actions, failed_actions)
    return result


@dataclass
class TolerantRunStats:
    start_time: float = field(default_factory=time.monotonic)
    executed_actions: int = 0
    failed_actions: int = 0
    robot_failures: List[Dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    timeout_message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_started(self) -> None:
        with self._lock:
            self.executed_actions += 1

    def record_failure(
        self,
        stage_id: str,
        robot_id: str,
        action: Action,
        action_index: int,
        exc: BaseException,
    ) -> None:
        ignored = failure_ignored_for_ratio(action, exc)
        failure = {
            "stage_id": stage_id,
            "robot_id": robot_id,
            "action_index": action_index,
            "action_type": action.action_type,
            "error": str(exc),
            "ignored_for_failure_ratio": ignored,
        }
        with self._lock:
            self.robot_failures.append(failure)
            if not ignored:
                self.failed_actions += 1

    def record_timeout(self, message: str) -> None:
        with self._lock:
            self.timed_out = True
            self.timeout_message = message

    def to_dict(self, *, status: str = "success") -> Dict[str, Any]:
        with self._lock:
            executed_actions = int(self.executed_actions)
            failed_actions = int(self.failed_actions)
            robot_failures = [dict(failure) for failure in self.robot_failures]
            timed_out = bool(self.timed_out)
            timeout_message = self.timeout_message
        return {
            "status": "timeout" if timed_out else status,
            "timed_out": timed_out,
            "timeout_message": timeout_message,
            "run_time_seconds": time.monotonic() - self.start_time,
            "executed_actions": executed_actions,
            "failed_actions": failed_actions,
            "action_sr": action_success_rate(executed_actions, failed_actions),
            "failure_action_ratio": (
                failed_actions / executed_actions if executed_actions else 0.0
            ),
            "robot_failures": robot_failures,
        }


def _check_deadline(deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise PlanExecutionTimeout("task-plan execution exceeded timeout")


class TolerantExecutor(Executor):
    """Execute one robot queue, stopping only this robot on action failure."""

    def __init__(
        self,
        runtime: Any,
        robot_id: str,
        actions: Sequence[Action],
        *,
        stage_id: str,
        stats: TolerantRunStats,
        deadline: Optional[float],
        logger: Optional[ExecutionLogger] = None,
        phase_coordinator: Optional[PhaseCoordinator] = None,
    ) -> None:
        super().__init__(
            runtime,
            robot_id,
            actions,
            stage_id=stage_id,
            logger=logger,
            phase_coordinator=phase_coordinator,
        )
        self.stats = stats
        self.deadline = deadline

    def execute(self) -> WorldState:
        if not self.robot_id:
            raise RuntimeError("Executor requires a robot_id to execute an action queue.")
        return self._execute_queue()

    def wait_for_condition(self, action: Action) -> None:
        if action.wait_until is None:
            return

        while True:
            _check_deadline(self.deadline)
            self.world_state.refresh([self.state])
            if action.wait_until(self.world_state):
                return
            if (
                action.timeout_ticks is not None
                and self.state.wait_ticks >= action.timeout_ticks
            ):
                raise RuntimeError(
                    f"{self.robot_id} timed out waiting for {action.action_type}."
                )
            self.state.wait_ticks += 1
            agent_id = self.runtime.physical_agent_id(self.robot_id)
            self.runtime.step(
                {"action": "Pass", "agentId": agent_id},
                check_success=False,
                save_frame=False,
            )

    def _execute_queue(self) -> WorldState:
        tick = 0
        try:
            while not self.state.finished():
                _check_deadline(self.deadline)
                action = self.state.next_action()
                if action is None:
                    break

                action_index = self.state.action_cursor
                self.world_state.tick = tick
                self.world_state.refresh([self.state])
                self.state.status = ROBOT_EXECUTING
                self.stats.record_started()
                try:
                    self.wait_for_condition(action)
                    _check_deadline(self.deadline)
                    event = self.execute_action(action)
                    self.record_temperature_goal_progress()
                except PlanExecutionTimeout as exc:
                    self.stats.record_failure(
                        self.state.current_stage_id,
                        self.robot_id,
                        action,
                        action_index,
                        exc,
                    )
                    raise
                except Exception as exc:
                    if self.handle_failure(action, action_index, exc, tick):
                        tick += 1
                    continue

                result = ActionResult(self.robot_id, action, ACTION_SUCCESS, event=event)
                self.state.last_action_result = result
                self.state.action_cursor += 1
                self.state.wait_ticks = 0
                self.state.status = (
                    ROBOT_FINISHED_STAGE
                    if self.state.finished()
                    else ROBOT_ACTION_SUCCESS
                )
                self.logger.result(tick, result)
                tick += 1
        finally:
            self.state.status = ROBOT_FINISHED_STAGE
            self.world_state.refresh([self.state])
            if self.phase_coordinator is not None and self.robot_id:
                self.phase_coordinator.mark_agent_done(
                    self.runtime.physical_agent_id(self.robot_id)
                )
        return self.world_state

    def handle_failure(
        self,
        action: Action,
        action_index: int,
        exc: BaseException,
        tick: int,
    ) -> bool:
        action_key = action.stable_id(self.robot_id, self.state.action_cursor)
        retries = self.state.retries_by_action.get(action_key, 0)

        if (
            action.on_failure != FAILURE_SKIP
            and action_allows_failure_retry(action)
            and action.on_failure in {FAILURE_RETRY, FAILURE_WAIT_AND_RETRY}
            and retries < action.max_retries
        ):
            self.state.retries_by_action[action_key] = retries + 1
            self.state.wait_ticks += 1
            self.state.status = ROBOT_ACTION_FAILED
            result = ActionResult(
                self.robot_id,
                action,
                ACTION_FAILED,
                error_message=str(exc),
                attempts=retries + 1,
            )
            self.state.last_action_result = result
            self.logger.result(tick, result)
            if action.on_failure == FAILURE_WAIT_AND_RETRY:
                agent_id = self.runtime.physical_agent_id(self.robot_id)
                self.runtime.step(
                    {"action": "Pass", "agentId": agent_id},
                    check_success=False,
                    save_frame=False,
                )
            return False

        self.stats.record_failure(
            self.state.current_stage_id,
            self.robot_id,
            action,
            action_index,
            exc,
        )
        result = ActionResult(
            self.robot_id,
            action,
            ACTION_FAILED,
            error_message=str(exc),
        )
        self.state.last_action_result = result
        self.state.action_cursor += 1
        self.state.wait_ticks = 0
        self.state.status = (
            ROBOT_FINISHED_STAGE
            if self.state.finished()
            else ROBOT_ACTION_FAILED
        )
        self.logger.result(tick, result)
        return True


class TolerantStageRunner:
    def __init__(
        self,
        runtime_obj: Any,
        *,
        stats: TolerantRunStats,
        deadline: Optional[float],
        logger: Optional[ExecutionLogger] = None,
    ) -> None:
        self.runtime = runtime_obj
        self.stats = stats
        self.deadline = deadline
        self.logger = logger or ExecutionLogger()
        self.world_state = WorldState(runtime_obj)

    def execute_stage(self, stage: StagePlan) -> WorldState:
        self.logger.stage_started(stage)
        _check_deadline(self.deadline)
        active_agent_ids = {
            self.runtime.physical_agent_id(robot_id)
            for robot_id in stage.robot_action_queues
        }
        phase_coordinator = PhaseCoordinator(
            self.runtime,
            active_agent_ids,
            deadline=self.deadline,
            timeout_error_factory=PlanExecutionTimeout,
        )
        executors = [
            TolerantExecutor(
                self.runtime,
                robot_id,
                actions,
                stage_id=stage.stage_id,
                stats=self.stats,
                deadline=self.deadline,
                logger=self.logger,
                phase_coordinator=phase_coordinator,
            )
            for robot_id, actions in stage.robot_action_queues.items()
        ]

        timeout_errors: List[BaseException] = []
        timeout_lock = threading.Lock()

        def run_executor(executor: TolerantExecutor) -> None:
            try:
                executor.execute()
            except PlanExecutionTimeout as exc:
                with timeout_lock:
                    timeout_errors.append(exc)

        threads = [
            threading.Thread(
                target=run_executor,
                args=(executor,),
                name=f"{stage.stage_id}-{executor.robot_id}",
                daemon=True,
            )
            for executor in executors
        ]
        for thread in threads:
            thread.start()

        for thread in threads:
            if self.deadline is None:
                thread.join()
                continue
            remaining = max(0.0, self.deadline - time.monotonic())
            thread.join(timeout=remaining)

        alive_threads = [thread.name for thread in threads if thread.is_alive()]
        if alive_threads:
            raise PlanExecutionTimeout(
                "task-plan execution exceeded timeout with active robot threads: "
                + ", ".join(alive_threads)
            )
        if timeout_errors:
            raise PlanExecutionTimeout(str(timeout_errors[0]))

        self.world_state.refresh([executor.state for executor in executors])
        return self.world_state


def run_action_plan_tolerant(
    runtime_obj: Any,
    raw_plan: Any,
    *,
    timeout_seconds: Optional[float] = DEFAULT_TIMEOUT_SECONDS,
    logger: Optional[ExecutionLogger] = None,
) -> Dict[str, Any]:
    """Run a task plan without letting one robot failure fail the whole plan."""

    stats = TolerantRunStats()
    deadline = (
        stats.start_time + float(timeout_seconds)
        if timeout_seconds is not None and float(timeout_seconds) > 0
        else None
    )
    plan = PlanLoader().load(raw_plan)
    PlanValidator(runtime_obj).validate(plan)
    logger = logger or ExecutionLogger()

    try:
        for stage in plan.stages:
            _check_deadline(deadline)
            runner = TolerantStageRunner(
                runtime_obj,
                stats=stats,
                deadline=deadline,
                logger=logger,
            )
            runner.execute_stage(stage)
        if plan.global_success_condition is not None:
            world_state = WorldState(runtime_obj)
            world_state.refresh([])
            if not plan.global_success_condition(world_state):
                raise RuntimeError(f"Global success condition failed for {plan.task_id}.")
    except PlanExecutionTimeout as exc:
        stats.record_timeout(str(exc))
    return stats.to_dict()


def default_baseline_root(base_line: str) -> Path:
    return _REPO_ROOT / "baselines" / base_line


def baseline_summary_paths(base_line: str, root: Path) -> List[Path]:
    if base_line == "LaMMA-P":
        return [root / "plan_to_code_results" / "plan_to_code_results.json"]
    if base_line == "SMART-LLM":
        return [root / "plan_to_code_results.json"]
    raise RuntimeError(f"Unsupported base-line: {base_line}")


def baseline_fallback_search_root(base_line: str, root: Path) -> Path:
    if base_line == "LaMMA-P":
        preferred = root / "logs" / "intermediate_runs"
    elif base_line == "SMART-LLM":
        preferred = root / "logs"
    else:
        raise RuntimeError(f"Unsupported base-line: {base_line}")
    return preferred if preferred.is_dir() else root


def is_successful_conversion_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("status") == "success"
        and record.get("success") is True
    )


def generated_executable_from_record(
    record: Dict[str, Any],
    *,
    root: Path,
    summary_path: Path,
) -> Optional[Path]:
    generated = record.get("generated")
    if not isinstance(generated, dict):
        return None
    raw_path = generated.get("executable_plan")
    if not raw_path:
        return None
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path

    root_relative = root / path
    if root_relative.is_file():
        return root_relative
    return summary_path.parent / path


def is_runner_compatible_executable(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return (
        "generated_plan_runtime" in text
        and "BUNDLE_DATA" in text
        and "--runner-mode" in text
    )


def unique_existing_compatible_paths(paths: Sequence[Path]) -> List[Path]:
    seen = set()
    resolved: List[Path] = []
    for path in paths:
        try:
            resolved_path = path.expanduser().resolve()
        except OSError:
            continue
        if resolved_path in seen or not resolved_path.is_file():
            continue
        if not is_runner_compatible_executable(resolved_path):
            continue
        seen.add(resolved_path)
        resolved.append(resolved_path)
    return resolved


def discover_baseline_summary_executables(base_line: str, root: Path) -> List[Path]:
    for summary_path in baseline_summary_paths(base_line, root):
        if not summary_path.is_file():
            continue
        try:
            records = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse baseline summary {summary_path}: {exc}") from exc
        if not isinstance(records, list):
            raise RuntimeError(f"Baseline summary must contain a result list: {summary_path}")

        candidates = [
            generated_executable_from_record(
                record,
                root=root,
                summary_path=summary_path,
            )
            for record in records
            if is_successful_conversion_record(record)
        ]
        compatible = unique_existing_compatible_paths(
            [path for path in candidates if path is not None]
        )
        if compatible:
            return compatible
    return []


def discover_baseline_fallback_executables(base_line: str, root: Path) -> List[Path]:
    search_root = baseline_fallback_search_root(base_line, root)
    candidates = sorted(search_root.rglob("plan_to_code/executable_plan.py"))
    return unique_existing_compatible_paths(candidates)


def discover_baseline_executable_plans(
    base_line: str,
    root: Optional[str],
) -> List[Path]:
    discovery_root = (
        Path(root).expanduser() if root else default_baseline_root(base_line)
    ).resolve()
    if not discovery_root.is_dir():
        raise RuntimeError(
            f"Baseline discovery root not found or not a directory: {discovery_root}"
        )

    summary_candidates = discover_baseline_summary_executables(
        base_line,
        discovery_root,
    )
    if summary_candidates:
        return summary_candidates
    return discover_baseline_fallback_executables(base_line, discovery_root)


def discover_parallel_run_executable_plans(parallel_run: str) -> List[Path]:
    try:
        task_run_dirs = pddlrun.discover_parallel_run_task_runs(parallel_run)
    except pddlrun.PlanToCodeError as exc:
        raise RuntimeError(str(exc)) from exc

    candidates: List[Path] = []
    missing_count = 0
    incompatible_count = 0
    seen = set()
    for task_run_dir in task_run_dirs:
        executable_path = task_run_dir / "plan_to_code" / "executable_plan.py"
        try:
            resolved_path = executable_path.expanduser().resolve()
        except OSError:
            missing_count += 1
            continue
        if not resolved_path.is_file():
            missing_count += 1
            continue
        if not is_runner_compatible_executable(resolved_path):
            incompatible_count += 1
            continue
        if resolved_path in seen:
            continue
        seen.add(resolved_path)
        candidates.append(resolved_path)

    skipped_parts = []
    if missing_count:
        skipped_parts.append(f"{missing_count} missing")
    if incompatible_count:
        skipped_parts.append(f"{incompatible_count} incompatible")
    if skipped_parts:
        print(
            "Skipped "
            + ", ".join(skipped_parts)
            + f" generated executable(s) for parallel run: {parallel_run}"
        )

    if not candidates:
        raise RuntimeError(
            "No runner-compatible plan_to_code/executable_plan.py files found "
            f"for parallel run: {parallel_run}. Run "
            "`python scripts/plantocode.py --parallel-run "
            f"{parallel_run}` first."
        )
    return candidates


def discover_executable_plans(
    explicit_paths: Sequence[str],
    root: Optional[str],
    py_dirs: Sequence[str] = (),
    base_line: Optional[str] = None,
    parallel_run: Optional[str] = None,
) -> List[Path]:
    candidates: List[Path] = []
    baseline_candidates: List[Path] = []
    if parallel_run:
        candidates.extend(discover_parallel_run_executable_plans(parallel_run))
    if base_line:
        baseline_candidates = discover_baseline_executable_plans(base_line, root)
        candidates.extend(baseline_candidates)
    for raw_path in explicit_paths:
        candidates.append(Path(raw_path).expanduser())
    if root and not base_line:
        candidates.extend(
            sorted(Path(root).expanduser().rglob("plan_to_code/executable_plan.py"))
        )
    for raw_dir in py_dirs:
        py_dir = Path(raw_dir).expanduser()
        if not py_dir.is_dir():
            raise RuntimeError(f"Python script directory not found: {py_dir}")
        py_files = sorted(py_dir.glob("*.py"))
        if not py_files:
            raise RuntimeError(f"No Python scripts found in directory: {py_dir}")
        candidates.extend(py_files)

    seen = set()
    resolved: List[Path] = []
    for path in candidates:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            raise RuntimeError(f"Generated executable not found: {path}")
        resolved.append(path)
    if base_line and not baseline_candidates and not resolved:
        baseline_root = (
            Path(root).expanduser() if root else default_baseline_root(base_line)
        ).resolve()
        raise RuntimeError(
            f"No runner-compatible {base_line} executable_plan.py files discovered "
            f"under {baseline_root}"
        )
    return resolved


def summary_output_path(output_dir: Path, base_line: Optional[str] = None) -> Path:
    date_suffix = time.strftime("%m%d")
    stem = f"{base_line}_{date_suffix}" if base_line else date_suffix
    max_sequence = 0
    for path in output_dir.glob(f"{stem}_*.json"):
        sequence_text = path.stem[len(stem) + 1 :]
        if sequence_text.isdigit():
            max_sequence = max(max_sequence, int(sequence_text))
    return output_dir / f"{stem}_{max_sequence + 1:02d}.json"


def prune_stdout_for_result(
    result: Dict[str, Any],
    *,
    save_all_stdout: bool,
) -> Dict[str, Any]:
    if not save_all_stdout and not result.get("robot_failures"):
        result.pop("stdout", None)
    return result


def parse_gpu_cleanup_pids(
    nvidia_smi_output: str,
    *,
    process_suffix: str = GPU_CLEANUP_PROCESS_SUFFIX,
) -> List[int]:
    pids: List[int] = []
    for raw_line in nvidia_smi_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, separator, process_name = line.partition(",")
        if not separator:
            continue
        if not process_name.strip().endswith(process_suffix):
            continue
        try:
            pids.append(int(pid_text.strip()))
        except ValueError:
            continue
    return pids


def cleanup_gpu_processes(round_index: int) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "round": round_index,
        "matched_pids": [],
        "killed_pids": [],
        "error": "",
    }
    try:
        query = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name",
                "--format=csv,noheader",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        event["error"] = str(exc)
        return event

    if query.returncode != 0:
        event["error"] = query.stderr.strip() or (
            f"nvidia-smi exited with status {query.returncode}"
        )
        return event

    matched_pids = parse_gpu_cleanup_pids(query.stdout)
    event["matched_pids"] = matched_pids
    if not matched_pids:
        return event

    try:
        kill_result = subprocess.run(
            ["kill", "-9", *[str(pid) for pid in matched_pids]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        event["error"] = str(exc)
        return event

    if kill_result.returncode == 0:
        event["killed_pids"] = matched_pids
    else:
        event["error"] = kill_result.stderr.strip() or (
            f"kill exited with status {kill_result.returncode}"
        )
    return event


def gpu_cleanup_error_event(round_index: int, exc: BaseException) -> Dict[str, Any]:
    return {
        "round": round_index,
        "matched_pids": [],
        "killed_pids": [],
        "error": str(exc),
    }


def failed_result_for_exception(executable_path: Path, exc: BaseException) -> Dict[str, Any]:
    result = {
        "status": "failed",
        "timed_out": False,
        "run_time_seconds": 0.0,
        "gcr": None,
        "executed_actions": 0,
        "failed_actions": 0,
        "action_sr": 1.0,
        "failure_action_ratio": 0.0,
        "robot_failures": [],
        "returncode": 1,
        "executable_path": str(executable_path),
        "error": str(exc),
    }
    return normalize_result_metrics(result)


def compact_attempt_result(result: Dict[str, Any], attempt: int) -> Dict[str, Any]:
    return {
        "attempt": attempt,
        "status": result.get("status", "unknown"),
        "timed_out": bool(result.get("timed_out")),
        "returncode": result.get("returncode"),
        "timeout_message": result.get("timeout_message", ""),
        "run_time_seconds": result.get("run_time_seconds", 0.0),
        "executed_actions": result.get("executed_actions", 0),
        "failed_actions": result.get("failed_actions", 0),
        "action_sr": result.get("action_sr", 1.0),
        "failure_action_ratio": result.get("failure_action_ratio", 0.0),
    }


def run_generated_executable(
    executable_path: Path,
    *,
    metrics_output: Path,
    timeout_seconds: float,
    save_all_stdout: bool = False,
) -> Dict[str, Any]:
    start_time = time.monotonic()
    child_env = os.environ.copy()
    command = [
        sys.executable,
        str(executable_path),
        "--runner-mode",
        "--metrics-output",
        str(metrics_output),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        result = {
            "status": "timeout",
            "timed_out": True,
            "timeout_message": f"subprocess exceeded {timeout_seconds:g} seconds",
            "run_time_seconds": time.monotonic() - start_time,
            "gcr": None,
            "executed_actions": 0,
            "failed_actions": 0,
            "action_sr": 1.0,
            "failure_action_ratio": 0.0,
            "robot_failures": [],
            "returncode": 124,
            "executable_path": str(executable_path),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
        prune_stdout_for_result(result, save_all_stdout=save_all_stdout)
        return result

    if metrics_output.is_file():
        try:
            result = json.loads(metrics_output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = {}
    else:
        result = {}

    if not isinstance(result, dict):
        result = {}
    result.setdefault(
        "status",
        "success" if completed.returncode == 0 else "failed",
    )
    result.setdefault("timed_out", False)
    result.setdefault("run_time_seconds", time.monotonic() - start_time)
    result.setdefault("gcr", None)
    result.setdefault("executed_actions", 0)
    result.setdefault("failed_actions", 0)
    result.setdefault("failure_action_ratio", 0.0)
    result.setdefault("robot_failures", [])
    normalize_result_metrics(result)
    result["returncode"] = completed.returncode
    result["executable_path"] = str(executable_path)
    result["stdout"] = completed.stdout
    result["stderr"] = completed.stderr
    prune_stdout_for_result(result, save_all_stdout=save_all_stdout)
    return result


def run_executable_round(
    *,
    executor: ThreadPoolExecutor,
    executable_paths: Sequence[Path],
    temp_metrics_dir: Path,
    round_index: int,
    timeout_seconds: float,
    save_all_stdout: bool,
) -> Dict[Path, Dict[str, Any]]:
    future_to_path = {}
    for index, executable_path in enumerate(executable_paths, start=1):
        metrics_output = temp_metrics_dir / (
            f"metrics_round_{round_index:02d}_{index:04d}.json"
        )
        future = executor.submit(
            run_generated_executable,
            executable_path,
            metrics_output=metrics_output,
            timeout_seconds=timeout_seconds,
            save_all_stdout=save_all_stdout,
        )
        future_to_path[future] = executable_path

    round_results: Dict[Path, Dict[str, Any]] = {}
    for future in as_completed(future_to_path):
        executable_path = future_to_path[future]
        try:
            result = future.result()
        except Exception as exc:
            result = failed_result_for_exception(executable_path, exc)
            prune_stdout_for_result(
                result,
                save_all_stdout=save_all_stdout,
            )
        round_results[executable_path] = result
        status = result.get("status", "unknown")
        print(f"{status}: {executable_path}")
    return round_results


def add_attempt_metadata(
    result: Dict[str, Any],
    attempts: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    final_result = dict(result)
    attempt_list = [dict(attempt) for attempt in attempts]
    final_result["attempt_count"] = len(attempt_list)
    final_result["timed_out_attempt_count"] = sum(
        1 for attempt in attempt_list if attempt.get("timed_out")
    )
    final_result["attempts"] = attempt_list
    return final_result


def run_executables_with_retries(
    executable_paths: Sequence[Path],
    *,
    max_workers: int,
    temp_metrics_dir: Path,
    timeout_seconds: float,
    save_all_stdout: bool,
) -> tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    attempts_by_path: Dict[Path, List[Dict[str, Any]]] = {
        path: [] for path in executable_paths
    }
    final_results_by_path: Dict[Path, Dict[str, Any]] = {}
    pending_paths = list(executable_paths)
    gpu_cleanup_events: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for round_index in range(MAX_TIMEOUT_RETRIES + 1):
            if not pending_paths:
                break
            round_results = run_executable_round(
                executor=executor,
                executable_paths=pending_paths,
                temp_metrics_dir=temp_metrics_dir,
                round_index=round_index,
                timeout_seconds=timeout_seconds,
                save_all_stdout=save_all_stdout,
            )

            try:
                gpu_cleanup_events.append(cleanup_gpu_processes(round_index))
            except Exception as exc:
                gpu_cleanup_events.append(gpu_cleanup_error_event(round_index, exc))

            next_pending_paths: List[Path] = []
            attempt_number = round_index + 1
            for executable_path in pending_paths:
                result = round_results[executable_path]
                normalize_result_metrics(result)
                attempts_by_path[executable_path].append(
                    compact_attempt_result(result, attempt_number)
                )
                final_results_by_path[executable_path] = result
                if result.get("timed_out"):
                    next_pending_paths.append(executable_path)

            if round_index >= MAX_TIMEOUT_RETRIES:
                break
            pending_paths = next_pending_paths

    results: List[Dict[str, Any]] = []
    timeout_retry_tasks: List[str] = []
    for executable_path in executable_paths:
        attempts = attempts_by_path[executable_path]
        if any(attempt.get("timed_out") for attempt in attempts):
            timeout_retry_tasks.append(str(executable_path))
        results.append(
            add_attempt_metadata(
                final_results_by_path[executable_path],
                attempts,
            )
        )
    return results, timeout_retry_tasks, gpu_cleanup_events


def build_summary(
    results: Sequence[Dict[str, Any]],
    start_time: float,
    *,
    base_line: Optional[str] = None,
    discovery_root: Optional[Path] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    timeout_retry_tasks: Optional[Iterable[str]] = None,
    gpu_cleanup_events: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result_list = [normalize_result_metrics(dict(result)) for result in results]
    summary = {
        "total_results": len(result_list),
        "success_count": sum(
            1
            for result in result_list
            if result.get("returncode") == 0 and not result.get("timed_out")
        ),
        "failure_count": sum(
            1
            for result in result_list
            if result.get("returncode") not in (0, None) and not result.get("timed_out")
        ),
        "timeout_count": sum(1 for result in result_list if result.get("timed_out")),
        "timeout_retry_policy": {
            "max_retries": MAX_TIMEOUT_RETRIES,
            "timeout_seconds": float(timeout_seconds),
        },
        "timeout_retry_tasks": list(timeout_retry_tasks or []),
        "gpu_cleanup_events": [
            dict(event) for event in (gpu_cleanup_events or [])
        ],
        "total_run_time_seconds": time.monotonic() - start_time,
        "results": result_list,
    }
    if base_line:
        summary["base_line"] = base_line
        if discovery_root is not None:
            summary["discovery_root"] = str(discovery_root)
    return summary


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multiple generated Python executable files in parallel."
    )
    parser.add_argument(
        "executable_plans",
        nargs="*",
        help="Generated plan_to_code/executable_plan.py files to run.",
    )
    parser.add_argument(
        "--root",
        help="Root directory to recursively search for plan_to_code/executable_plan.py.",
    )
    parser.add_argument(
        "--parallel-run",
        help=(
            "Path to one parallel_runs output directory or summary.json whose "
            "generated task executable_plan.py files should be run."
        ),
    )
    parser.add_argument(
        "--base-line",
        choices=BASE_LINE_CHOICES,
        help=(
            "Discover runner-compatible generated plans for a baseline. "
            "Supported values: LaMMA-P, SMART-LLM."
        ),
    )
    parser.add_argument(
        "--py-dir",
        action="append",
        default=[],
        help="Directory whose direct child *.py files should be run.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum number of generated executables to run concurrently.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-executable timeout in seconds.",
    )
    parser.add_argument(
        "--output-dir",
        default="./coderun_results",
        help="Directory for the summary JSON file.",
    )
    parser.add_argument(
        "--save-all-stdout",
        action="store_true",
        help="Keep stdout in every result instead of only results with robot_failures.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    if args.max_workers < 1:
        print("ERROR: --max-workers must be at least 1")
        return 1
    if args.timeout_seconds <= 0:
        print("ERROR: --timeout-seconds must be positive")
        return 1
    if args.parallel_run and (
        args.executable_plans or args.root or args.py_dir or args.base_line
    ):
        print(
            "ERROR: --parallel-run cannot be combined with positional "
            "executable plans, --root, --py-dir, or --base-line"
        )
        return 1

    try:
        executable_paths = discover_executable_plans(
            args.executable_plans,
            args.root,
            args.py_dir,
            args.base_line,
            args.parallel_run,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    if not executable_paths:
        print("ERROR: no generated executable_plan.py files were provided or discovered")
        return 1

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    start_time = time.monotonic()
    results: List[Dict[str, Any]] = []
    timeout_retry_tasks: List[str] = []
    gpu_cleanup_events: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="parallel_runner_metrics_") as temp_dir:
        temp_metrics_dir = Path(temp_dir)
        results, timeout_retry_tasks, gpu_cleanup_events = run_executables_with_retries(
            executable_paths,
            max_workers=args.max_workers,
            temp_metrics_dir=temp_metrics_dir,
            timeout_seconds=float(args.timeout_seconds),
            save_all_stdout=args.save_all_stdout,
        )

    results.sort(key=lambda result: str(result.get("executable_path", "")))
    discovery_root = None
    if args.base_line:
        discovery_root = (
            Path(args.root).expanduser()
            if args.root
            else default_baseline_root(args.base_line)
        ).resolve()
    summary = build_summary(
        results,
        start_time,
        base_line=args.base_line,
        discovery_root=discovery_root,
        timeout_seconds=float(args.timeout_seconds),
        timeout_retry_tasks=timeout_retry_tasks,
        gpu_cleanup_events=gpu_cleanup_events,
    )
    summary_path = summary_output_path(output_dir, args.base_line)
    write_result_json(summary_path, summary)
    print(f"Summary saved to: {summary_path}")

    return 0 if summary["failure_count"] == 0 and summary["timeout_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
