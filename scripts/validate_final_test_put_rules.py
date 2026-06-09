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


PUT_SKILLS = {"PutOn", "PutIn"}
FLOOR_PLAN_FILE_RE = re.compile(r"^FloorPlan(\d+)\.jsonl$")
SkillSets = Dict[str, Any]
SkillSetLoader = Callable[[str], SkillSets]


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
class FileValidationStats:
    path: Path
    rows: int = 0
    valid: int = 0
    invalid: int = 0
    changed: bool = False
    invalid_details: List[InvalidDetail] = field(default_factory=list)


@dataclass
class ValidationStats:
    files: int = 0
    rows: int = 0
    valid: int = 0
    invalid: int = 0
    changed_files: int = 0
    invalid_details: List[InvalidDetail] = field(default_factory=list)

    def add_file(self, file_stats: FileValidationStats) -> None:
        self.files += 1
        self.rows += file_stats.rows
        self.valid += file_stats.valid
        self.invalid += file_stats.invalid
        if file_stats.changed:
            self.changed_files += 1
        self.invalid_details.extend(file_stats.invalid_details)


def floor_plan_from_path(path: Path) -> str:
    match = FLOOR_PLAN_FILE_RE.match(path.name)
    if match is None:
        raise ValueError(f"Expected a FloorPlan*.jsonl file, got: {path}")
    return f"FloorPlan{match.group(1)}"


def discover_task_files(data_dir: Path, pattern: str) -> List[Path]:
    directories = sorted(path for path in data_dir.glob(pattern) if path.is_dir())
    files: List[Path] = []
    for directory in directories:
        files.extend(sorted(directory.glob("FloorPlan*.jsonl")))
    return files


def validate_task_record(record: Dict[str, Any], skill_sets: SkillSets) -> Tuple[bool, List[InvalidDetail]]:
    subtasks = record.get("subtasks")
    if not isinstance(subtasks, list):
        return False, [InvalidDetail(0, "missing or invalid subtasks")]

    invalid_details: List[InvalidDetail] = []
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            invalid_details.append(InvalidDetail(0, "subtask is not an object"))
            continue

        skill = subtask.get("skill")
        if skill not in PUT_SKILLS:
            continue

        objects = subtask.get("objects")
        if not isinstance(objects, list) or len(objects) != 2:
            invalid_details.append(
                InvalidDetail(0, "PutOn/PutIn objects must contain exactly two items", skill, objects)
            )
            continue

        obj, receptacle = objects
        if not isinstance(obj, str) or not isinstance(receptacle, str):
            invalid_details.append(
                InvalidDetail(0, "PutOn/PutIn objects must be strings", skill, objects)
            )
            continue

        if not data_engine._can_place_with_skill(obj, receptacle, skill, skill_sets):
            invalid_details.append(
                InvalidDetail(0, "invalid PutOn/PutIn placement pair", skill, objects)
            )

    return not invalid_details, invalid_details


def _line_record_with_invalid(value: Any, valid: bool) -> Dict[str, Any]:
    if isinstance(value, dict):
        value.pop("valid", None)
        if valid:
            value.pop("invalid", None)
        else:
            value["invalid"] = True
        return value
    return {"original": value, "invalid": True}


def _validate_json_value(value: Any, skill_sets: SkillSets) -> Tuple[Dict[str, Any], bool, List[InvalidDetail]]:
    if not isinstance(value, dict):
        return _line_record_with_invalid(value, False), False, [
            InvalidDetail(0, "JSON line is not a task object")
        ]

    valid, invalid_details = validate_task_record(value, skill_sets)
    return _line_record_with_invalid(value, valid), valid, invalid_details


def process_task_file(
    path: Path,
    *,
    dry_run: bool = False,
    skill_set_loader: SkillSetLoader = data_engine._build_object_skill_sets,
) -> FileValidationStats:
    floor_plan = floor_plan_from_path(path)
    skill_sets = skill_set_loader(floor_plan)
    stats = FileValidationStats(path=path)
    output_records: List[Dict[str, Any]] = []

    lines = path.read_text(encoding="utf-8").splitlines()
    for line_no, raw_line in enumerate(lines, start=1):
        stats.rows += 1
        original_marker = (False, None, False, None)
        original_is_task_object = False
        if not raw_line.strip():
            output_record: Dict[str, Any] = {"invalid": True}
            valid = False
            invalid_details = [InvalidDetail(0, "empty JSONL line")]
        else:
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path}:{line_no}: {exc}") from exc

            original_is_task_object = isinstance(value, dict)
            if original_is_task_object:
                original_marker = (
                    "valid" in value,
                    value.get("valid"),
                    "invalid" in value,
                    value.get("invalid"),
                )
            output_record, valid, invalid_details = _validate_json_value(value, skill_sets)

        if valid:
            stats.valid += 1
        else:
            stats.invalid += 1

        for detail in invalid_details:
            detail.line_no = line_no
            stats.invalid_details.append(detail)

        output_records.append(output_record)
        if not raw_line.strip() or not original_is_task_object:
            stats.changed = True
        elif original_marker != (
            "valid" in output_record,
            output_record.get("valid"),
            "invalid" in output_record,
            output_record.get("invalid"),
        ):
            stats.changed = True

    if stats.changed and not dry_run:
        write_jsonl_atomic(path, output_records)

    return stats


def write_jsonl_atomic(path: Path, records: Iterable[Dict[str, Any]]) -> None:
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
            for record in records:
                tmp_file.write(json.dumps(record, ensure_ascii=False))
                tmp_file.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def run_validation(
    data_dir: Path,
    *,
    pattern: str = "final_test_new_*",
    dry_run: bool = False,
    show_invalid: bool = False,
    skill_set_loader: Optional[SkillSetLoader] = None,
) -> ValidationStats:
    loader = skill_set_loader or data_engine._build_object_skill_sets
    skill_set_cache: Dict[str, SkillSets] = {}

    def cached_loader(floor_plan: str) -> SkillSets:
        if floor_plan not in skill_set_cache:
            skill_set_cache[floor_plan] = loader(floor_plan)
        return skill_set_cache[floor_plan]

    stats = ValidationStats()
    for path in discover_task_files(data_dir, pattern):
        file_stats = process_task_file(path, dry_run=dry_run, skill_set_loader=cached_loader)
        stats.add_file(file_stats)
        if show_invalid:
            for detail in file_stats.invalid_details:
                print(detail.format(path))
    return stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mark final_test_new FloorPlan JSONL tasks valid/invalid under PutOn/PutIn rules."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_REPO_ROOT / "data",
        help="Directory containing final_test_new_* folders. Defaults to the repo data directory.",
    )
    parser.add_argument(
        "--pattern",
        default="final_test_new_*",
        help="Directory glob under --data-dir to scan. Defaults to final_test_new_*.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print statistics without writing invalid markers.",
    )
    parser.add_argument(
        "--show-invalid",
        action="store_true",
        help="Print file:line details for invalid tasks.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    stats = run_validation(
        args.data_dir,
        pattern=args.pattern,
        dry_run=args.dry_run,
        show_invalid=args.show_invalid,
    )
    mode = "DRY RUN" if args.dry_run else "UPDATED"
    print(
        f"{mode}: scanned {stats.files} files, {stats.rows} rows, "
        f"valid={stats.valid}, invalid={stats.invalid}, changed_files={stats.changed_files}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
