import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, TextIO, Tuple


RUN_GLOB = "pddlrun_llmseparate_*"
FLOOR_DIRECTORY_PATTERN = re.compile(r"FloorPlan([0-9]+)")
DATASET_FILE_PATTERN = re.compile(r"FloorPlan([0-9]+)\.jsonl")
RUNNER = "scripts/run_pddlrun_llmseparate_parallel.py"


class RunEvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class CompletionPlan:
    run_dir: Path
    test_set: str
    model: str
    allocate_model: str
    completed_floors: Tuple[int, ...]
    target_floors: Tuple[int, ...]
    missing_floors: Tuple[int, ...]


@dataclass(frozen=True)
class RunError:
    run_dir: Path
    message: str


@dataclass(frozen=True)
class ScanResult:
    plans: Tuple[CompletionPlan, ...]
    errors: Tuple[RunError, ...]


def default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _required_text(record: Dict[str, object], field: str, source: Path) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RunEvaluationError(f"missing {field} metadata in {source}")
    return value


def _load_json_object(path: Path, description: str) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunEvaluationError(f"cannot read {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunEvaluationError(f"{description} is not a JSON object: {path}")
    return value


def _parseable_floor_summaries(run_dir: Path) -> List[Tuple[int, Dict[str, object]]]:
    summaries: List[Tuple[int, Dict[str, object]]] = []
    seen_floors = set()
    for child in sorted(run_dir.iterdir(), key=lambda path: path.name):
        match = FLOOR_DIRECTORY_PATTERN.fullmatch(child.name)
        if match is None or not child.is_dir():
            continue
        summary_path = child / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(summary, dict):
            continue
        floor = int(match.group(1))
        if floor in seen_floors:
            raise RunEvaluationError(f"duplicate completed FloorPlan{floor} summaries")
        seen_floors.add(floor)
        summaries.append((floor, summary))
    summaries.sort(key=lambda item: item[0])
    return summaries


def _resolve_task_run_dir(repo_root: Path, raw_path: str) -> Path:
    task_run_dir = Path(raw_path)
    if not task_run_dir.is_absolute():
        task_run_dir = repo_root / task_run_dir
    return task_run_dir


def _metadata_from_summaries(
    repo_root: Path,
    summaries: Sequence[Tuple[int, Dict[str, object]]],
) -> Tuple[str, str, str]:
    test_sets = set()
    models = set()
    allocate_models = set()
    referenced_manifest_count = 0

    for floor, summary in summaries:
        results = summary.get("results")
        if not isinstance(results, list):
            raise RunEvaluationError(
                f"FloorPlan{floor} summary has no results list for metadata evaluation"
            )
        for result_index, result in enumerate(results):
            source = Path(f"FloorPlan{floor}/summary.json results[{result_index}]")
            if not isinstance(result, dict):
                raise RunEvaluationError(f"result metadata is not an object in {source}")
            result_model = _required_text(result, "model", source)
            result_allocate_model = _required_text(result, "allocate_model", source)
            models.add(result_model)
            allocate_models.add(result_allocate_model)

            raw_task_run_dir = result.get("task_run_dir")
            if raw_task_run_dir is None:
                continue
            if not isinstance(raw_task_run_dir, str) or not raw_task_run_dir.strip():
                raise RunEvaluationError(f"invalid task_run_dir metadata in {source}")

            manifest_path = (
                _resolve_task_run_dir(repo_root, raw_task_run_dir) / "run_manifest.json"
            )
            manifest = _load_json_object(manifest_path, "task run_manifest.json")
            manifest_test_set = _required_text(manifest, "test_set", manifest_path)
            manifest_model = _required_text(manifest, "model", manifest_path)
            manifest_allocate_model = _required_text(
                manifest, "allocate_model", manifest_path
            )
            if result_model != manifest_model:
                raise RunEvaluationError(
                    f"model conflict between {source} and {manifest_path}"
                )
            if result_allocate_model != manifest_allocate_model:
                raise RunEvaluationError(
                    f"allocate_model conflict between {source} and {manifest_path}"
                )
            test_sets.add(manifest_test_set)
            models.add(manifest_model)
            allocate_models.add(manifest_allocate_model)
            referenced_manifest_count += 1

    if referenced_manifest_count == 0:
        raise RunEvaluationError("missing referenced task run_manifest.json metadata")
    if len(test_sets) != 1:
        raise RunEvaluationError("conflicting test_set metadata across task manifests")
    if len(models) != 1:
        raise RunEvaluationError("conflicting model metadata across floor results")
    if len(allocate_models) != 1:
        raise RunEvaluationError(
            "conflicting allocate_model metadata across floor results"
        )
    return next(iter(test_sets)), next(iter(models)), next(iter(allocate_models))


def _target_floors(repo_root: Path, test_set: str) -> Tuple[int, ...]:
    dataset_dir = repo_root / "data" / test_set
    if not dataset_dir.is_dir():
        raise RunEvaluationError(f"dataset directory not found: {dataset_dir}")
    floors = {
        int(match.group(1))
        for path in dataset_dir.iterdir()
        if path.is_file()
        for match in [DATASET_FILE_PATTERN.fullmatch(path.name)]
        if match is not None
    }
    if not floors:
        raise RunEvaluationError(
            f"dataset has no exact FloorPlan<number>.jsonl files: {dataset_dir}"
        )
    return tuple(sorted(floors))


def evaluate_run(repo_root: Path, run_dir: Path) -> Optional[CompletionPlan]:
    summaries = _parseable_floor_summaries(run_dir)
    if not summaries:
        raise RunEvaluationError("no parseable numeric floor summaries")

    test_set, model, allocate_model = _metadata_from_summaries(repo_root, summaries)
    completed_floors = tuple(floor for floor, _summary in summaries)
    target_floors = _target_floors(repo_root, test_set)
    completed_set = set(completed_floors)
    target_set = set(target_floors)
    outside_target = sorted(completed_set - target_set)
    if outside_target:
        labels = ", ".join(f"FloorPlan{floor}" for floor in outside_target)
        raise RunEvaluationError(f"completed floors outside target dataset: {labels}")

    missing_floors = tuple(sorted(target_set - completed_set))
    if not missing_floors or len(completed_floors) * 2 <= len(target_floors):
        return None
    return CompletionPlan(
        run_dir=run_dir,
        test_set=test_set,
        model=model,
        allocate_model=allocate_model,
        completed_floors=completed_floors,
        target_floors=target_floors,
        missing_floors=missing_floors,
    )


def scan_repository(repo_root: Path) -> ScanResult:
    repo_root = Path(repo_root).resolve()
    parallel_runs_dir = repo_root / "parallel_runs"
    if not parallel_runs_dir.is_dir():
        return ScanResult(plans=(), errors=())

    plans: List[CompletionPlan] = []
    errors: List[RunError] = []
    for run_dir in sorted(parallel_runs_dir.glob(RUN_GLOB), key=lambda path: path.name):
        if not run_dir.is_dir() or "_v2_" in run_dir.name:
            continue
        try:
            if not any(run_dir.iterdir()) or (run_dir / "summary.json").is_file():
                continue
            plan = evaluate_run(repo_root, run_dir)
        except (OSError, RunEvaluationError) as exc:
            errors.append(RunError(run_dir=run_dir, message=str(exc)))
            continue
        if plan is not None:
            plans.append(plan)
    return ScanResult(plans=tuple(plans), errors=tuple(errors))


def command_argv(repo_root: Path, plan: CompletionPlan) -> List[str]:
    argv = [
        "python",
        str(Path(repo_root).resolve() / RUNNER),
        "--floor-plans",
        *(str(floor) for floor in plan.missing_floors),
        "--model",
        plan.model,
    ]
    if plan.allocate_model != plan.model:
        argv.extend(["--allocate-model", plan.allocate_model])
    argv.extend(
        [
            "--test-set",
            plan.test_set,
            "--output-root",
            str(plan.run_dir),
            "--merge-existing-floor-summaries",
        ]
    )
    return argv


def run(
    repo_root: Path,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    repo_root = Path(repo_root).resolve()
    result = scan_repository(repo_root)
    for plan in result.plans:
        completed_count = len(plan.completed_floors)
        target_count = len(plan.target_floors)
        missing = ", ".join(str(floor) for floor in plan.missing_floors)
        stdout.write(
            f"# {plan.run_dir.name}: {completed_count}/{target_count} floors complete; "
            f"missing {missing}\n"
        )
        stdout.write(f"{shlex.join(command_argv(repo_root, plan))}\n")
    for error in result.errors:
        stderr.write(f"ERROR {error.run_dir.name}: {error.message}\n")
    return 1 if result.errors else 0


def main() -> int:
    return run(default_repo_root())


if __name__ == "__main__":
    raise SystemExit(main())
