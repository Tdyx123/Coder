"""Shared helpers for plan-to-code converters."""

from __future__ import annotations

import json
import py_compile
import re
from pathlib import Path
from pprint import pformat
from typing import Any, Dict, List, Optional, Sequence


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parent


def normalize_floor_plan(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("floorplan"):
        return text[len("FloorPlan") :]
    return text


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compile_python(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def render_bundle_literal(bundle_data: Dict[str, Any]) -> str:
    return pformat(bundle_data, width=100, sort_dicts=False)


def build_bundle_data(
    *,
    task: str,
    task_plan_data: Dict[str, Any],
    gcr: Sequence[Any],
    no_trans: int,
    object_mappings: Dict[str, str],
    object_mapping_warnings: Sequence[str],
    phases: Sequence[Any] = (),
    object_id_bindings: Sequence[Any] = (),
) -> Dict[str, Any]:
    return {
        "task": task,
        "task_plan": task_plan_data,
        "gcr": list(gcr),
        "no_trans": no_trans,
        "phases": list(phases),
        "object_mappings": dict(object_mappings),
        "object_mapping_warnings": list(object_mapping_warnings),
        "object_id_bindings": list(object_id_bindings),
    }


def render_executable_plan(
    *,
    bundle_data: Dict[str, Any],
    task_file: Path,
    task_index: int,
    description: str,
    repo_root: Optional[Path] = None,
) -> str:
    bundle_literal = render_bundle_literal(bundle_data)
    code_repo_root = str(repo_root or REPO_ROOT)
    return f'''#!/usr/bin/env python3
"""{description}"""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path({code_repo_root!r})
_SCRIPT_DIR = REPO_ROOT / "scripts"
for path in (_SCRIPT_DIR, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

RUNNER_MODE_ARG = "--runner-mode"
if RUNNER_MODE_ARG in sys.argv[1:]:
    os.environ["renderImage"] = "0"

from executor_system.generated_plan_runtime import main as run_generated_plan


BUNDLE_DATA = {bundle_literal}

TASK_FILE = {str(task_file)!r}
TASK_INDEX = {task_index!r}


if __name__ == "__main__":
    try:
        raise SystemExit(run_generated_plan(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__))
    except RuntimeError as exc:
        print(f"ERROR: {{exc}}")
        raise SystemExit(1)
'''


def write_plan_to_code_summary(
    results: Sequence[Dict[str, Any]],
    output_dir: Path,
    *,
    dry_run: bool = False,
    include_dry_run: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    total = len(results)
    successful = sum(1 for result in results if result.get("success"))
    summary: Dict[str, Any] = {
        "total_results": total,
        "successful_generations": successful,
        "failed_generations": total - successful,
        "success_rate": successful / total * 100 if total else 0,
        "total_generation_time": sum(float(result.get("generation_time", 0)) for result in results),
    }
    if include_dry_run:
        summary["dry_run"] = dry_run
    if extra:
        summary.update(extra)

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "plan_to_code_summary.json", summary)
        write_json(output_dir / "plan_to_code_results.json", list(results))
    return summary


def parse_objects_ai(objects_ai: str) -> List[str]:
    if not objects_ai:
        return []

    text = objects_ai.strip()
    if text.startswith("objects"):
        _prefix, _sep, text = text.partition("=")
        text = text.strip()

    try:
        import ast

        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    return object_names_from_items(parsed)


def object_names_from_items(items: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("objectType") or item.get("objectId")
        else:
            name = item
        if name:
            names.append(str(name))
    return names


def load_object_names(
    data_repo_root: Path,
    floor_plan: str,
    task_context: Dict[str, Any],
) -> List[str]:
    names = parse_objects_ai(str(task_context.get("objects_ai", "")))
    if names:
        return names

    cache_path = (
        data_repo_root
        / "data"
        / "ai2thor_objects_cache"
        / f"FloorPlan{normalize_floor_plan(floor_plan)}.json"
    )
    if not cache_path.exists():
        return []

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    if not isinstance(cached, list):
        return []
    return object_names_from_items(cached)


def sanitize_slug(value: str, max_length: int = 96) -> str:
    slug = re.sub(r'[<>:"/\\|?*\s]+', "_", str(value).strip())
    slug = slug.strip("._")
    if not slug:
        slug = "task"
    return slug[:max_length].rstrip("._") or "task"
