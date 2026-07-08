#!/usr/bin/env python3
"""Build a lightweight PDDL RAG corpus from intermediate logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from parsing_utils import ParsingUtils
from run_config import load_run_config


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_STAGES = ("decompose", "allocate", "problem_generation")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build PDDL RAG corpus JSONL and a simple lexical index from intermediate logs."
    )
    parser.add_argument(
        "--base-path",
        default=str(REPO_ROOT),
        help="Repository root. Defaults to the parent of scripts/.",
    )
    parser.add_argument(
        "--logs-root",
        help="Root containing intermediate run directories. Defaults to RunConfig.storage_base_dir.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/rag",
        help="Directory for default output files.",
    )
    parser.add_argument(
        "--corpus-path",
        help="Output JSONL corpus path. Defaults to output-dir/task_<stage>_corpus.jsonl.",
    )
    parser.add_argument(
        "--index-path",
        help="Output lexical index JSON path. Defaults to output-dir/task_<stage>_index.json.",
    )
    parser.add_argument(
        "--summary-path",
        help="Output summary JSON path. Defaults to output-dir/task_<stage>_summary.json.",
    )
    parser.add_argument(
        "--stage",
        choices=SUPPORTED_STAGES,
        required=True,
        help="Required stage to emit. Supported values: decompose, allocate.",
    )
    parser.add_argument(
        "--limit-runs",
        type=int,
        help="Optional maximum number of run manifests to process for debugging.",
    )
    return parser.parse_args(argv)


def resolve_path(base_path: Path, value: Optional[str], default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_path / path


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def artifact_relpath(manifest: Dict[str, Any], section: str, key: str, default: str) -> str:
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict):
        section_value = artifacts.get(section)
        if isinstance(section_value, dict):
            value = section_value.get(key)
            if isinstance(value, str) and value:
                return value
    return default


def artifact_path(run_dir: Path, manifest: Dict[str, Any], section: str, key: str, default: str) -> Path:
    return run_dir / artifact_relpath(manifest, section, key, default)


def safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def compact_json(value: Any, max_chars: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def load_task_context(run_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    path = artifact_path(run_dir, manifest, "inputs", "task_context", "inputs/task_context.json")
    data = read_json(path)
    return safe_dict(data)


def load_key_objects(run_dir: Path, manifest: Dict[str, Any]) -> List[Any]:
    path = artifact_path(run_dir, manifest, "allocate", "key_objects", "02_allocate/00_key_objects.json")
    return safe_list(read_json(path))


def load_key_object_states(run_dir: Path, manifest: Dict[str, Any]) -> Any:
    path = artifact_path(
        run_dir,
        manifest,
        "problem_files",
        "key_object_pddl_states",
        "05_problem_generation/key_object_pddl_states.json",
    )
    data = read_json(path)
    return [] if data is None else data


def object_names_from_context(task_context: Dict[str, Any], limit: int = 30) -> List[str]:
    objects_text = str(task_context.get("objects_ai", ""))
    names = re.findall(r"'name'\s*:\s*'([^']+)'|\"name\"\s*:\s*\"([^\"]+)\"", objects_text)
    result: List[str] = []
    seen: Set[str] = set()
    for left, right in names:
        name = left or right
        key = name.lower()
        if key not in seen:
            seen.add(key)
            result.append(name)
        if len(result) >= limit:
            break
    return result


def robot_summary(robots: Any) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for robot in safe_list(robots):
        if not isinstance(robot, dict):
            continue
        summary.append(
            {
                "name": robot.get("name"),
                "skills": robot.get("skills", []),
                "mass_capacity": robot.get("mass_capacity"),
            }
        )
    return summary


def load_subtasks(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    index_path = artifact_path(
        run_dir,
        manifest,
        "problem_files",
        "subtasks_index",
        "04_problem_files/03_subtasks.json",
    )
    entries = safe_list(read_json(index_path))
    subtasks: List[Dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        subtask_index = entry.get("index", position)
        relpath = entry.get("path")
        text = read_text(run_dir / relpath) if isinstance(relpath, str) else None
        subtasks.append(
            {
                "index": subtask_index,
                "path": relpath,
                "text": text or "",
            }
        )

    if subtasks:
        return subtasks

    subtasks_dir = run_dir / "04_problem_files" / "subtasks"
    for path in sorted(subtasks_dir.glob("subtask_*.txt")):
        match = re.search(r"subtask_(\d+)", path.name)
        subtask_index = int(match.group(1)) if match else len(subtasks) + 1
        subtasks.append(
            {
                "index": subtask_index,
                "path": str(path.relative_to(run_dir)),
                "text": read_text(path) or "",
            }
        )
    return subtasks


def load_generated_problem_map(run_dir: Path, manifest: Dict[str, Any]) -> Dict[int, str]:
    path = artifact_path(
        run_dir,
        manifest,
        "problem_files",
        "generated_problem_files",
        "04_problem_files/04_generated_problem_files.json",
    )
    entries = safe_list(read_json(path))
    result: Dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = numeric_int(entry.get("index"))
        content = entry.get("content")
        if index is not None and isinstance(content, str):
            result[index] = content
    return result


def numeric_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def subtask_index_from_problem_file(name: str) -> Optional[int]:
    match = re.search(r"subtask_(\d+)", name)
    return int(match.group(1)) if match else None


def load_validation_records(run_dir: Path, manifest: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    path = artifact_path(
        run_dir,
        manifest,
        "validate",
        "manifest",
        "07_validate/validation_manifest.json",
    )
    records = safe_list(read_json(path))
    result: Dict[int, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        problem_file = record.get("problem_file")
        index = subtask_index_from_problem_file(problem_file) if isinstance(problem_file, str) else None
        if index is not None:
            result[index] = record
    return result


def load_planner_records(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = artifact_path(
        run_dir,
        manifest,
        "planner",
        "manifest",
        "08_planner/planner_manifest.json",
    )
    return [record for record in safe_list(read_json(path)) if isinstance(record, dict)]


def planner_records_by_subtask(records: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for record in records:
        problem_file = record.get("problem_file")
        index = subtask_index_from_problem_file(problem_file) if isinstance(problem_file, str) else None
        if index is not None:
            result[index] = record
    return result


def planner_success_count(records: Iterable[Dict[str, Any]]) -> int:
    return sum(1 for record in records if record.get("return_code") == 0)


def completion_counts(manifest: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    completion = manifest.get("completion")
    if not isinstance(completion, dict):
        return None, None
    successful = numeric_int(completion.get("successful_subtasks"))
    total = numeric_int(completion.get("total_subtasks"))
    return successful, total


def classify_quality(manifest: Dict[str, Any], planner_records: List[Dict[str, Any]]) -> str:
    successful, total = completion_counts(manifest)
    has_planner_records = bool(planner_records)
    all_planner_success = has_planner_records and all(
        record.get("return_code") == 0 for record in planner_records
    )
    completion_success = total is not None and total > 0 and successful == total
    if completion_success and all_planner_success:
        return "success"
    if planner_success_count(planner_records) > 0:
        return "partial"
    if (
        successful is not None
        and total is not None
        and total > 0
        and 0 < successful < total
    ):
        return "partial"
    return "failed"


def extract_sequence_operations(allocate_output: str) -> str:
    return "\n".join(ParsingUtils.extract_sequence_operations(allocate_output))


def allocate_output_path(run_dir: Path, manifest: Dict[str, Any]) -> Path:
    return artifact_path(
        run_dir,
        manifest,
        "allocate",
        "output",
        "02_allocate/02_allocate_output.txt",
    )


def final_sequence_section_lines(allocate_output: str) -> List[str]:
    lines = allocate_output.strip().splitlines()
    final_header_index: Optional[int] = None
    for index, line in enumerate(lines):
        if ParsingUtils.is_sequence_header(line):
            final_header_index = index

    if final_header_index is None:
        return []

    sequence_lines: List[str] = []
    header_line = lines[final_header_index].strip()
    header_match = re.search(
        r"\bSequence\s+of\s+Operations?\b\s*:?\s*(?P<trailing>.*)$",
        header_line,
        re.IGNORECASE,
    )
    if header_match:
        trailing = header_match.group("trailing").strip()
        if trailing:
            sequence_lines.append(trailing)

    sequence_lines.extend(
        line.strip()
        for line in lines[final_header_index + 1:]
        if line.strip()
    )
    return sequence_lines


def sequence_line_is_assignment_only(line: str) -> bool:
    assignment_re = ParsingUtils.sequence_assignment_re()
    stripped = re.sub(r"^\s*[-*]\s+", "", line.strip())
    if not assignment_re.search(stripped):
        return False

    remainder = assignment_re.sub("", stripped)
    remainder = re.sub(r"[;\s]+", "", remainder)
    return not remainder


def validate_allocate_candidate(
    allocate_output: str,
    subtasks: List[Dict[str, Any]],
    robots: List[Dict[str, Any]],
    quality: str,
) -> Tuple[bool, str, List[str], Dict[int, int]]:
    if quality != "success":
        return False, "non_success_quality", [], {}
    if not robots:
        return False, "missing_robots", [], {}
    if not subtasks:
        return False, "missing_subtasks", [], {}
    if not allocate_output.strip():
        return False, "missing_allocate_output", [], {}

    expected_subtask_ids: Set[int] = set()
    for subtask in subtasks:
        subtask_index = numeric_int(safe_dict(subtask).get("index"))
        if subtask_index is None:
            return False, "invalid_subtask_index", [], {}
        expected_subtask_ids.add(subtask_index)

    sequence_lines = final_sequence_section_lines(allocate_output)
    if not sequence_lines:
        return False, "missing_sequence", [], {}

    merged_lines = ParsingUtils.merge_sequence_lines(sequence_lines)
    if any(not sequence_line_is_assignment_only(line) for line in merged_lines):
        return False, "unparseable_sequence_line", [], {}

    sequence_operations, assignments = ParsingUtils.parse_sequence_section(sequence_lines)
    if not assignments:
        return False, "missing_sequence_assignment", [], {}

    assigned_subtask_ids = set(assignments)
    if not expected_subtask_ids.issubset(assigned_subtask_ids):
        return False, "missing_subtask_assignment", sequence_operations, assignments
    if not assigned_subtask_ids.issubset(expected_subtask_ids):
        return False, "extra_subtask_assignment", sequence_operations, assignments

    valid_robot_ids = set(range(1, len(robots) + 1))
    if any(robot_id not in valid_robot_ids for robot_id in assignments.values()):
        return False, "invalid_robot_id", sequence_operations, assignments

    return True, "", sequence_operations, assignments


def make_doc_id(stage: str, run_dir: Path, suffix: str = "") -> str:
    digest = hashlib.sha1(f"{run_dir}|{stage}|{suffix}".encode("utf-8")).hexdigest()[:12]
    return f"{stage}:{digest}"


def source_path(run_dir: Path, path: Path) -> Optional[str]:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def base_metadata(
    manifest: Dict[str, Any],
    run_dir: Path,
    quality: str,
    source_paths: List[str],
) -> Dict[str, Any]:
    return {
        "task": manifest.get("task"),
        "task_index": manifest.get("task_index"),
        "test_set": manifest.get("test_set"),
        "floor_plan": manifest.get("floor_plan"),
        "model": manifest.get("model"),
        "run_date": manifest.get("run_date"),
        "run_sequence": manifest.get("run_sequence"),
        "task_run_dir": str(run_dir),
        "source_paths": source_paths,
        "run_quality": quality,
    }


def make_document(
    doc_id: str,
    stage: str,
    query_text: str,
    content: str,
    metadata: Dict[str, Any],
    quality: str,
) -> Dict[str, Any]:
    return {
        "id": doc_id,
        "stage": stage,
        "query_text": query_text.strip(),
        "content": content.strip(),
        "metadata": metadata,
        "quality": quality,
        "retrieval_eligible": quality == "success",
    }


def build_decompose_doc(
    run_dir: Path,
    manifest: Dict[str, Any],
    task_context: Dict[str, Any],
    key_objects: List[Any],
    quality: str,
) -> Optional[Dict[str, Any]]:
    output_path = artifact_path(
        run_dir,
        manifest,
        "decompose",
        "output",
        "01_decompose/02_decompose_output.txt",
    )
    output = read_text(output_path)
    if not output:
        return None

    task = str(manifest.get("task") or task_context.get("task") or "")
    query_text = f"Task: {task}"
    content = "\n".join(
        [
            "# Task",
            task,
            "",
            "# Decomposition Output",
            output,
        ]
    )
    metadata = base_metadata(manifest, run_dir, quality, [source_path(run_dir, output_path) or ""])
    metadata["key_object_count"] = len(key_objects)
    return make_document(
        make_doc_id("decompose", run_dir),
        "decompose",
        query_text,
        content,
        metadata,
        quality,
    )


def build_allocate_doc(
    run_dir: Path,
    manifest: Dict[str, Any],
    task_context: Dict[str, Any],
    key_objects: List[Any],
    subtasks: List[Dict[str, Any]],
    quality: str,
    sequence_operations: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    output_path = allocate_output_path(run_dir, manifest)
    output = read_text(output_path)
    if not output:
        return None

    task = str(manifest.get("task") or task_context.get("task") or "")
    robots = robot_summary(task_context.get("robots"))
    subtask_text = "\n".join(
        f"Subtask {entry.get('index')}: {entry.get('text', '').strip()}" for entry in subtasks
    )
    sequence = (
        "\n".join(sequence_operations)
        if sequence_operations is not None
        else extract_sequence_operations(output)
    )
    query_text = "\n".join(
        [
            f"Task: {task}",
            subtask_text,
            f"Robots: {compact_json(robots, 1200)}",
            f"Key objects: {compact_json(key_objects, 1200)}",
        ]
    )
    content = "\n".join(
        [
            "# Task",
            task,
            "",
            "# Subtasks",
            subtask_text,
            "",
            "# Robots",
            compact_json(robots),
            "",
            "# Key Objects",
            compact_json(key_objects),
            "",
            "# Allocation Output",
            output,
            "",
            "# Sequence of Operations",
            sequence,
        ]
    )
    metadata = base_metadata(manifest, run_dir, quality, [source_path(run_dir, output_path) or ""])
    metadata["subtask_count"] = len(subtasks)
    metadata["robot_count"] = len(robots)
    return make_document(
        make_doc_id("allocate", run_dir),
        "allocate",
        query_text,
        content,
        metadata,
        quality,
    )


def read_raw_problem(run_dir: Path, subtask_index: int, generated_problem: str) -> Tuple[str, Optional[str]]:
    path = run_dir / "05_problem_generation" / "outputs" / f"subtask_{subtask_index:02d}_problem.pddl"
    text = read_text(path)
    if text is not None:
        return text, source_path(run_dir, path)
    return generated_problem, None


def read_plan_text(run_dir: Path, planner_record: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    if not planner_record:
        return "", None
    output_path = planner_record.get("compatibility_output")
    if not isinstance(output_path, str) or not output_path:
        return "", None
    path = Path(output_path)
    if not path.is_absolute():
        path = run_dir / output_path
    text = read_text(path)
    return (text or ""), source_path(run_dir, path) if text is not None else None


def pddl_robot_from_domain_file(domain_file: Any) -> Optional[str]:
    if not isinstance(domain_file, str) or not domain_file.strip():
        return None
    stem = Path(domain_file).stem.strip()
    return stem or None


def pddl_robot_from_problem(problem_text: str) -> Optional[str]:
    match = re.search(r"\(:domain\s+([^\s)]+)", problem_text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def build_problem_generation_docs(
    run_dir: Path,
    manifest: Dict[str, Any],
    task_context: Dict[str, Any],
    subtasks: List[Dict[str, Any]],
    generated_problem_map: Dict[int, str],
    validation_records: Dict[int, Dict[str, Any]],
    planner_record_map: Dict[int, Dict[str, Any]],
    key_object_states: Any,
    quality: str,
    skip_reasons: Counter,
) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    task = str(manifest.get("task") or task_context.get("task") or "")
    robots = robot_summary(task_context.get("robots"))

    for subtask in subtasks:
        subtask_index = numeric_int(subtask.get("index"))
        if subtask_index is None:
            skip_reasons["filtered_problem_generation_invalid_subtask_index"] += 1
            continue
        subtask_text = str(subtask.get("text") or "")
        generated_problem = generated_problem_map.get(subtask_index, "")
        raw_problem, raw_problem_path = read_raw_problem(run_dir, subtask_index, generated_problem)
        validation_record = validation_records.get(subtask_index)
        planner_record = planner_record_map.get(subtask_index)
        plan_text, plan_path = read_plan_text(run_dir, planner_record)
        if not plan_text.strip():
            skip_reasons["filtered_problem_generation_missing_plan"] += 1
            continue
        if not raw_problem:
            skip_reasons["missing_problem_generation_problem"] += 1
            continue

        assigned_pddl_robot = (
            pddl_robot_from_domain_file((planner_record or {}).get("domain_file"))
            or pddl_robot_from_problem(raw_problem)
            or pddl_robot_from_problem(generated_problem)
        )
        source_paths = [
            str(path)
            for path in [subtask.get("path"), raw_problem_path, plan_path]
            if path
        ]
        query_text = "\n".join(
            [
                f"Task: {task}",
                f"Subtask {subtask_index}: {subtask_text.strip()}",
                f"Assigned Robot: {assigned_pddl_robot or ''}",
                f"Robots: {compact_json(robots, 1200)}",
                f"Key object PDDL states: {compact_json(key_object_states, 1200)}",
            ]
        )
        content = "\n".join(
            [
                "# Task",
                task,
                "",
                f"# Subtask {subtask_index}",
                subtask_text,
                "",
                "# Key Object PDDL States",
                compact_json(key_object_states),
                "",
                "# Assigned Robot",
                assigned_pddl_robot or "",
                "",
                "# Generated Problem",
                raw_problem,
            ]
        )
        metadata = base_metadata(manifest, run_dir, quality, source_paths)
        metadata.update(
            {
                "subtask_index": subtask_index,
                "assigned_pddl_robot": assigned_pddl_robot,
                "planner_return_code": (planner_record or {}).get("return_code"),
                "planner_status": (planner_record or {}).get("status"),
                "domain_file": (planner_record or {}).get("domain_file"),
                "plan_path": plan_path,
                "validation_status": (validation_record or {}).get("status"),
            }
        )
        docs.append(
            make_document(
                make_doc_id("problem_generation", run_dir, str(subtask_index)),
                "problem_generation",
                query_text,
                content,
                metadata,
                quality,
            )
        )
    return docs


def tokenize(text: str) -> Counter:
    tokens = [token.lower() for token in TOKEN_RE.findall(text)]
    return Counter(token for token in tokens if len(token) > 1)


def build_index(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    postings: Dict[str, List[List[Any]]] = defaultdict(list)
    doc_metadata: Dict[str, Dict[str, Any]] = {}
    for doc in documents:
        doc_id = str(doc["id"])
        tokens = tokenize(f"{doc.get('query_text', '')}\n{doc.get('content', '')}")
        for token, count in sorted(tokens.items()):
            postings[token].append([doc_id, count])
        doc_metadata[doc_id] = {
            "stage": doc.get("stage"),
            "quality": doc.get("quality"),
            "retrieval_eligible": doc.get("retrieval_eligible"),
            "task": safe_dict(doc.get("metadata")).get("task"),
            "task_run_dir": safe_dict(doc.get("metadata")).get("task_run_dir"),
        }

    return {
        "version": 1,
        "tokenizer": "lowercase alnum underscore tokens; min length 2",
        "document_count": len(documents),
        "documents": doc_metadata,
        "postings": dict(sorted(postings.items())),
    }


def discover_manifests(logs_root: Path, limit_runs: Optional[int] = None) -> List[Path]:
    manifests = sorted(path for path in logs_root.rglob("run_manifest.json") if path.is_file())
    if limit_runs is not None:
        return manifests[: max(0, limit_runs)]
    return manifests


def build_documents_for_manifest(
    manifest_path: Path,
    stages: Set[str],
    skip_reasons: Counter,
) -> List[Dict[str, Any]]:
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        skip_reasons["invalid_manifest"] += 1
        return []

    run_dir = manifest_path.parent
    task_context = load_task_context(run_dir, manifest)
    key_objects = load_key_objects(run_dir, manifest)
    key_object_states = load_key_object_states(run_dir, manifest)
    subtasks = load_subtasks(run_dir, manifest)
    generated_problem_map = load_generated_problem_map(run_dir, manifest)
    validation_records = load_validation_records(run_dir, manifest)
    planner_records = load_planner_records(run_dir, manifest)
    planner_record_map = planner_records_by_subtask(planner_records)
    quality = classify_quality(manifest, planner_records)

    documents: List[Dict[str, Any]] = []
    if "decompose" in stages:
        doc = build_decompose_doc(run_dir, manifest, task_context, key_objects, quality)
        if doc:
            documents.append(doc)
        else:
            skip_reasons["missing_decompose_output"] += 1

    if "allocate" in stages:
        output = read_text(allocate_output_path(run_dir, manifest))
        if not output:
            skip_reasons["missing_allocate_output"] += 1
            doc = None
        else:
            robots = robot_summary(task_context.get("robots"))
            valid, reason, sequence_operations, _ = validate_allocate_candidate(
                output,
                subtasks,
                robots,
                quality,
            )
            if valid:
                doc = build_allocate_doc(
                    run_dir,
                    manifest,
                    task_context,
                    key_objects,
                    subtasks,
                    quality,
                    sequence_operations=sequence_operations,
                )
                if not doc:
                    skip_reasons["missing_allocate_output"] += 1
            else:
                skip_reasons[f"filtered_allocate_{reason}"] += 1
                doc = None
        if doc:
            documents.append(doc)

    if "problem_generation" in stages:
        previous_skip_count = sum(skip_reasons.values())
        problem_docs = build_problem_generation_docs(
            run_dir,
            manifest,
            task_context,
            subtasks,
            generated_problem_map,
            validation_records,
            planner_record_map,
            key_object_states,
            quality,
            skip_reasons,
        )
        if problem_docs:
            documents.extend(problem_docs)
        elif sum(skip_reasons.values()) == previous_skip_count:
            skip_reasons["missing_problem_generation_outputs"] += 1

    return documents


def write_corpus(path: Path, documents: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for doc in documents:
            file_obj.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")


def summarize(
    documents: List[Dict[str, Any]],
    manifest_count: int,
    skip_reasons: Counter,
    corpus_path: Path,
    index_path: Path,
    summary_path: Path,
) -> Dict[str, Any]:
    by_stage_quality: Dict[str, Counter] = defaultdict(Counter)
    for doc in documents:
        by_stage_quality[str(doc.get("stage"))][str(doc.get("quality"))] += 1

    return {
        "version": 1,
        "manifest_count": manifest_count,
        "document_count": len(documents),
        "retrieval_eligible_count": sum(1 for doc in documents if doc.get("retrieval_eligible")),
        "by_stage_quality": {
            stage: dict(sorted(counter.items()))
            for stage, counter in sorted(by_stage_quality.items())
        },
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "outputs": {
            "corpus_path": str(corpus_path),
            "index_path": str(index_path),
            "summary_path": str(summary_path),
        },
    }


def build_corpus(
    logs_root: Path,
    stages: Set[str],
    limit_runs: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    skip_reasons: Counter = Counter()
    manifest_paths = discover_manifests(logs_root, limit_runs)
    documents: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for manifest_path in manifest_paths:
        for doc in build_documents_for_manifest(manifest_path, stages, skip_reasons):
            doc_id = str(doc.get("id"))
            if doc_id in seen_ids:
                skip_reasons["duplicate_doc_id"] += 1
                continue
            seen_ids.add(doc_id)
            documents.append(doc)

    return documents, {
        "manifest_count": len(manifest_paths),
        "skip_reasons": skip_reasons,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    base_path = Path(args.base_path).expanduser().resolve()
    run_config = load_run_config(base_path)
    output_dir = resolve_path(base_path, args.output_dir, base_path / "data" / "rag")
    stage = args.stage
    default_prefix = f"task_{stage}"
    corpus_path = resolve_path(base_path, args.corpus_path, output_dir / f"{default_prefix}_corpus.jsonl")
    index_path = resolve_path(base_path, args.index_path, output_dir / f"{default_prefix}_index.json")
    summary_path = resolve_path(base_path, args.summary_path, output_dir / f"{default_prefix}_summary.json")
    logs_root = resolve_path(base_path, args.logs_root, run_config.storage_base_dir)

    documents, stats = build_corpus(logs_root, {stage}, args.limit_runs)
    index = build_index(documents)
    summary = summarize(
        documents,
        stats["manifest_count"],
        stats["skip_reasons"],
        corpus_path,
        index_path,
        summary_path,
    )

    write_corpus(corpus_path, documents)
    write_json(index_path, index)
    write_json(summary_path, summary)

    print(
        f"Wrote {len(documents)} documents from {stats['manifest_count']} runs to {corpus_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
