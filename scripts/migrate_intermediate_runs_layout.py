#!/usr/bin/env python3
"""Migrate old intermediate run directories into the dataset-scoped layout."""

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from run_config import normalize_floor_plan


RUN_DIR_RE = re.compile(r"^\d{8}_\d{3}$")
DATE_RE = re.compile(r"(20\d{6})")


@dataclass
class MigrationRecord:
    summary_path: Path
    floor_summary_path: Path
    source: Path
    target: Path
    old_task_run_dir: str
    test_set: str
    floor_plan: str
    task: str
    run_date: str
    run_sequence: int
    status: str = "planned"
    reason: str = ""


@dataclass
class MigrationStats:
    planned: int = 0
    applied: int = 0
    copied: int = 0
    skipped: int = 0
    missing: int = 0
    errors: int = 0
    json_updated: int = 0


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def sanitize_filename(value: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\s]+', "_", str(value).strip())
    sanitized = sanitized.strip("._")
    return sanitized or "task"


def resolve_path(path: str, base_path: Path) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return (base_path / resolved).resolve()


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_with_backup(path: Path, content: Dict[str, Any]) -> None:
    backup_path = path.with_name(f"{path.name}.bak")
    if path.exists() and not backup_path.exists():
        shutil.copy2(path, backup_path)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def summary_paths(parallel_root: Path, selected_summaries: Optional[List[str]], base_path: Path) -> List[Path]:
    if selected_summaries:
        return [resolve_path(summary, base_path) for summary in selected_summaries]
    return sorted(parallel_root.glob("pddlrun_llmseparate_*/summary.json"))


def is_new_layout(path: Path, intermediate_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(intermediate_root.resolve())
    except ValueError:
        return False

    parts = relative.parts
    return len(parts) >= 3 and "___" in parts[0] and RUN_DIR_RE.fullmatch(parts[2]) is not None


def extract_run_date(source: Path) -> str:
    manifest_path = source / "run_manifest.json"
    if manifest_path.exists():
        try:
            manifest = read_json(manifest_path)
        except json.JSONDecodeError:
            manifest = {}
        created_at = str(manifest.get("created_at", ""))
        match = DATE_RE.search(created_at)
        if match:
            return match.group(1)

    match = DATE_RE.search(source.name)
    if match:
        return match.group(1)
    raise ValueError(f"Cannot infer run date from {source}")


def next_run_sequence(task_parent: Path, run_date: str, reserved_targets: Iterable[Path]) -> int:
    reserved_names = {
        target.name
        for target in reserved_targets
        if target.parent == task_parent
    }
    max_sequence = 0
    if task_parent.exists():
        for child in task_parent.iterdir():
            if not child.is_dir():
                continue
            match = re.fullmatch(rf"{re.escape(run_date)}_(\d{{3}})", child.name)
            if match:
                max_sequence = max(max_sequence, int(match.group(1)))
    for name in reserved_names:
        match = re.fullmatch(rf"{re.escape(run_date)}_(\d{{3}})", name)
        if match:
            max_sequence = max(max_sequence, int(match.group(1)))
    return max_sequence + 1


def floor_summary_path(summary_path: Path, floor_plan: str) -> Path:
    return summary_path.parent / f"FloorPlan{floor_plan}" / "summary.json"


def iter_summary_results(summary_data: Dict[str, Any]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for floor_summary in summary_data.get("summaries", []):
        if not isinstance(floor_summary, dict):
            continue
        for result in floor_summary.get("results", []):
            if isinstance(result, dict):
                yield floor_summary, result


def build_records(
    summaries: List[Path],
    intermediate_root: Path,
    base_path: Path,
    fallback_test_set: Optional[str],
) -> List[MigrationRecord]:
    records: List[MigrationRecord] = []
    reserved_targets: List[Path] = []

    for summary_path in summaries:
        if not summary_path.exists():
            records.append(MigrationRecord(
                summary_path=summary_path,
                floor_summary_path=summary_path,
                source=summary_path,
                target=summary_path,
                old_task_run_dir="",
                test_set="",
                floor_plan="",
                task="",
                run_date="",
                run_sequence=0,
                status="missing",
                reason="summary file not found",
            ))
            continue

        summary_data = read_json(summary_path)
        test_set = summary_data.get("test_set") or fallback_test_set
        for floor_summary, result in iter_summary_results(summary_data):
            old_task_run_dir = result.get("task_run_dir")
            if not old_task_run_dir:
                records.append(MigrationRecord(
                    summary_path=summary_path,
                    floor_summary_path=summary_path,
                    source=summary_path,
                    target=summary_path,
                    old_task_run_dir="",
                    test_set=str(test_set or ""),
                    floor_plan=str(floor_summary.get("floor_plan", result.get("floor_plan", ""))),
                    task=str(result.get("task", "")),
                    run_date="",
                    run_sequence=0,
                    status="skipped",
                    reason="missing task_run_dir",
                ))
                continue

            source = resolve_path(str(old_task_run_dir), base_path)
            if is_new_layout(source, intermediate_root):
                status = "skipped" if source.exists() else "missing"
                reason = "already in new layout" if source.exists() else "task_run_dir not found"
                records.append(MigrationRecord(
                    summary_path=summary_path,
                    floor_summary_path=floor_summary_path(summary_path, normalize_floor_plan(str(result.get("floor_plan", floor_summary.get("floor_plan", ""))))),
                    source=source,
                    target=source,
                    old_task_run_dir=str(old_task_run_dir),
                    test_set=str(test_set or ""),
                    floor_plan=normalize_floor_plan(str(result.get("floor_plan", floor_summary.get("floor_plan", "")))),
                    task=str(result.get("task", "")),
                    run_date="",
                    run_sequence=0,
                    status=status,
                    reason=reason,
                ))
                continue

            if not test_set:
                records.append(MigrationRecord(
                    summary_path=summary_path,
                    floor_summary_path=summary_path,
                    source=source,
                    target=source,
                    old_task_run_dir=str(old_task_run_dir),
                    test_set="",
                    floor_plan=normalize_floor_plan(str(result.get("floor_plan", floor_summary.get("floor_plan", "")))),
                    task=str(result.get("task", "")),
                    run_date="",
                    run_sequence=0,
                    status="skipped",
                    reason="missing test_set",
                ))
                continue

            if not source.exists():
                records.append(MigrationRecord(
                    summary_path=summary_path,
                    floor_summary_path=floor_summary_path(summary_path, normalize_floor_plan(str(result.get("floor_plan", floor_summary.get("floor_plan", ""))))),
                    source=source,
                    target=source,
                    old_task_run_dir=str(old_task_run_dir),
                    test_set=str(test_set),
                    floor_plan=normalize_floor_plan(str(result.get("floor_plan", floor_summary.get("floor_plan", "")))),
                    task=str(result.get("task", "")),
                    run_date="",
                    run_sequence=0,
                    status="missing",
                    reason="task_run_dir not found",
                ))
                continue

            floor_plan = normalize_floor_plan(str(result.get("floor_plan", floor_summary.get("floor_plan", ""))))
            task = str(result.get("task") or source.name)
            try:
                run_date = extract_run_date(source)
            except ValueError as exc:
                records.append(MigrationRecord(
                    summary_path=summary_path,
                    floor_summary_path=floor_summary_path(summary_path, floor_plan),
                    source=source,
                    target=source,
                    old_task_run_dir=str(old_task_run_dir),
                    test_set=str(test_set),
                    floor_plan=floor_plan,
                    task=task,
                    run_date="",
                    run_sequence=0,
                    status="skipped",
                    reason=str(exc),
                ))
                continue

            task_parent = (
                intermediate_root
                / f"{sanitize_filename(str(test_set))}___{sanitize_filename(floor_plan)}"
                / sanitize_filename(task)[:80]
            )
            run_sequence = next_run_sequence(task_parent, run_date, reserved_targets)
            target = task_parent / f"{run_date}_{run_sequence:03d}"
            reserved_targets.append(target)
            records.append(MigrationRecord(
                summary_path=summary_path,
                floor_summary_path=floor_summary_path(summary_path, floor_plan),
                source=source,
                target=target,
                old_task_run_dir=str(old_task_run_dir),
                test_set=str(test_set),
                floor_plan=floor_plan,
                task=task,
                run_date=run_date,
                run_sequence=run_sequence,
            ))

    return records


def replace_task_run_dirs(content: Any, replacements: Dict[str, str]) -> Any:
    if isinstance(content, dict):
        return {
            key: replace_task_run_dirs(value, replacements)
            for key, value in content.items()
        }
    if isinstance(content, list):
        return [replace_task_run_dirs(value, replacements) for value in content]
    if isinstance(content, str):
        return replacements.get(content, content)
    return content


def update_summary_json(json_paths: Iterable[Path], replacements: Dict[str, str]) -> int:
    updated = 0
    for path in sorted(set(json_paths)):
        if not path.exists():
            continue
        original = read_json(path)
        changed = replace_task_run_dirs(original, replacements)
        if changed != original:
            write_json_with_backup(path, changed)
            updated += 1
    return updated


def update_manifest(record: MigrationRecord) -> bool:
    manifest_path = record.target / "run_manifest.json"
    if not manifest_path.exists():
        return False
    manifest = read_json(manifest_path)
    manifest.update({
        "storage_base_dir": str(record.target.parents[2]),
        "task_run_dir": str(record.target),
        "test_set": record.test_set,
        "floor_plan": record.floor_plan,
        "run_date": record.run_date,
        "run_sequence": record.run_sequence,
    })
    write_json_with_backup(manifest_path, manifest)
    return True


def apply_record(record: MigrationRecord, copy: bool) -> None:
    record.target.parent.mkdir(parents=True, exist_ok=True)
    if record.target.exists():
        raise FileExistsError(f"target already exists: {record.target}")
    if copy:
        shutil.copytree(record.source, record.target)
    else:
        shutil.move(str(record.source), str(record.target))


def print_records(records: List[MigrationRecord]) -> None:
    for record in records:
        if record.status == "planned":
            print(f"PLAN {record.source} -> {record.target}")
        else:
            detail = f": {record.reason}" if record.reason else ""
            print(f"{record.status.upper()} {record.source}{detail}")


def run_migration(args: argparse.Namespace, base_path: Optional[Path] = None) -> MigrationStats:
    base_path = (base_path or repo_root()).resolve()
    parallel_root = resolve_path(args.parallel_root, base_path)
    intermediate_root = resolve_path(args.intermediate_root, base_path)
    summaries = summary_paths(parallel_root, args.summary, base_path)
    records = build_records(
        summaries=summaries,
        intermediate_root=intermediate_root,
        base_path=base_path,
        fallback_test_set=args.fallback_test_set,
    )

    stats = MigrationStats(
        planned=sum(1 for record in records if record.status == "planned"),
        skipped=sum(1 for record in records if record.status == "skipped"),
        missing=sum(1 for record in records if record.status == "missing"),
    )

    print_records(records)
    if not args.apply:
        print(
            f"Dry run complete: {stats.planned} planned, "
            f"{stats.skipped} skipped, {stats.missing} missing."
        )
        return stats

    replacements: Dict[str, str] = {}
    json_paths: List[Path] = []
    applied_records: List[MigrationRecord] = []

    for record in records:
        if record.status != "planned":
            continue
        try:
            apply_record(record, copy=args.copy)
        except Exception as exc:
            record.status = "error"
            record.reason = str(exc)
            stats.errors += 1
            print(f"ERROR {record.source}: {exc}")
            continue

        record.status = "copied" if args.copy else "applied"
        if args.copy:
            stats.copied += 1
        else:
            stats.applied += 1
        replacements[record.old_task_run_dir] = str(record.target)
        json_paths.extend([record.summary_path, record.floor_summary_path])
        applied_records.append(record)
        print(f"{record.status.upper()} {record.source} -> {record.target}")

    if not args.no_update_json:
        stats.json_updated += update_summary_json(json_paths, replacements)
        for record in applied_records:
            if update_manifest(record):
                stats.json_updated += 1

    print(
        f"Migration complete: {stats.applied} moved, {stats.copied} copied, "
        f"{stats.skipped} skipped, {stats.missing} missing, "
        f"{stats.errors} errors, {stats.json_updated} JSON files updated."
    )
    return stats


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate intermediate run directories referenced by parallel_runs summaries."
    )
    parser.add_argument("--parallel-root", default="parallel_runs")
    parser.add_argument("--intermediate-root", default="logs/intermediate_runs")
    parser.add_argument(
        "--summary",
        action="append",
        help="Specific top-level parallel summary to migrate. May be passed multiple times.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually move/copy directories and update JSON.")
    parser.add_argument("--copy", action="store_true", help="Copy runs instead of moving them.")
    parser.add_argument("--fallback-test-set", default=None)
    parser.add_argument("--no-update-json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None, base_path: Optional[Path] = None) -> int:
    args = parse_args(argv)
    run_migration(args, base_path=base_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
