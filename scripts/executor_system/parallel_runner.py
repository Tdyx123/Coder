#!/usr/bin/env python3
"""Run generated Python executables concurrently and collect metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


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
    PlanLoader,
    PlanValidator,
    ROBOT_BLOCKED,
    ROBOT_EXECUTING,
    ROBOT_FINISHED_STAGE,
    ROBOT_ACTION_SUCCESS,
    StagePlan,
    WorldState,
)
from executor_system.executor import Executor, PhaseCoordinator  # noqa: E402


DEFAULT_TIMEOUT_SECONDS = 100.0
IGNORED_FAILURE_ACTION_TYPES = {"Teleport", "TeleportObjectToHand"}


class PlanExecutionTimeout(TimeoutError):
    """Raised when the cooperative task-plan deadline is reached."""


def write_result_json(path: Path, result: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
        ignored = action.action_type in IGNORED_FAILURE_ACTION_TYPES
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
        blocked = False
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
                    self.state.status = ROBOT_BLOCKED
                    self.logger.result(tick, result)
                    blocked = True
                    break

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
            if not blocked:
                self.state.status = ROBOT_FINISHED_STAGE
            self.world_state.refresh([self.state])
            if self.phase_coordinator is not None and self.robot_id:
                self.phase_coordinator.mark_agent_done(
                    self.runtime.physical_agent_id(self.robot_id)
                )
        return self.world_state


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
        phase_coordinator = PhaseCoordinator(self.runtime, active_agent_ids)
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


def discover_executable_plans(
    explicit_paths: Sequence[str],
    root: Optional[str],
    py_dirs: Sequence[str] = (),
) -> List[Path]:
    candidates: List[Path] = []
    for raw_path in explicit_paths:
        candidates.append(Path(raw_path).expanduser())
    if root:
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
    return resolved


def result_path_for(output_dir: Path, executable_path: Path, index: int) -> Path:
    digest = hashlib.sha1(str(executable_path).encode("utf-8")).hexdigest()[:10]
    return output_dir / f"result_{index:04d}_{digest}.json"


def prune_stdout_for_result(
    result: Dict[str, Any],
    *,
    save_all_stdout: bool,
) -> Dict[str, Any]:
    if not save_all_stdout and not result.get("robot_failures"):
        result.pop("stdout", None)
    return result


def run_generated_executable(
    executable_path: Path,
    *,
    metrics_output: Path,
    result_output: Optional[Path],
    timeout_seconds: float,
    save_all_stdout: bool = False,
) -> Dict[str, Any]:
    start_time = time.monotonic()
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
            "failure_action_ratio": 0.0,
            "robot_failures": [],
            "returncode": 124,
            "executable_path": str(executable_path),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
        prune_stdout_for_result(result, save_all_stdout=save_all_stdout)
        if result_output is not None:
            write_result_json(result_output, result)
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
    result["returncode"] = completed.returncode
    result["executable_path"] = str(executable_path)
    result["stdout"] = completed.stdout
    result["stderr"] = completed.stderr
    prune_stdout_for_result(result, save_all_stdout=save_all_stdout)
    if result_output is not None:
        write_result_json(result_output, result)
    return result


def build_summary(results: Sequence[Dict[str, Any]], start_time: float) -> Dict[str, Any]:
    result_list = [dict(result) for result in results]
    return {
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
        "total_run_time_seconds": time.monotonic() - start_time,
        "results": result_list,
    }


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
        default="./parallel_runner_results",
        help="Directory for the summary and optional per-task result JSON files.",
    )
    parser.add_argument(
        "--write-individual-results",
        action="store_true",
        help="Write per-task result_*.json files in --output-dir.",
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

    try:
        executable_paths = discover_executable_plans(
            args.executable_plans,
            args.root,
            args.py_dir,
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

    with tempfile.TemporaryDirectory(prefix="parallel_runner_metrics_") as temp_dir:
        temp_metrics_dir = Path(temp_dir)
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            future_to_path = {}
            future_to_result_output: Dict[Any, Optional[Path]] = {}
            for index, executable_path in enumerate(executable_paths, start=1):
                individual_result_output = (
                    result_path_for(output_dir, executable_path, index)
                    if args.write_individual_results
                    else None
                )
                metrics_output = individual_result_output or (
                    temp_metrics_dir
                    / result_path_for(output_dir, executable_path, index).name
                )
                future = executor.submit(
                    run_generated_executable,
                    executable_path,
                    metrics_output=metrics_output,
                    result_output=individual_result_output,
                    timeout_seconds=float(args.timeout_seconds),
                    save_all_stdout=args.save_all_stdout,
                )
                future_to_path[future] = executable_path
                future_to_result_output[future] = individual_result_output

            for future in as_completed(future_to_path):
                executable_path = future_to_path[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "status": "failed",
                        "timed_out": False,
                        "run_time_seconds": 0.0,
                        "gcr": None,
                        "executed_actions": 0,
                        "failed_actions": 0,
                        "failure_action_ratio": 0.0,
                        "robot_failures": [],
                        "returncode": 1,
                        "executable_path": str(executable_path),
                        "error": str(exc),
                    }
                    prune_stdout_for_result(
                        result,
                        save_all_stdout=args.save_all_stdout,
                    )
                    result_output = future_to_result_output[future]
                    if result_output is not None:
                        write_result_json(result_output, result)
                results.append(result)
                status = result.get("status", "unknown")
                print(f"{status}: {executable_path}")

    results.sort(key=lambda result: str(result.get("executable_path", "")))
    summary = build_summary(results, start_time)
    write_result_json(output_dir / "parallel_runner_summary.json", summary)
    print(f"Summary saved to: {output_dir / 'parallel_runner_summary.json'}")

    return 0 if summary["failure_count"] == 0 and summary["timeout_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
