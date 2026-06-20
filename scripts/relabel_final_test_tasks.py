import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
for path in (_SCRIPT_DIR, _REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

import data_engine
from resources.robots import robots


DEFAULT_TARGET_DIRS = (
    "final_test_new_0",
    "final_test_new_1",
    "final_test_new_0521_0",
    "final_test_new_0521_1",
    "final_test_new_0525_1",
    "final_test_new_0526_1",
    "final_test_new_0527_1",
    "final_test_new_0528_1",
    "final_test_new_0530_1",
    "final_test_new_0609_1",
    "final_test_new_0610_1",
)

FLOOR_PLAN_FILE_RE = re.compile(r"^FloorPlan(\d+)\.jsonl$")
SkillSets = Dict[str, Any]
SkillSetLoader = Callable[[int], SkillSets]
FloorObjectsLoader = Callable[[int], List[Dict[str, Any]]]
ObjectMassLoader = Callable[[int], Dict[str, float]]


@dataclass
class InvalidDetail:
    line_no: int
    reason: str
    skill: Optional[str] = None
    objects: Optional[List[Any]] = None

    def format(self, path: Path) -> str:
        detail = f"{path}:{self.line_no}: {self.reason}"
        if self.skill is not None:
            detail += f" [{self.skill}"
            if self.objects is not None:
                detail += f" {self.objects}"
            detail += "]"
        return detail


@dataclass
class FileRelabelStats:
    path: Path
    rows: int = 0
    valid: int = 0
    invalid: int = 0
    changed: bool = False
    invalid_added: int = 0
    invalid_removed: int = 0
    pre_task_changed: int = 0
    invalid_details: List[InvalidDetail] = field(default_factory=list)


@dataclass
class RelabelStats:
    files: int = 0
    rows: int = 0
    valid: int = 0
    invalid: int = 0
    changed_files: int = 0
    invalid_added: int = 0
    invalid_removed: int = 0
    pre_task_changed: int = 0
    invalid_details: List[InvalidDetail] = field(default_factory=list)

    def add_file(self, file_stats: FileRelabelStats) -> None:
        self.files += 1
        self.rows += file_stats.rows
        self.valid += file_stats.valid
        self.invalid += file_stats.invalid
        if file_stats.changed:
            self.changed_files += 1
        self.invalid_added += file_stats.invalid_added
        self.invalid_removed += file_stats.invalid_removed
        self.pre_task_changed += file_stats.pre_task_changed
        self.invalid_details.extend(file_stats.invalid_details)


@dataclass
class RelabelContext:
    skill_set_loader: SkillSetLoader = data_engine._build_object_skill_sets
    floor_objects_loader: FloorObjectsLoader = data_engine._load_floor_objects_for_pre_task_actions
    object_mass_loader: ObjectMassLoader = lambda floor_plan: load_object_mass_map(floor_plan)
    bad_subtask_rules: Optional[Sequence[data_engine.BadSubtaskRule]] = None
    no_valid_position_rules: Optional[set] = None
    robot_defs: Sequence[Dict[str, Any]] = field(default_factory=lambda: robots)

    def __post_init__(self) -> None:
        self._skill_set_cache: Dict[int, SkillSets] = {}
        self._floor_objects_cache: Dict[int, List[Dict[str, Any]]] = {}
        self._mass_map_cache: Dict[int, Dict[str, float]] = {}
        if self.bad_subtask_rules is None:
            self.bad_subtask_rules = data_engine.load_bad_subtask_rules()
        if self.no_valid_position_rules is None:
            self.no_valid_position_rules = data_engine.load_no_valid_position_rules()
        self._engine = data_engine.DataEngine.__new__(data_engine.DataEngine)

    def skill_sets(self, floor_plan: int) -> SkillSets:
        if floor_plan not in self._skill_set_cache:
            self._skill_set_cache[floor_plan] = self.skill_set_loader(floor_plan)
        return self._skill_set_cache[floor_plan]

    def floor_objects(self, floor_plan: int) -> List[Dict[str, Any]]:
        if floor_plan not in self._floor_objects_cache:
            self._floor_objects_cache[floor_plan] = self.floor_objects_loader(floor_plan)
        return self._floor_objects_cache[floor_plan]

    def mass_map(self, floor_plan: int) -> Dict[str, float]:
        if floor_plan not in self._mass_map_cache:
            self._mass_map_cache[floor_plan] = self.object_mass_loader(floor_plan)
        return self._mass_map_cache[floor_plan]

    def all_objects(self, floor_plan: int) -> List[str]:
        return list(self.mass_map(floor_plan))

    def check_subtasks(self, subtasks: List[Dict[str, Any]], skill_sets: SkillSets) -> bool:
        return self._engine.check_subtasks(subtasks, skill_sets)

    def robot_can_complete_subtask(
        self,
        robot: Dict[str, Any],
        subtask: Dict[str, Any],
        obj_mass_map: Dict[str, float],
    ) -> bool:
        return self._engine._robot_can_complete_subtask(robot, subtask, obj_mass_map)


def floor_plan_from_path(path: Path) -> int:
    match = FLOOR_PLAN_FILE_RE.match(path.name)
    if match is None:
        raise ValueError(f"Expected a FloorPlan*.jsonl file, got: {path}")
    return int(match.group(1))


def discover_task_files(data_dir: Path, target_dirs: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    for target_dir in target_dirs:
        directory = data_dir / target_dir
        if not directory.is_dir():
            raise FileNotFoundError(f"Target directory not found: {directory}")
        files.extend(sorted(directory.glob("FloorPlan*.jsonl")))
    return files


def load_object_mass_map(
    floor_plan: int,
    cache_dir: Path = data_engine._REPO_ROOT / "data" / "ai2thor_objects_cache",
) -> Dict[str, float]:
    cache_path = cache_dir / f"FloorPlan{floor_plan}.json"
    if not cache_path.is_file():
        raise FileNotFoundError(f"AI2-THOR object cache not found: {cache_path}")

    try:
        raw_objects = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid object cache JSON: {cache_path}") from exc

    if not isinstance(raw_objects, list):
        raise ValueError(f"Object cache must be a list: {cache_path}")

    mass_map: Dict[str, float] = {}
    for index, item in enumerate(raw_objects):
        if not isinstance(item, dict):
            raise ValueError(f"Object cache entry #{index} must be an object: {cache_path}")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            mass_map[name] = float(item.get("mass", 0.0))
        except (TypeError, ValueError):
            mass_map[name] = 0.0

    if not mass_map:
        raise ValueError(f"Object cache has no named objects: {cache_path}")
    return mass_map


def _invalid_detail(
    reason: str,
    subtask: Optional[Dict[str, Any]] = None,
) -> InvalidDetail:
    if isinstance(subtask, dict):
        objects = subtask.get("objects")
        return InvalidDetail(
            0,
            reason,
            subtask.get("skill") if isinstance(subtask.get("skill"), str) else None,
            objects if isinstance(objects, list) else None,
        )
    return InvalidDetail(0, reason)


def validate_subtask(
    subtask: Any,
    floor_plan: int,
    context: RelabelContext,
) -> List[InvalidDetail]:
    if not isinstance(subtask, dict):
        return [InvalidDetail(0, "subtask is not an object")]

    skill = subtask.get("skill")
    config = data_engine.SKILL_CONFIGS.get(skill)
    if config is None:
        return [_invalid_detail("unknown or unsupported skill", subtask)]

    objects = subtask.get("objects")
    if not isinstance(objects, list) or len(objects) != config.arity:
        return [_invalid_detail("subtask objects do not match skill arity", subtask)]
    if not all(isinstance(obj, str) for obj in objects):
        return [_invalid_detail("subtask objects must be strings", subtask)]

    skill_sets = context.skill_sets(floor_plan)
    all_objects = context.all_objects(floor_plan)
    invalid_details: List[InvalidDetail] = []

    if not data_engine._skill_can_generate(config, all_objects, skill_sets):
        invalid_details.append(_invalid_detail("skill cannot generate in floor plan", subtask))

    if config.arity == 1:
        if config.primary_set is not None and objects[0] not in skill_sets.get(config.primary_set, []):
            invalid_details.append(_invalid_detail("object is not valid for skill", subtask))
    elif not data_engine._can_match_skill_pair(
        objects[0],
        objects[1],
        config,
        skill_sets,
        all_objects,
    ):
        invalid_details.append(_invalid_detail("object pair is not valid for skill", subtask))

    if data_engine.subtask_matches_bad_subtask_rule(
        floor_plan,
        subtask,
        context.bad_subtask_rules or [],
    ):
        invalid_details.append(_invalid_detail("matches bad subtask rule", subtask))

    if data_engine.subtask_matches_no_valid_position_rule(
        floor_plan,
        subtask,
        context.no_valid_position_rules or set(),
        skill_sets,
    ):
        invalid_details.append(_invalid_detail("matches no-valid-position rule", subtask))

    if not data_engine._subtask_uses_pickupable_objects(subtask, skill_sets):
        invalid_details.append(_invalid_detail("required pickup object is not pickupable", subtask))

    return invalid_details


def _is_robot_index(value: Any, robot_count: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= robot_count
    )


def validate_robot_assignments(
    record: Dict[str, Any],
    subtasks: List[Dict[str, Any]],
    floor_plan: int,
    context: RelabelContext,
) -> List[InvalidDetail]:
    invalid_details: List[InvalidDetail] = []
    robot_list = record.get("robot list")
    assigned_robots = record.get("assigned_robots")
    robot_count = len(context.robot_defs)

    if not isinstance(robot_list, list):
        return [InvalidDetail(0, "missing or invalid robot list")]
    if not isinstance(assigned_robots, list):
        return [InvalidDetail(0, "missing or invalid assigned_robots")]

    if not 2 <= len(robot_list) <= 4:
        invalid_details.append(InvalidDetail(0, "robot list must contain 2 to 4 robots"))
    if len(set(robot_list)) != len(robot_list):
        invalid_details.append(InvalidDetail(0, "robot list contains duplicate robots"))
    if not all(_is_robot_index(robot_idx, robot_count) for robot_idx in robot_list):
        invalid_details.append(InvalidDetail(0, "robot list contains invalid robot index"))

    if len(assigned_robots) != len(subtasks):
        invalid_details.append(InvalidDetail(0, "assigned_robots length must match subtasks"))
        return invalid_details

    selected_robot_ids = set(robot_list)
    obj_mass_map = context.mass_map(floor_plan)
    for subtask, robot_idx in zip(subtasks, assigned_robots):
        if not _is_robot_index(robot_idx, robot_count):
            invalid_details.append(_invalid_detail("assigned robot index is invalid", subtask))
            continue
        if robot_idx not in selected_robot_ids:
            invalid_details.append(_invalid_detail("assigned robot is not in robot list", subtask))
            continue

        robot = context.robot_defs[robot_idx - 1]
        if not context.robot_can_complete_subtask(robot, subtask, obj_mass_map):
            invalid_details.append(_invalid_detail("assigned robot cannot complete subtask", subtask))

    return invalid_details


def validate_task_record(
    record: Any,
    floor_plan: int,
    context: RelabelContext,
) -> Tuple[bool, List[InvalidDetail]]:
    if not isinstance(record, dict):
        return False, [InvalidDetail(0, "JSON line is not a task object")]

    subtasks = record.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        return False, [InvalidDetail(0, "missing or invalid subtasks")]

    invalid_details: List[InvalidDetail] = []
    for subtask in subtasks:
        invalid_details.extend(validate_subtask(subtask, floor_plan, context))
    if invalid_details:
        return False, invalid_details

    typed_subtasks = [subtask for subtask in subtasks if isinstance(subtask, dict)]
    skill_sets = context.skill_sets(floor_plan)
    if not context.check_subtasks(typed_subtasks, skill_sets):
        return False, [InvalidDetail(0, "subtasks fail data_engine.check_subtasks")]

    invalid_details.extend(
        validate_robot_assignments(record, typed_subtasks, floor_plan, context)
    )
    return not invalid_details, invalid_details


def _build_pre_task_actions(
    record: Any,
    floor_plan: int,
    context: RelabelContext,
) -> List[Dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    subtasks = record.get("subtasks")
    if not isinstance(subtasks, list):
        return []
    valid_subtasks = [subtask for subtask in subtasks if isinstance(subtask, dict)]
    return data_engine._build_pre_task_actions_for_subtasks(
        valid_subtasks,
        context.floor_objects(floor_plan),
    )


def _ordered_task_record(record: Dict[str, Any]) -> Dict[str, Any]:
    preferred_keys = (
        "task",
        "robot list",
        "object_states",
        "pre_task_actions",
        "trans",
        "max_trans",
        "subtasks",
        "assigned_robots",
    )
    invalid_value = record.pop("invalid", None)
    ordered: Dict[str, Any] = {}
    for key in preferred_keys:
        if key in record:
            ordered[key] = record.pop(key)
    for key, value in record.items():
        ordered[key] = value
    if invalid_value is not None:
        ordered["invalid"] = invalid_value
    return ordered


def relabel_json_value(
    value: Any,
    floor_plan: int,
    context: RelabelContext,
) -> Tuple[Dict[str, Any], bool, List[InvalidDetail], List[Dict[str, Any]]]:
    pre_task_actions = _build_pre_task_actions(value, floor_plan, context)
    valid, invalid_details = validate_task_record(value, floor_plan, context)

    if not isinstance(value, dict):
        output_record = {"original": value, "pre_task_actions": pre_task_actions}
        output_record["invalid"] = True
        return output_record, False, invalid_details, pre_task_actions

    output_record = dict(value)
    output_record.pop("valid", None)
    output_record.pop("invalid", None)
    output_record["pre_task_actions"] = pre_task_actions
    if not valid:
        output_record["invalid"] = True
    return _ordered_task_record(output_record), valid, invalid_details, pre_task_actions


def process_task_file(
    path: Path,
    *,
    context: RelabelContext,
    dry_run: bool = False,
) -> FileRelabelStats:
    floor_plan = floor_plan_from_path(path)
    stats = FileRelabelStats(path=path)
    output_lines: List[str] = []

    lines = path.read_text(encoding="utf-8").splitlines()
    for line_no, raw_line in enumerate(lines, start=1):
        stats.rows += 1
        original_invalid = False
        original_pre_task_actions: Any = None

        if raw_line.strip():
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path}:{line_no}: {exc}") from exc
            if isinstance(value, dict):
                original_invalid = bool(value.get("invalid"))
                original_pre_task_actions = value.get("pre_task_actions")
        else:
            value = {}
            original_invalid = False

        output_record, valid, invalid_details, pre_task_actions = relabel_json_value(
            value,
            floor_plan,
            context,
        )

        if not raw_line.strip():
            output_record = {"pre_task_actions": [], "invalid": True}
            valid = False
            invalid_details = [InvalidDetail(0, "empty JSONL line")]
            pre_task_actions = []

        if valid:
            stats.valid += 1
        else:
            stats.invalid += 1

        if not valid and not original_invalid:
            stats.invalid_added += 1
        if valid and original_invalid:
            stats.invalid_removed += 1
        if original_pre_task_actions != pre_task_actions:
            stats.pre_task_changed += 1

        for detail in invalid_details:
            detail.line_no = line_no
            stats.invalid_details.append(detail)

        output_line = json.dumps(output_record, ensure_ascii=False)
        output_lines.append(output_line)
        if raw_line != output_line:
            stats.changed = True

    if stats.changed and not dry_run:
        write_jsonl_atomic(path, output_lines)

    return stats


def write_jsonl_atomic(path: Path, lines: Iterable[str]) -> None:
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            for line in lines:
                tmp_file.write(line)
                tmp_file.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def run_relabel(
    data_dir: Path,
    *,
    target_dirs: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    show_invalid: bool = False,
    context: Optional[RelabelContext] = None,
) -> RelabelStats:
    context = context or RelabelContext()
    selected_target_dirs = tuple(target_dirs or DEFAULT_TARGET_DIRS)

    stats = RelabelStats()
    for path in discover_task_files(data_dir, selected_target_dirs):
        file_stats = process_task_file(path, context=context, dry_run=dry_run)
        stats.add_file(file_stats)
        if show_invalid:
            for detail in file_stats.invalid_details:
                print(detail.format(path))
    return stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Relabel final_test_new task JSONL files with data_engine generation "
            "rules and pre_task_actions."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_REPO_ROOT / "data",
        help="Directory containing final_test_new_* folders. Defaults to the repo data directory.",
    )
    parser.add_argument(
        "--target-dir",
        action="append",
        dest="target_dirs",
        help=(
            "Target directory under --data-dir. Can be repeated. "
            "Defaults to the curated final_test_new directories."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print statistics without writing files.",
    )
    parser.add_argument(
        "--show-invalid",
        action="store_true",
        help="Print file:line details for invalid tasks.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    stats = run_relabel(
        args.data_dir,
        target_dirs=args.target_dirs,
        dry_run=args.dry_run,
        show_invalid=args.show_invalid,
    )
    mode = "DRY RUN" if args.dry_run else "UPDATED"
    print(
        f"{mode}: scanned {stats.files} files, {stats.rows} rows, "
        f"valid={stats.valid}, invalid={stats.invalid}, "
        f"changed_files={stats.changed_files}, invalid_added={stats.invalid_added}, "
        f"invalid_removed={stats.invalid_removed}, "
        f"pre_task_changed={stats.pre_task_changed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
