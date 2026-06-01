#!/usr/bin/env python3
"""Generate DPO preference JSONL from repeated parallel run artifacts.

Edit SOURCE_SUMMARY_FILES, OUTPUT_JSONL_PATH, and STAGES_TO_INCLUDE below
before running this script.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]

# Configure concrete source files here.
SOURCE_SUMMARY_FILES = [
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260517_140101" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260518_145253" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260518_214413" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260520_162612" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260520_204726" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260520_225427" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260521_151830" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260522_102424" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260524_195257" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260525_141235" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260526_143831" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260526_194352" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260527_135853" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260527_151453" / "summary.json"),
    str(REPO_ROOT / "parallel_runs" / "pddlrun_llmseparate_20260528_153620" / "summary.json"),
]

# Configure the output file here.
OUTPUT_JSONL_PATH = str(REPO_ROOT / "data" / "dpo" / "dpo_preferences.jsonl")
OUTPUT_FORMAT = "ms_swift"
SYSTEM_MESSAGE = None

# Configure the stages to include here.
STAGES_TO_INCLUDE = [
    "02_allocate"
]

PROMPT_SIMILARITY_THRESHOLD = 0.90
MAX_EXACT_PROMPT_SIMILARITY_CHARS = 1000
PROMPT_SIMILARITY_SAMPLE_CHARS = 2000

SINGLE_FILE_STAGES = {
    "01_decompose": {
        "prompt": "01_decompose/01_decompose_prompt.txt",
        "output": "01_decompose/02_decompose_output.txt",
    },
    "02_allocate": {
        "prompt": "02_allocate/01_allocate_prompt.txt",
        "output": "02_allocate/02_allocate_output.txt",
    },
}

DIRECTORY_STAGES = {
    "05_problem_generation": {
        "prompt_dir": "05_problem_generation/prompts",
        "output_dir": "05_problem_generation/outputs",
        "prompt_suffix": "_prompt.txt",
        "output_suffix": "_problem.pddl",
    },
    "07_validate": {
        "prompt_dir": "07_validate/prompts",
        "output_dir": "07_validate/outputs",
        "prompt_suffix": "_prompt.txt",
        "output_suffix": "_validated.pddl",
    },
}

TaskKey = Tuple[str, str, int]


@dataclass(frozen=True)
class Candidate:
    task_key: TaskKey
    task_text: str
    stage: str
    slot: str
    prompt: str
    output: str
    score: float
    source_summary: str
    task_run_dir: str
    model: str


@dataclass
class GenerationStats:
    summary_files_read: int = 0
    results_seen: int = 0
    candidates_seen: int = 0
    prompt_groups_considered: int = 0
    examples_written: int = 0
    skipped_missing_summary: int = 0
    skipped_missing_task_run_dir: int = 0
    skipped_missing_artifact: int = 0
    skipped_task_conflicts: int = 0
    skipped_same_score: int = 0
    skipped_same_output: int = 0
    skipped_no_pair: int = 0
    skipped_unknown_stage: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "summary_files_read": self.summary_files_read,
            "results_seen": self.results_seen,
            "candidates_seen": self.candidates_seen,
            "prompt_groups_considered": self.prompt_groups_considered,
            "examples_written": self.examples_written,
            "skipped_missing_summary": self.skipped_missing_summary,
            "skipped_missing_task_run_dir": self.skipped_missing_task_run_dir,
            "skipped_missing_artifact": self.skipped_missing_artifact,
            "skipped_task_conflicts": self.skipped_task_conflicts,
            "skipped_same_score": self.skipped_same_score,
            "skipped_same_output": self.skipped_same_output,
            "skipped_no_pair": self.skipped_no_pair,
            "skipped_unknown_stage": self.skipped_unknown_stage,
        }


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(path: str, base_path: Path = REPO_ROOT) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return (base_path / resolved).resolve()


def normalize_floor_plan(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]
    return text


def normalize_task_text(value: str) -> str:
    return " ".join(value.casefold().split())


def normalize_prompt(value: str) -> str:
    return " ".join(value.split())


def prompt_similarity_text(value: str) -> str:
    if len(value) <= PROMPT_SIMILARITY_SAMPLE_CHARS:
        return normalize_prompt(value)

    half = PROMPT_SIMILARITY_SAMPLE_CHARS // 2
    return normalize_prompt(value[:half] + "\n...\n" + value[-half:])


def prompt_similarity(prompt_a: str, prompt_b: str) -> float:
    normalized_a = prompt_similarity_text(prompt_a)
    normalized_b = prompt_similarity_text(prompt_b)
    if not normalized_a and not normalized_b:
        return 1.0
    return SequenceMatcher(None, normalized_a, normalized_b).ratio()


def prompts_are_similar(normalized_a: str, normalized_b: str, threshold: float) -> bool:
    if normalized_a == normalized_b:
        return True
    if not normalized_a or not normalized_b:
        return False

    shorter = min(len(normalized_a), len(normalized_b))
    longer = max(len(normalized_a), len(normalized_b))
    if shorter / longer < threshold:
        return False

    matcher = SequenceMatcher(None, normalized_a, normalized_b)
    if matcher.quick_ratio() < threshold:
        return False

    # Full SequenceMatcher.ratio() can be expensive for long PDDL prompts. We
    # compare sampled prompt text, and after task/stage/slot matching quick_ratio
    # is a practical guard for large nearly-identical prompts.
    if len(normalized_a) + len(normalized_b) > MAX_EXACT_PROMPT_SIMILARITY_CHARS:
        return True

    return matcher.ratio() >= threshold


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_subtask_slot(value: str) -> Optional[str]:
    match = re.search(r"subtask[_-](\d+)", value, flags=re.IGNORECASE)
    if not match:
        return None
    return f"subtask_{int(match.group(1)):02d}"


def flatten_summary_results(summary_data: Dict[str, Any]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for floor_summary in summary_data.get("summaries", []):
        if not isinstance(floor_summary, dict):
            continue
        for result in floor_summary.get("results", []):
            if isinstance(result, dict):
                yield floor_summary, result

    for result in summary_data.get("results", []):
        if isinstance(result, dict):
            yield {}, result


def data_root_for_summary(summary_data: Dict[str, Any], data_root: str = "data") -> Path:
    data_root_path = Path(data_root).expanduser()
    if data_root_path.is_absolute():
        return data_root_path

    repo_root = summary_data.get("repo_root")
    if repo_root:
        return Path(str(repo_root)).expanduser() / data_root_path

    return REPO_ROOT / data_root_path


def expected_subtask_count(
    summary_data: Dict[str, Any],
    test_set: str,
    floor_plan: str,
    task_index: int,
    cache: Dict[Tuple[str, str, str, int], Optional[int]],
) -> Optional[int]:
    repo_root = str(summary_data.get("repo_root") or REPO_ROOT)
    cache_key = (repo_root, test_set, floor_plan, task_index)
    if cache_key in cache:
        return cache[cache_key]

    count: Optional[int] = None
    if test_set:
        jsonl_path = data_root_for_summary(summary_data) / test_set / f"FloorPlan{floor_plan}.jsonl"
        if jsonl_path.exists():
            with jsonl_path.open("r", encoding="utf-8") as handle:
                for idx, raw_line in enumerate(handle):
                    line = raw_line.strip()
                    if not line:
                        continue
                    if idx == task_index:
                        record = json.loads(line)
                        subtasks = record.get("subtasks")
                        if isinstance(subtasks, list):
                            count = len(subtasks)
                        break

    cache[cache_key] = count
    return count


def task_score(
    summary_data: Dict[str, Any],
    result: Dict[str, Any],
    test_set: str,
    floor_plan: str,
    task_index: int,
    expected_count_cache: Dict[Tuple[str, str, str, int], Optional[int]],
) -> float:
    expected_count = expected_subtask_count(
        summary_data,
        test_set,
        floor_plan,
        task_index,
        expected_count_cache,
    )
    if expected_count is None:
        expected_count = safe_int(result.get("total"))

    if expected_count <= 0:
        return 0.0

    return safe_int(result.get("tc")) / expected_count


def planner_scores_by_slot(task_run_dir: Path) -> Dict[str, float]:
    manifest_path = task_run_dir / "08_planner" / "planner_manifest.json"
    if not manifest_path.exists():
        return {}

    try:
        manifest = read_json(manifest_path)
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(manifest, list):
        return {}

    scores: Dict[str, float] = {}
    for record in manifest:
        if not isinstance(record, dict):
            continue
        slot = extract_subtask_slot(str(record.get("problem_file") or record.get("compatibility_output") or ""))
        if not slot:
            continue
        score = 1.0 if safe_int(record.get("return_code"), default=1) == 0 else 0.0
        scores[slot] = max(scores.get(slot, 0.0), score)

    return scores


def collect_candidates(
    source_summary_files: Sequence[str],
    stages_to_include: Sequence[str],
    stats: GenerationStats,
) -> List[Candidate]:
    candidates: List[Candidate] = []
    expected_count_cache: Dict[Tuple[str, str, str, int], Optional[int]] = {}

    for summary_file in source_summary_files:
        summary_path = resolve_path(summary_file)
        if not summary_path.exists():
            stats.skipped_missing_summary += 1
            continue

        summary_data = read_json(summary_path)
        stats.summary_files_read += 1
        test_set = str(summary_data.get("test_set") or "")

        for floor_summary, result in flatten_summary_results(summary_data):
            stats.results_seen += 1
            task_run_dir_value = result.get("task_run_dir")
            if not task_run_dir_value:
                stats.skipped_missing_task_run_dir += 1
                continue

            task_run_dir_base = Path(str(summary_data.get("repo_root") or summary_path.parent))
            task_run_dir = resolve_path(str(task_run_dir_value), task_run_dir_base)
            if not task_run_dir.is_dir():
                stats.skipped_missing_task_run_dir += 1
                continue

            floor_plan = normalize_floor_plan(result.get("floor_plan") or floor_summary.get("floor_plan"))
            if not floor_plan:
                stats.skipped_missing_task_run_dir += 1
                continue

            if "task_index" not in result:
                stats.skipped_missing_task_run_dir += 1
                continue
            task_index = safe_int(result.get("task_index"), default=-1)
            if task_index < 0:
                stats.skipped_missing_task_run_dir += 1
                continue

            key: TaskKey = (test_set, floor_plan, task_index)
            task_text = str(result.get("task") or "")
            base_score = task_score(summary_data, result, test_set, floor_plan, task_index, expected_count_cache)
            model = str(result.get("model") or "")
            planner_scores: Optional[Dict[str, float]] = None

            for stage in stages_to_include:
                if stage in SINGLE_FILE_STAGES:
                    definition = SINGLE_FILE_STAGES[stage]
                    prompt_path = task_run_dir / definition["prompt"]
                    output_path = task_run_dir / definition["output"]
                    if not prompt_path.exists() or not output_path.exists():
                        stats.skipped_missing_artifact += 1
                        continue
                    candidates.append(Candidate(
                        task_key=key,
                        task_text=task_text,
                        stage=stage,
                        slot="single",
                        prompt=read_text(prompt_path),
                        output=read_text(output_path),
                        score=base_score,
                        source_summary=str(summary_path),
                        task_run_dir=str(task_run_dir),
                        model=model,
                    ))
                    stats.candidates_seen += 1
                    continue

                if stage in DIRECTORY_STAGES:
                    if planner_scores is None:
                        planner_scores = planner_scores_by_slot(task_run_dir)
                    definition = DIRECTORY_STAGES[stage]
                    prompt_dir = task_run_dir / definition["prompt_dir"]
                    output_dir = task_run_dir / definition["output_dir"]
                    if not prompt_dir.is_dir() or not output_dir.is_dir():
                        stats.skipped_missing_artifact += 1
                        continue

                    prompt_suffix = str(definition["prompt_suffix"])
                    output_suffix = str(definition["output_suffix"])
                    for prompt_path in sorted(prompt_dir.glob(f"*{prompt_suffix}")):
                        output_name = prompt_path.name[: -len(prompt_suffix)] + output_suffix
                        output_path = output_dir / output_name
                        if not output_path.exists():
                            stats.skipped_missing_artifact += 1
                            continue
                        slot = extract_subtask_slot(prompt_path.name) or prompt_path.stem
                        candidates.append(Candidate(
                            task_key=key,
                            task_text=task_text,
                            stage=stage,
                            slot=slot,
                            prompt=read_text(prompt_path),
                            output=read_text(output_path),
                            score=planner_scores.get(slot, base_score),
                            source_summary=str(summary_path),
                            task_run_dir=str(task_run_dir),
                            model=model,
                        ))
                        stats.candidates_seen += 1
                    continue

                stats.skipped_unknown_stage += 1

    return candidates


def cluster_by_prompt_similarity(candidates: Sequence[Candidate], threshold: float) -> List[List[Candidate]]:
    if not candidates:
        return []

    normalized_prompts = [prompt_similarity_text(candidate.prompt) for candidate in candidates]
    adjacency: DefaultDict[int, List[int]] = defaultdict(list)
    for left_idx in range(len(candidates)):
        for right_idx in range(left_idx + 1, len(candidates)):
            if prompts_are_similar(normalized_prompts[left_idx], normalized_prompts[right_idx], threshold):
                adjacency[left_idx].append(right_idx)
                adjacency[right_idx].append(left_idx)

    clusters: List[List[Candidate]] = []
    visited = set()
    for start_idx in range(len(candidates)):
        if start_idx in visited:
            continue

        stack = [start_idx]
        visited.add(start_idx)
        cluster_indexes: List[int] = []
        while stack:
            idx = stack.pop()
            cluster_indexes.append(idx)
            for neighbor_idx in adjacency[idx]:
                if neighbor_idx not in visited:
                    visited.add(neighbor_idx)
                    stack.append(neighbor_idx)

        clusters.append([candidates[idx] for idx in sorted(cluster_indexes)])

    return clusters


def build_preference_example(candidates: Sequence[Candidate]) -> Optional[Dict[str, str]]:
    high_to_low = sorted(
        candidates,
        key=lambda item: (item.score, item.source_summary, item.task_run_dir, item.model),
        reverse=True,
    )
    low_to_high = sorted(
        candidates,
        key=lambda item: (item.score, item.source_summary, item.task_run_dir, item.model),
    )

    for chosen in high_to_low:
        for rejected in low_to_high:
            if chosen is rejected:
                continue
            if chosen.score <= rejected.score:
                continue
            if chosen.output.strip() == rejected.output.strip():
                continue
            return {
                "prompt": chosen.prompt,
                "chosen": chosen.output,
                "rejected": rejected.output,
            }

    return None


def generate_dpo_examples(
    source_summary_files: Sequence[str],
    stages_to_include: Sequence[str] = STAGES_TO_INCLUDE,
    prompt_similarity_threshold: float = PROMPT_SIMILARITY_THRESHOLD,
) -> Tuple[List[Dict[str, str]], GenerationStats]:
    stats = GenerationStats()
    candidates = collect_candidates(source_summary_files, stages_to_include, stats)

    candidates_by_task: DefaultDict[TaskKey, List[Candidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_task[candidate.task_key].append(candidate)

    examples: List[Dict[str, str]] = []
    for task_candidates in candidates_by_task.values():
        normalized_tasks = {
            normalize_task_text(candidate.task_text)
            for candidate in task_candidates
            if normalize_task_text(candidate.task_text)
        }
        if len(normalized_tasks) > 1:
            stats.skipped_task_conflicts += 1
            continue

        buckets: DefaultDict[Tuple[str, str], List[Candidate]] = defaultdict(list)
        for candidate in task_candidates:
            buckets[(candidate.stage, candidate.slot)].append(candidate)

        for bucket_candidates in buckets.values():
            for prompt_group in cluster_by_prompt_similarity(bucket_candidates, prompt_similarity_threshold):
                if len(prompt_group) < 2:
                    continue

                stats.prompt_groups_considered += 1
                example = build_preference_example(prompt_group)
                if example is not None:
                    examples.append(example)
                    stats.examples_written += 1
                    continue

                if len({candidate.score for candidate in prompt_group}) <= 1:
                    stats.skipped_same_score += 1
                elif len({candidate.output.strip() for candidate in prompt_group}) <= 1:
                    stats.skipped_same_output += 1
                else:
                    stats.skipped_no_pair += 1

    return examples, stats


def format_dpo_example(
    example: Dict[str, str],
    output_format: str,
    system_message: Optional[str] = None,
) -> Dict[str, Any]:
    if output_format == "trl":
        return {
            "prompt": example["prompt"],
            "chosen": example["chosen"],
            "rejected": example["rejected"],
        }

    if output_format == "ms_swift":
        messages = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.extend([
            {"role": "user", "content": example["prompt"]},
            {"role": "assistant", "content": example["chosen"]},
        ])
        return {
            "messages": messages,
            "rejected_response": example["rejected"],
        }

    raise ValueError(f"Unsupported DPO output format: {output_format}")


def write_dpo_jsonl(
    examples: Sequence[Dict[str, str]],
    output_path: str,
    output_format: str = OUTPUT_FORMAT,
    system_message: Optional[str] = SYSTEM_MESSAGE,
) -> None:
    path = resolve_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for example in examples:
            formatted = format_dpo_example(example, output_format, system_message)
            handle.write(json.dumps(formatted, ensure_ascii=False) + "\n")


def main() -> None:
    examples, stats = generate_dpo_examples(
        SOURCE_SUMMARY_FILES,
        stages_to_include=STAGES_TO_INCLUDE,
        prompt_similarity_threshold=PROMPT_SIMILARITY_THRESHOLD,
    )
    write_dpo_jsonl(
        examples,
        OUTPUT_JSONL_PATH,
        output_format=OUTPUT_FORMAT,
        system_message=SYSTEM_MESSAGE,
    )

    print(f"Wrote {len(examples)} DPO example(s) to {resolve_path(OUTPUT_JSONL_PATH)}")
    for key, value in stats.as_dict().items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
