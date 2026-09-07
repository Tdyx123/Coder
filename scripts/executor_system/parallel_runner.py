#!/usr/bin/env python3
"""Run generated Python executables concurrently and collect metrics."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
import warnings
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
from executor_system.movement import MovementConfig, NavigationDeferred  # noqa: E402
from executor_system.runtime import is_pickup_object_clip_error  # noqa: E402
from executor_system.run_results import (  # noqa: E402
    ActionLedger,
    RunResultStore,
    ResultStorageError,
    atomic_write_json,
    normalize_output,
    task_key_for_executable,
    validate_result,
)
from executor_system.process_supervisor import (  # noqa: E402
    OwnedProcessScope,
    ProcessStartCancelled,
    ProcessOutcome,
    run_owned_process,
)
from baseline_converters import pddlrun  # noqa: E402


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_STARTUP_GRACE_SECONDS = 60.0
DEFAULT_FINALIZATION_GRACE_SECONDS = 10.0
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
MAX_TIMEOUT_RETRIES = 2
GPU_CLEANUP_PROCESS_SUFFIX = "0d69f666c7f282e54abfe58f1e917"
IGNORED_FAILURE_ACTION_TYPES = {"Teleport", "TeleportObjectToHand"}
BASE_LINE_CHOICES = ("LaMMA-P", "SMART-LLM", "Scale-Plan", "KGLAMP", "COT")


def finite_positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return seconds


def parent_timeout_seconds(
    startup_grace_seconds: float,
    execution_timeout_seconds: float,
    finalization_grace_seconds: float,
) -> float:
    total = (
        startup_grace_seconds
        + execution_timeout_seconds
        + finalization_grace_seconds
    )
    if not math.isfinite(total) or total <= 0:
        raise ValueError("aggregate parent timeout must be a finite positive number")
    return total


def effective_timeout_seconds(
    movement_mode: Optional[str],
    explicit_timeout: Optional[float],
) -> float:
    if explicit_timeout is not None:
        return float(explicit_timeout)
    resolved_mode = MovementConfig.resolve(movement_mode).mode.value
    return 120.0 if resolved_mode == "step" else DEFAULT_TIMEOUT_SECONDS


def failure_ignored_for_ratio(action: Action, exc: BaseException) -> bool:
    if action.action_type in IGNORED_FAILURE_ACTION_TYPES:
        return True
    return (
        action.action_type == "PickupObject"
        and is_pickup_object_clip_error(exc)
    )


from .execution_control import (
    PlanExecutionTimeout, ExecutionCancelled, install_control, run_workers,
    raise_if_execution_aborted,
)


def write_result_json(path: Path, result: Dict[str, Any]) -> None:
    atomic_write_json(path, result)


def action_success_rate(executed_actions: int, failed_actions: int) -> float:
    return (
        (executed_actions - failed_actions) / executed_actions
        if executed_actions
        else 1.0
    )


def normalize_result_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    result.pop("exec_rate", None)
    if "action_sr" not in result:
        executed_actions = int(result.get("executed_actions", 0) or 0)
        failed_actions = int(result.get("failed_actions", 0) or 0)
        result["action_sr"] = (
            action_success_rate(executed_actions, failed_actions)
            if executed_actions
            else None
        )
    return result


@dataclass
class TolerantRunStats:
    start_time: float = field(default_factory=time.monotonic)
    executed_actions: int = 0
    failed_actions: int = 0
    robot_failures: List[Dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    timeout_message: str = ""
    action_ledger: ActionLedger = field(default_factory=ActionLedger)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_started(self) -> None:
        with self._lock:
            self.executed_actions += 1

    def record_deferred(self) -> None:
        with self._lock:
            if self.executed_actions < 1:
                raise RuntimeError("cannot defer an action that was not started")
            self.executed_actions -= 1

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
        result = {
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
        result.update(self.action_ledger.freeze())
        return result


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
        stage_index: int = 0,
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
        self.stage_index = int(stage_index)
        self.deadline = deadline
        self.timeout_error_factory = PlanExecutionTimeout

    def execute(self) -> WorldState:
        if not self.robot_id:
            raise RuntimeError("Executor requires a robot_id to execute an action queue.")
        return self._execute_queue()

    def wait_for_condition(self, action: Action) -> None:
        if action.wait_until is None:
            return

        while True:
            self.control.check()
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
                self.control.check()
                action = self.state.next_action()
                if action is None:
                    break

                action_index = self.state.action_cursor
                ledger_key = f"{self.stage_index}:{self.robot_id}:{action_index}"
                self.world_state.tick = tick
                self.world_state.refresh([self.state])
                self.state.status = ROBOT_EXECUTING
                self.stats.record_started()
                self.stats.action_ledger.record_started(ledger_key)
                self.stats.action_ledger.record_attempt()
                action_wave = None
                try:
                    self.wait_for_condition(action)
                    self.control.check()
                    action_wave = self.before_action(action)
                    event = self.execute_action(action, action_wave=action_wave)
                    self.control.check()
                    self.record_temperature_goal_progress()
                except NavigationDeferred:
                    self.stats.record_deferred()
                    tick += 1
                    continue
                except (PlanExecutionTimeout, ExecutionCancelled):
                    raise
                except Exception as exc:
                    raise_if_execution_aborted(self.runtime, exc)
                    if self.phase_coordinator is not None and action_wave is not None:
                        self.phase_coordinator.abort_action_wave(
                            action_wave,
                            self.runtime.physical_agent_id(self.robot_id),
                            exc,
                        )
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
                self.stats.action_ledger.record_terminal(ledger_key, "succeeded")
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
        ledger_key: Optional[str] = None,
    ) -> bool:
        raise_if_execution_aborted(self.runtime, exc)
        self.control.check()
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
        self.stats.action_ledger.record_terminal(
            ledger_key or f"{self.stage_index}:{self.robot_id}:{action_index}",
            "failed",
            ignored_for_legacy=failure_ignored_for_ratio(action, exc),
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
        stage_index: int,
        logger: Optional[ExecutionLogger] = None,
    ) -> None:
        self.runtime = runtime_obj
        self.stats = stats
        self.deadline = deadline
        self.stage_index = int(stage_index)
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
                stage_index=self.stage_index,
                stats=self.stats,
                deadline=self.deadline,
                logger=self.logger,
                phase_coordinator=phase_coordinator,
            )
            for robot_id, actions in stage.robot_action_queues.items()
        ]

        run_workers(self.runtime, executors, phase_coordinator, stage.stage_id)

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

    control = install_control(runtime_obj, timeout_seconds)
    plan = PlanLoader().load(raw_plan)
    planned_keys = [
        f"{stage_index}:{robot_id}:{cursor}"
        for stage_index, stage in enumerate(plan.stages)
        for robot_id, actions in stage.robot_action_queues.items()
        for cursor, _action in enumerate(actions)
    ]
    stats = TolerantRunStats(action_ledger=ActionLedger(planned_keys))
    runtime_obj.action_ledger = stats.action_ledger
    deadline = control.deadline
    PlanValidator(runtime_obj).validate(plan)
    logger = logger or ExecutionLogger()

    try:
        for stage_index, stage in enumerate(plan.stages):
            control.check()
            runner = TolerantStageRunner(
                runtime_obj,
                stats=stats,
                deadline=deadline,
                stage_index=stage_index,
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
    finally:
        runtime_obj.execution_report = stats.to_dict()
        runtime_obj.action_metrics = {key: runtime_obj.execution_report[key] for key in
                                     ('action_counts', 'raw_action_sr', 'ignored_failure_count')}
    return runtime_obj.execution_report


def default_baseline_root(base_line: str) -> Path:
    return _REPO_ROOT / "baselines" / base_line


def baseline_summary_paths(base_line: str, root: Path) -> List[Path]:
    if base_line in {"LaMMA-P", "Scale-Plan", "KGLAMP", "COT"}:
        return [root / "plan_to_code_results" / "plan_to_code_results.json"]
    if base_line == "SMART-LLM":
        return [root / "plan_to_code_results.json"]
    raise RuntimeError(f"Unsupported base-line: {base_line}")


def baseline_fallback_search_root(base_line: str, root: Path) -> Path:
    if base_line in {"LaMMA-P", "Scale-Plan", "KGLAMP"}:
        preferred = root / "logs" / "intermediate_runs"
    elif base_line == "COT":
        return root / "parallel_runs"
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
    if base_line == "COT" and any(
        path.is_file() for path in baseline_summary_paths(base_line, discovery_root)
    ):
        return []
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
    """Reserve a daily sequence atomically before returning a valid summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    date_suffix = time.strftime("%m%d")
    stem = f"{base_line}_{date_suffix}" if base_line else date_suffix
    sequence = 1
    while True:
        path = output_dir / f"{stem}_{sequence:02d}.json"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            sequence += 1
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"run_status": "in_progress", "results": [], "total_results": 0},
                          handle, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path


def prune_stdout_for_result(result: Dict[str, Any], *, save_all_stdout: bool) -> Dict[str, Any]:
    successful = (result.get("status") == "success" and not result.get("robot_failures")
                  and result.get("execution_status", "completed") == "completed"
                  and result.get("task_success") is not False)
    if not save_all_stdout and successful:
        result.pop("stdout", None)
        path = result.pop("stdout_path", None)
        if path is not None:
            Path(path).unlink(missing_ok=True)
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
    """Deprecated compatibility entrypoint; owned process groups replace it."""

    warnings.warn(
        "cleanup_gpu_processes is deprecated; process groups are cleaned by owner",
        DeprecationWarning,
        stacklevel=2,
    )
    return {
        "round": round_index,
        "matched_pids": [],
        "killed_pids": [],
        "error": "deprecated no-op",
    }


def gpu_cleanup_error_event(round_index: int, exc: BaseException) -> Dict[str, Any]:
    return {
        "round": round_index,
        "matched_pids": [],
        "killed_pids": [],
        "error": str(exc),
    }


def failed_result_for_exception(
    executable_path: Path,
    exc: BaseException,
    movement_mode: str = "step",
) -> Dict[str, Any]:
    result = {
        "status": "failed",
        "timed_out": False,
        "run_time_seconds": 0.0,
        "gcr": None,
        "executed_actions": 0,
        "failed_actions": 0,
        "action_sr": None,
        "failure_action_ratio": 0.0,
        "robot_failures": [],
        "returncode": 1,
        "executable_path": str(executable_path),
        "movement_mode": str(movement_mode),
        "navigation_metrics": {},
        "error": str(exc),
    }
    return normalize_result_metrics(result)


def invalid_runner_result(
    executable_path: Path,
    *,
    movement_mode: str,
    run_time_seconds: float,
    returncode: int,
    identity: Dict[str, Any],
    error: str,
) -> Dict[str, Any]:
    """Represent absent, malformed, or untrusted child metrics conservatively."""

    return {
        "status": "failed",
        "process_status": "failed",
        "execution_status": "failed",
        "evaluation_status": "incomplete",
        "task_success": None,
        "evaluation_version": "legacy_v1",
        "execution_policy": "legacy",
        "run_time_seconds": run_time_seconds,
        "gcr": None,
        "tc": None,
        "sr": None,
        "ru": None,
        "executed_actions": 0,
        "failed_actions": 0,
        "action_sr": None,
        "failure_action_ratio": 0.0,
        "robot_failures": [],
        "returncode": returncode,
        "executable_path": str(executable_path),
        "movement_mode": str(movement_mode),
        "navigation_metrics": {},
        "error": error,
        **identity,
    }


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
        "action_sr": result.get("action_sr"),
        "failure_action_ratio": result.get("failure_action_ratio", 0.0),
    }


def _read_process_log(path: Path) -> str:
    try:
        return normalize_output(path.read_bytes())
    except OSError:
        return ""


def process_cleanup_event(outcome: ProcessOutcome) -> Dict[str, Any]:
    return {
        "pid": outcome.pid,
        "pgid": outcome.pgid,
        "timed_out": outcome.timed_out,
        "returncode": outcome.returncode,
        "termination_events": [dict(event) for event in outcome.termination_events],
    }


def run_generated_executable(
    executable_path: Path,
    *,
    metrics_output: Path,
    timeout_seconds: float,
    movement_mode: str = "step",
    save_all_stdout: bool = False,
    run_id: Optional[str] = None,
    task_key: Optional[str] = None,
    attempt: int = 1,
    startup_grace_seconds: float = DEFAULT_STARTUP_GRACE_SECONDS,
    finalization_grace_seconds: float = DEFAULT_FINALIZATION_GRACE_SECONDS,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    process_scope: Optional[OwnedProcessScope] = None,
) -> Dict[str, Any]:
    start_time = time.monotonic()
    child_env = os.environ.copy()
    identity = {
        "run_id": str(run_id or uuid.uuid4().hex),
        "task_key": task_key_for_executable(executable_path),
        "attempt": int(attempt),
    }
    child_env.update(
        {
            "LAMMAP_RUN_ID": identity["run_id"],
            "LAMMAP_TASK_KEY": identity["task_key"],
            "LAMMAP_ATTEMPT": str(identity["attempt"]),
        }
    )
    command = [
        sys.executable,
        str(executable_path),
        "--runner-mode",
        "--metrics-output",
        str(metrics_output),
        "--timeout-seconds",
        str(timeout_seconds),
        "--movement-mode",
        str(movement_mode),
    ]
    total_timeout_seconds = parent_timeout_seconds(
        startup_grace_seconds,
        timeout_seconds,
        finalization_grace_seconds,
    )
    stdout_path = (metrics_output.with_name("stdout.log") if metrics_output.name == "child_metrics.json"
                   else metrics_output.with_suffix(".stdout.log"))
    stderr_path = (metrics_output.with_name("stderr.log") if metrics_output.name == "child_metrics.json"
                   else metrics_output.with_suffix(".stderr.log"))
    try:
        outcome = run_owned_process(
            command,
            timeout_seconds=total_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=child_env,
            process_scope=process_scope,
        )
    except ProcessStartCancelled as exc:
        result = invalid_runner_result(
            executable_path,
            movement_mode=movement_mode,
            run_time_seconds=time.monotonic() - start_time,
            returncode=1,
            identity=identity,
            error=str(exc),
        )
        result.update(
            status="cancelled",
            process_status="cancelled",
            execution_status="cancelled",
        )
        return result
    except OSError as exc:
        return invalid_runner_result(
            executable_path,
            movement_mode=movement_mode,
            run_time_seconds=time.monotonic() - start_time,
            returncode=1,
            identity=identity,
            error=f"could not start runner process: {exc}",
        )

    stdout = _read_process_log(stdout_path)
    stderr = _read_process_log(stderr_path)
    cleanup_events = [process_cleanup_event(outcome)]
    if outcome.timed_out:
        result = {
            "status": "timeout",
            "timed_out": True,
            "timeout_message": (
                f"subprocess exceeded total budget {total_timeout_seconds:g} seconds "
                f"(startup {startup_grace_seconds:g}, execution {timeout_seconds:g}, "
                f"finalization {finalization_grace_seconds:g})"
            ),
            "run_time_seconds": outcome.wall_time_seconds,
            "gcr": None,
            "tc": None,
            "sr": None,
            "ru": None,
            "satisfied_goal_count": None,
            "executed_actions": 0,
            "failed_actions": 0,
            "action_sr": None,
            "failure_action_ratio": 0.0,
            "robot_failures": [],
            "returncode": 124,
            "executable_path": str(executable_path),
            "movement_mode": str(movement_mode),
            "navigation_metrics": {},
            "stdout": stdout,
            "stderr": stderr,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "process_cleanup_events": cleanup_events,
            "process_status": "timeout",
            "execution_status": "timeout",
            "evaluation_status": "incomplete",
            "task_success": None,
            "evaluation_version": "legacy_v1",
            "execution_policy": "legacy",
            **identity,
        }
        prune_stdout_for_result(result, save_all_stdout=save_all_stdout)
        return result

    metrics_error = ""
    if metrics_output.is_file():
        try:
            result = json.loads(metrics_output.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            result = {}
            metrics_error = f"invalid runner metrics: {exc}"
    else:
        result = {}
        metrics_error = "runner metrics file was not created"

    if not isinstance(result, dict) or not result:
        result = invalid_runner_result(
            executable_path,
            movement_mode=movement_mode,
            run_time_seconds=outcome.wall_time_seconds,
            returncode=int(outcome.returncode if outcome.returncode is not None else 1),
            identity=identity,
            error=metrics_error or "runner metrics must be a non-empty JSON object",
        )
    else:
        try:
            result = validate_result(
                result,
                returncode=int(outcome.returncode if outcome.returncode is not None else 1),
                expected_identity=identity,
            )
        except ValueError as exc:
            result = invalid_runner_result(
                executable_path,
                movement_mode=movement_mode,
                run_time_seconds=outcome.wall_time_seconds,
                returncode=int(outcome.returncode if outcome.returncode is not None else 1),
                identity=identity,
                error=f"invalid runner metrics: {exc}",
            )
    result.setdefault(
        "status",
        "success" if result.get("process_status") == "completed" else "failed",
    )
    result.setdefault("timed_out", False)
    result.setdefault("run_time_seconds", outcome.wall_time_seconds)
    result.setdefault("gcr", None)
    result.setdefault("executed_actions", 0)
    result.setdefault("failed_actions", 0)
    result.setdefault("failure_action_ratio", 0.0)
    result.setdefault("robot_failures", [])
    result.setdefault("movement_mode", str(movement_mode))
    result.setdefault("navigation_metrics", {})
    normalize_result_metrics(result)
    result["returncode"] = outcome.returncode
    result["executable_path"] = str(executable_path)
    result["stdout"] = stdout
    result["stderr"] = stderr
    result["stdout_path"] = str(stdout_path)
    result["stderr_path"] = str(stderr_path)
    result["process_cleanup_events"] = cleanup_events
    prune_stdout_for_result(result, save_all_stdout=save_all_stdout)
    return result


def _run_and_record(executable_path: Path, *, result_store: Optional[RunResultStore], **kwargs) -> Dict[str, Any]:
    """Persist inside the worker, independently of as_completed/parent interrupts."""
    try:
        result = run_generated_executable(executable_path, **kwargs)
    except Exception as exc:
        result = failed_result_for_exception(executable_path, exc, kwargs["movement_mode"])
        result.update(process_status="failed", execution_status="failed", evaluation_status="incomplete",
                      task_success=None, tc=None, sr=None, ru=None)
    identity = dict(run_id=kwargs["run_id"], task_key=kwargs["task_key"], attempt=kwargs["attempt"])
    # This wrapper also labels failures raised before a child was started.
    for key, value in identity.items():
        result.setdefault(key, value)
    if result_store is not None:
        try:
            result_store.record_attempt(identity["task_key"], identity["attempt"], result)
        except Exception as exc:
            raise ResultStorageError(f"could not persist attempt for {executable_path}: {exc}") from exc
    return result


def run_executable_round(
    *,
    executor: ThreadPoolExecutor,
    executable_paths: Sequence[Path],
    temp_metrics_dir: Path,
    round_index: int,
    timeout_seconds: float,
    movement_mode: str,
    save_all_stdout: bool,
    run_id: str,
    startup_grace_seconds: float,
    finalization_grace_seconds: float,
    termination_grace_seconds: float,
    process_scope: OwnedProcessScope,
    submitted_futures: Optional[List[Any]] = None,
    completed_results: Optional[Dict[Path, Dict[str, Any]]] = None,
    result_store: Optional[RunResultStore] = None,
) -> Dict[Path, Dict[str, Any]]:
    future_to_path = {}
    for index, executable_path in enumerate(executable_paths, start=1):
        task_key = task_key_for_executable(executable_path)
        if result_store is not None:
            metrics_output = result_store.start_attempt(task_key, round_index + 1) / "child_metrics.json"
        else:
            metrics_output = temp_metrics_dir / f"metrics_round_{round_index:02d}_{index:04d}.json"
        future = executor.submit(
            _run_and_record,
            executable_path,
            result_store=result_store,
            metrics_output=metrics_output,
            timeout_seconds=timeout_seconds,
            movement_mode=movement_mode,
            save_all_stdout=save_all_stdout,
            run_id=run_id,
            task_key=task_key_for_executable(executable_path),
            attempt=round_index + 1,
            startup_grace_seconds=startup_grace_seconds,
            finalization_grace_seconds=finalization_grace_seconds,
            termination_grace_seconds=termination_grace_seconds,
            process_scope=process_scope,
        )
        future_to_path[future] = executable_path
        if submitted_futures is not None:
            submitted_futures.append(future)

    round_results: Dict[Path, Dict[str, Any]] = {}
    try:
        for future in as_completed(future_to_path):
            executable_path = future_to_path[future]
            try:
                result = future.result()
            except ResultStorageError:
                raise
            except Exception as exc:
                result = failed_result_for_exception(
                    executable_path,
                    exc,
                    movement_mode,
                )
                prune_stdout_for_result(
                    result,
                    save_all_stdout=save_all_stdout,
                )
            round_results[executable_path] = result
            if completed_results is not None:
                completed_results[executable_path] = dict(result)
            status = result.get("status", "unknown")
            print(f"{status}: {executable_path}")
    finally:
        if completed_results is not None:
            for future, executable_path in future_to_path.items():
                if executable_path in round_results or not future.done():
                    continue
                try:
                    completed_results[executable_path] = dict(future.result())
                except Exception as exc:
                    completed_results[executable_path] = failed_result_for_exception(
                        executable_path,
                        exc,
                        movement_mode,
                    )
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
    movement_mode: str = "step",
    save_all_stdout: bool,
    run_id: Optional[str] = None,
    startup_grace_seconds: float = DEFAULT_STARTUP_GRACE_SECONDS,
    finalization_grace_seconds: float = DEFAULT_FINALIZATION_GRACE_SECONDS,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    completed_results: Optional[Dict[Path, Dict[str, Any]]] = None,
    process_scope: Optional[OwnedProcessScope] = None,
    result_store: Optional[RunResultStore] = None,
) -> tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    resolved_run_id = str(run_id or uuid.uuid4().hex)
    attempts_by_path: Dict[Path, List[Dict[str, Any]]] = {
        path: [] for path in executable_paths
    }
    final_results_by_path: Dict[Path, Dict[str, Any]] = {}
    pending_paths = list(executable_paths)
    process_cleanup_events: List[Dict[str, Any]] = []
    scope = process_scope or OwnedProcessScope()
    submitted_futures = []

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        for round_index in range(MAX_TIMEOUT_RETRIES + 1):
            if not pending_paths:
                break
            round_results = run_executable_round(
                executor=executor,
                executable_paths=pending_paths,
                temp_metrics_dir=temp_metrics_dir,
                round_index=round_index,
                timeout_seconds=timeout_seconds,
                movement_mode=movement_mode,
                save_all_stdout=save_all_stdout,
                run_id=resolved_run_id,
                startup_grace_seconds=startup_grace_seconds,
                finalization_grace_seconds=finalization_grace_seconds,
                termination_grace_seconds=termination_grace_seconds,
                completed_results=completed_results,
                process_scope=scope,
                submitted_futures=submitted_futures,
                result_store=result_store,
            )

            next_pending_paths: List[Path] = []
            attempt_number = round_index + 1
            for executable_path in pending_paths:
                result = round_results[executable_path]
                normalize_result_metrics(result)
                attempts_by_path[executable_path].append(
                    compact_attempt_result(result, attempt_number)
                )
                final_results_by_path[executable_path] = result
                process_cleanup_events.extend(result.get("process_cleanup_events", []))
                if result.get("timed_out"):
                    next_pending_paths.append(executable_path)

            if round_index >= MAX_TIMEOUT_RETRIES:
                break
            pending_paths = next_pending_paths
    finally:
        # Cancel work which has not reached a worker before atomically closing
        # registration and snapshotting all already-created process groups.
        for future in submitted_futures:
            future.cancel()
        process_cleanup_events.extend(
            scope.cleanup(
                termination_grace_seconds,
                reason="parallel-runner-interrupted",
            )
        )
        executor.shutdown(wait=True)
        # Storage failures remain fatal even when iteration was interrupted
        # before the failed future could be consumed.
        for future in submitted_futures:
            if not future.cancelled():
                failure = future.exception()
                if isinstance(failure, ResultStorageError):
                    raise failure

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
    return results, timeout_retry_tasks, process_cleanup_events


def build_summary(
    results: Sequence[Dict[str, Any]],
    start_time: float,
    *,
    base_line: Optional[str] = None,
    discovery_root: Optional[Path] = None,
    timeout_seconds: Optional[float] = None,
    movement_mode: str = "step",
    timeout_retry_tasks: Optional[Iterable[str]] = None,
    gpu_cleanup_events: Optional[Sequence[Dict[str, Any]]] = None,
    process_cleanup_events: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    resolved_timeout_seconds = effective_timeout_seconds(
        movement_mode,
        timeout_seconds,
    )
    result_list = [normalize_result_metrics(dict(result)) for result in results]
    grouped_results: Dict[tuple, Dict[str, Any]] = {}
    for result in result_list:
        key = (
            result.get("metrics_schema_version", 1),
            result.get("evaluation_version", "legacy_v1"),
            result.get("execution_policy", "legacy"),
            result.get("movement_mode", str(movement_mode)),
        )
        group = grouped_results.setdefault(
            key,
            {
                "metrics_schema_version": key[0],
                "evaluation_version": key[1],
                "execution_policy": key[2],
                "movement_mode": key[3],
                "total_task_count": 0,
                "valid_evaluation_count": 0,
                "task_success_count": 0,
            },
        )
        group["total_task_count"] += 1
        if result.get("evaluation_status") == "valid":
            group["valid_evaluation_count"] += 1
            if result.get("task_success") is True:
                group["task_success_count"] += 1

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
            "timeout_seconds": resolved_timeout_seconds,
        },
        "movement_mode": str(movement_mode),
        "effective_timeout_seconds": resolved_timeout_seconds,
        "timeout_retry_tasks": list(timeout_retry_tasks or []),
        "gpu_cleanup_events": [
            dict(event) for event in (gpu_cleanup_events or [])
        ],
        "process_cleanup_events": [
            dict(event) for event in (process_cleanup_events or [])
        ],
        "total_run_time_seconds": time.monotonic() - start_time,
        "results": result_list,
        "result_groups": [
            grouped_results[key] for key in sorted(grouped_results, key=repr)
        ],
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
            f"Supported values: {', '.join(BASE_LINE_CHOICES)}."
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
        type=finite_positive_seconds,
        default=None,
        help="Per-executable timeout; defaults to 30s for teleport and 120s for step.",
    )
    parser.add_argument(
        "--startup-grace-seconds",
        type=finite_positive_seconds,
        default=DEFAULT_STARTUP_GRACE_SECONDS,
        help="Startup allowance added to each parent process budget.",
    )
    parser.add_argument(
        "--finalization-grace-seconds",
        type=finite_positive_seconds,
        default=DEFAULT_FINALIZATION_GRACE_SECONDS,
        help="Finalization allowance added to each parent process budget.",
    )
    parser.add_argument(
        "--termination-grace-seconds",
        type=finite_positive_seconds,
        default=DEFAULT_TERMINATION_GRACE_SECONDS,
        help="Maximum TERM/KILL cleanup window for an owned process group.",
    )
    parser.add_argument(
        "--movement-mode",
        choices=("teleport", "step"),
        default=None,
        help="Robot movement mode; otherwise LAMMAP_MOVEMENT_MODE or step.",
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
    parser.add_argument("--rebuild-summary", metavar="RUN_DIR",
                        help="Rebuild a summary from completed durable attempts without running tasks.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    if args.rebuild_summary:
        run_dir = Path(args.rebuild_summary).expanduser().resolve()
        if not run_dir.is_dir() or run_dir.parent.name != "runs":
            print("ERROR: --rebuild-summary requires an existing output_dir/runs/RUN_ID directory")
            return 1
        try:
            store = RunResultStore(run_dir.parent.parent, run_dir.name)
            store.summary_path = summary_output_path(store.output_dir)
            summary = store.write_summary("rebuilt")
            print(f"Summary rebuilt from {run_dir}: {store.summary_path}")
            return 0
        except (OSError, ValueError) as exc:
            print(f"ERROR: could not rebuild summary: {exc}")
            return 1
    try:
        movement_mode = MovementConfig.resolve(args.movement_mode).mode.value
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    timeout_seconds = effective_timeout_seconds(
        movement_mode,
        args.timeout_seconds,
    )
    if args.max_workers < 1:
        print("ERROR: --max-workers must be at least 1")
        return 1
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        print("ERROR: --timeout-seconds must be a finite positive number")
        return 1
    try:
        parent_timeout_seconds(
            args.startup_grace_seconds,
            timeout_seconds,
            args.finalization_grace_seconds,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
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
    start_time = time.monotonic()
    run_id = uuid.uuid4().hex
    store = RunResultStore(output_dir, run_id)
    discovery_root = None
    if args.base_line:
        discovery_root = (Path(args.root).expanduser() if args.root
                          else default_baseline_root(args.base_line)).resolve()
    store.summary_metadata = build_summary([], start_time, base_line=args.base_line,
        discovery_root=discovery_root, timeout_seconds=timeout_seconds, movement_mode=movement_mode)
    completed_results: Dict[Path, Dict[str, Any]] = {}
    try:
        store.summary_path = summary_output_path(output_dir, args.base_line)
        store.write_summary("in_progress")
        results, timeout_retry_tasks, process_cleanup_events = run_executables_with_retries(
            executable_paths,
            max_workers=args.max_workers,
            temp_metrics_dir=store.run_dir,
            timeout_seconds=timeout_seconds,
            movement_mode=movement_mode,
            save_all_stdout=args.save_all_stdout,
            run_id=run_id,
            result_store=store,
            startup_grace_seconds=args.startup_grace_seconds,
            finalization_grace_seconds=args.finalization_grace_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
            completed_results=completed_results,
        )
        store.summary_metadata = build_summary(results, start_time, base_line=args.base_line,
            discovery_root=discovery_root, timeout_seconds=timeout_seconds, movement_mode=movement_mode,
            timeout_retry_tasks=timeout_retry_tasks, process_cleanup_events=process_cleanup_events)
        summary = store.write_summary("completed")
    except (KeyboardInterrupt, SystemExit):
        try:
            store.write_summary("interrupted")
        except (OSError, ValueError) as exc:
            print(f"ERROR: could not save interrupted summary: {exc}")
        raise
    except (OSError, ValueError, ResultStorageError) as exc:
        print(f"ERROR: result storage failed: {exc}")
        try:
            store.write_summary("failed", storage_error=str(exc))
        except (OSError, ValueError) as summary_exc:
            print(f"ERROR: could not save failure summary: {summary_exc}")
        return 1
    print(f"Summary saved to: {store.summary_path}")
    return 0 if summary["failure_count"] == 0 and summary["timeout_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
