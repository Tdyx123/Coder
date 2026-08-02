#!/usr/bin/env python3
"""Audit semantic precedence in matched task-decomposition records.

The audit deliberately separates three concepts:

* semantic precedence: an explicit temporal relation in the task text;
* resource serialization: two subtasks assigned to the same robot;
* executable scheduling: stage lines in the allocation artifact.

For the current generated task language, ``then`` is treated as a hard semantic
barrier. Commas and ``and`` inside one side of a barrier do not create a hard
order. No PDDL preconditions are consulted.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from parsing_utils import ParsingUtils


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = Path("data/grpo/deepseek_v3_2_decomposition_matches.jsonl")
DEFAULT_JSON_OUTPUT = Path("data/grpo/deepseek_v3_2_ordering_backtest.json")
DEFAULT_MARKDOWN_OUTPUT = Path("reports/deepseek_v3_2_ordering_backtest.md")
ALLOCATION_OUTPUT = Path("02_allocate/02_allocate_output.txt")

Edge = Tuple[int, int]

SKILL_ACTION_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "Break": (r"\bbreak\b", r"\bsmash\b"),
    "ColdObject": (r"\bcool\b", r"\bchill\b"),
    "CookByStoveBurner": (r"\bcook\b",),
    "FillWater": (r"\bfill\b",),
    "HeatByStoveBurner": (r"\bheat\b",),
    "Open": (r"\bopen\b",),
    "PutIn": (r"\bput\b", r"\bplace\b"),
    "PutOn": (r"\bput\b", r"\bplace\b"),
    "RunToaster": (r"\btoast\b",),
    "Slice": (r"\bslice\b", r"\bcut\b"),
    "SwitchOn": (
        r"\bswitch\s+on\b",
        r"\bturn\s+on\b",
        r"\bswitch\s+(?:it|them)\s+on\b",
        r"\bturn\s+(?:it|them)\s+on\b",
    ),
    "Wash": (r"\bwash\b", r"\bclean\b"),
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "in",
    "into",
    "on",
    "the",
    "then",
    "to",
}


@dataclass(frozen=True)
class SubtaskView:
    decomposed_index: int
    title: str
    task_subtask_index: int
    skill: str
    objects: Tuple[str, ...]
    reference_robot: Optional[int]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decomposed_index": self.decomposed_index,
            "title": self.title,
            "task_subtask_index": self.task_subtask_index,
            "skill": self.skill,
            "objects": list(self.objects),
            "reference_robot": self.reference_robot,
        }


@dataclass
class SemanticStages:
    clauses: List[str]
    stages: List[List[int]]
    stage_by_subtask: Dict[int, int]
    scores: Dict[int, List[int]]
    issues: List[str]

    @property
    def direct_edges(self) -> Set[Edge]:
        edges: Set[Edge] = set()
        for left, right in zip(self.stages, self.stages[1:]):
            edges.update((source, target) for source in left for target in right)
        return edges

    @property
    def closure_edges(self) -> Set[Edge]:
        edges: Set[Edge] = set()
        for left_stage in range(len(self.stages)):
            for right_stage in range(left_stage + 1, len(self.stages)):
                edges.update(
                    (source, target)
                    for source in self.stages[left_stage]
                    for target in self.stages[right_stage]
                )
        return edges


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Matched decomposition JSONL.")
    parser.add_argument(
        "--output-json",
        default=str(DEFAULT_JSON_OUTPUT),
        help="Detailed machine-readable audit output.",
    )
    parser.add_argument(
        "--output-markdown",
        default=str(DEFAULT_MARKDOWN_OUTPUT),
        help="Reader-facing Markdown report.",
    )
    return parser


def resolve_path(value: str | Path, repo_root: Path = REPO_ROOT) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo_root / path


def normalized_text(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())


def normalized_task_text(value: Any) -> str:
    return normalized_text(value)


def split_camel_case(value: str) -> str:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    expanded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", expanded)
    return normalized_text(expanded)


def phrase_present(text: str, value: str) -> bool:
    phrase = split_camel_case(value)
    if not phrase:
        return False
    if re.search(rf"\b{re.escape(phrase)}\b", text):
        return True
    return phrase.replace(" ", "") in text.replace(" ", "")


def split_semantic_clauses(task: str) -> List[str]:
    clauses = [
        normalized_text(part)
        for part in re.split(r"\bthen\b", task, flags=re.IGNORECASE)
    ]
    return [clause for clause in clauses if clause]


def action_score(skill: str, clause: str, primary_object: str) -> int:
    if skill == "RunMicrowave":
        primary = split_camel_case(primary_object)
        patterns = (
            rf"\bmicrowave\s+(?:the\s+)?{re.escape(primary)}\b",
            r"(?:^|\band\s+)\s*microwave\b",
        )
    else:
        patterns = SKILL_ACTION_PATTERNS.get(skill, ())
    return 10 if any(re.search(pattern, clause) for pattern in patterns) else 0


def clause_score(subtask: SubtaskView, clause: str) -> int:
    primary = subtask.objects[0] if subtask.objects else ""
    score = action_score(subtask.skill, clause, primary)
    for object_index, object_name in enumerate(subtask.objects):
        if phrase_present(clause, object_name):
            score += 7 if object_index == 0 else 4

    title_tokens = {
        token
        for token in normalized_text(subtask.title).split()
        if token not in STOPWORDS and len(token) > 2
    }
    clause_tokens = set(clause.split())
    score += min(3, len(title_tokens & clause_tokens))
    return score


def infer_semantic_stages(task: str, subtasks: Sequence[SubtaskView]) -> SemanticStages:
    clauses = split_semantic_clauses(task)
    if not clauses:
        clauses = [normalized_text(task)]

    stage_by_subtask: Dict[int, int] = {}
    scores: Dict[int, List[int]] = {}
    issues: List[str] = []
    previous_stage = 0

    for subtask in sorted(subtasks, key=lambda item: item.decomposed_index):
        subtask_scores = [clause_score(subtask, clause) for clause in clauses]
        scores[subtask.decomposed_index] = subtask_scores
        best_score = max(subtask_scores, default=0)
        best_stages = [
            index for index, score in enumerate(subtask_scores) if score == best_score
        ]

        if best_score == 0:
            chosen_stage = min(previous_stage, len(clauses) - 1)
            issues.append(
                f"subtask {subtask.decomposed_index} has no lexical stage match; "
                f"fell back to stage {chosen_stage + 1}"
            )
        elif len(best_stages) == 1:
            chosen_stage = best_stages[0]
        else:
            monotonic_candidates = [
                stage for stage in best_stages if stage >= previous_stage
            ]
            chosen_stage = (
                monotonic_candidates[0] if monotonic_candidates else best_stages[0]
            )
            issues.append(
                f"subtask {subtask.decomposed_index} ties across stages "
                f"{[stage + 1 for stage in best_stages]}; chose stage {chosen_stage + 1}"
            )

        stage_by_subtask[subtask.decomposed_index] = chosen_stage
        previous_stage = max(previous_stage, chosen_stage)

    stages = [[] for _ in clauses]
    for subtask_index, stage_index in sorted(stage_by_subtask.items()):
        stages[stage_index].append(subtask_index)

    empty_stages = [index + 1 for index, stage in enumerate(stages) if not stage]
    if empty_stages:
        issues.append(f"semantic stages without a matched subtask: {empty_stages}")

    return SemanticStages(
        clauses=clauses,
        stages=stages,
        stage_by_subtask=stage_by_subtask,
        scores=scores,
        issues=issues,
    )


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            records.append(record)
    return records


def read_task_record(record: Dict[str, Any], repo_root: Path) -> Tuple[Dict[str, Any], Path]:
    source = record.get("source") or {}
    task_path_value = source.get("task_jsonl_path")
    if not task_path_value:
        raise ValueError(f"{record.get('id')}: source.task_jsonl_path is missing")
    task_path = resolve_path(str(task_path_value), repo_root)
    task_records = read_jsonl(task_path)
    expected_task = normalized_task_text(record.get("task"))

    raw_index = source.get("task_index")
    candidate_index: Optional[int] = None
    if isinstance(raw_index, int) and not isinstance(raw_index, bool):
        candidate_index = raw_index
    elif isinstance(raw_index, str) and raw_index.strip().isdigit():
        candidate_index = int(raw_index.strip())

    if candidate_index is not None and 0 <= candidate_index < len(task_records):
        candidate = task_records[candidate_index]
        if normalized_task_text(candidate.get("task")) == expected_task:
            return candidate, task_path

    matches = [
        item
        for item in task_records
        if normalized_task_text(item.get("task")) == expected_task
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{record.get('id')}: expected one matching task in {task_path}, "
            f"found {len(matches)}"
        )
    return matches[0], task_path


def build_subtask_views(
    record: Dict[str, Any],
    task_record: Dict[str, Any],
) -> List[SubtaskView]:
    canonical_subtasks = task_record.get("subtasks")
    assigned_robots = task_record.get("assigned_robots")
    if not isinstance(canonical_subtasks, list):
        raise ValueError(f"{record.get('id')}: task record subtasks is not a list")
    if not isinstance(assigned_robots, list):
        assigned_robots = []

    views: List[SubtaskView] = []
    for match in record.get("subtask_matches") or []:
        decomposed_index = int(match["decomposed_subtask_index"])
        canonical_index = int(match["task_subtask_index"])
        canonical_offset = canonical_index - 1
        if not (0 <= canonical_offset < len(canonical_subtasks)):
            raise ValueError(
                f"{record.get('id')}: invalid task_subtask_index {canonical_index}"
            )
        subtask = canonical_subtasks[canonical_offset]
        reference_robot: Optional[int] = None
        if canonical_offset < len(assigned_robots):
            raw_robot = assigned_robots[canonical_offset]
            if isinstance(raw_robot, int) and not isinstance(raw_robot, bool):
                reference_robot = raw_robot
        views.append(
            SubtaskView(
                decomposed_index=decomposed_index,
                title=str(match.get("decomposed_subtask_title") or "").strip(),
                task_subtask_index=canonical_index,
                skill=str(subtask.get("skill") or ""),
                objects=tuple(str(item) for item in subtask.get("objects") or []),
                reference_robot=reference_robot,
            )
        )

    views.sort(key=lambda item: item.decomposed_index)
    expected_indices = list(range(1, len(views) + 1))
    actual_indices = [item.decomposed_index for item in views]
    if actual_indices != expected_indices:
        raise ValueError(
            f"{record.get('id')}: decomposition indices {actual_indices} are not "
            f"contiguous {expected_indices}"
        )
    return views


def parse_allocation_schedule(
    record: Dict[str, Any],
    repo_root: Path,
) -> Dict[str, Any]:
    source = record.get("source") or {}
    run_dir_value = source.get("task_run_dir")
    if not run_dir_value:
        return {
            "status": "missing_task_run_dir",
            "path": None,
            "lines": [],
            "stage_by_subtask": {},
            "local_robot_by_subtask": {},
        }

    run_dir = resolve_path(str(run_dir_value), repo_root)
    allocation_path = run_dir / ALLOCATION_OUTPUT
    if not allocation_path.is_file():
        return {
            "status": "missing_allocation_output",
            "path": str(allocation_path.relative_to(repo_root)),
            "lines": [],
            "stage_by_subtask": {},
            "local_robot_by_subtask": {},
        }

    try:
        text = allocation_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {
            "status": "unreadable_allocation_output",
            "path": str(allocation_path.relative_to(repo_root)),
            "lines": [],
            "stage_by_subtask": {},
            "local_robot_by_subtask": {},
        }

    lines = ParsingUtils.extract_sequence_operations(text)
    stage_by_subtask: Dict[int, int] = {}
    local_robot_by_subtask: Dict[int, int] = {}
    assignment_re = ParsingUtils.sequence_assignment_re()
    for stage_index, line in enumerate(lines):
        for match in assignment_re.finditer(line):
            subtask_index = int(match.group(1))
            stage_by_subtask[subtask_index] = stage_index
            local_robot_by_subtask[subtask_index] = int(match.group(2))

    return {
        "status": "ok" if lines else "unparseable_sequence",
        "path": str(allocation_path.relative_to(repo_root)),
        "lines": lines,
        "stage_by_subtask": stage_by_subtask,
        "local_robot_by_subtask": local_robot_by_subtask,
    }


def edge_dict(edge: Edge, by_index: Dict[int, SubtaskView]) -> Dict[str, Any]:
    source, target = edge
    return {
        "before": source,
        "after": target,
        "before_title": by_index[source].title,
        "after_title": by_index[target].title,
        "skill_pair": f"{by_index[source].skill}->{by_index[target].skill}",
    }


def allocation_violations(
    semantic_edges: Iterable[Edge],
    stage_by_subtask: Dict[int, int],
) -> Tuple[List[Edge], List[Edge]]:
    parallel: List[Edge] = []
    reversed_edges: List[Edge] = []
    for source, target in sorted(semantic_edges):
        if source not in stage_by_subtask or target not in stage_by_subtask:
            continue
        source_stage = stage_by_subtask[source]
        target_stage = stage_by_subtask[target]
        if source_stage == target_stage:
            parallel.append((source, target))
        elif source_stage > target_stage:
            reversed_edges.append((source, target))
    return parallel, reversed_edges


def overview_text(decomposition_output: str) -> str:
    marker = re.search(
        r"action\s+description\s+from\s+domain",
        decomposition_output,
        flags=re.IGNORECASE,
    )
    return decomposition_output[: marker.start()] if marker else decomposition_output


def extract_parallel_claims(decomposition_output: str, subtask_count: int) -> List[Dict[str, Any]]:
    overview = overview_text(decomposition_output)
    claims: List[Dict[str, Any]] = []
    for raw_line in overview.splitlines():
        for raw_sentence in re.split(r"(?<=[.!?])\s+", raw_line.strip()):
            sentence = raw_sentence.strip()
            lowered = normalized_text(sentence)
            if "parallel" not in lowered:
                continue

            if re.search(
                r"\b(?:parallelize|parallelise)\s+all\b|"
                r"\ball\s+(?:the\s+)?subtasks?\b.*\bparallel",
                lowered,
            ):
                pairs = list(combinations(range(1, subtask_count + 1), 2))
                claims.append(
                    {
                        "line": sentence,
                        "kind": "all_parallel",
                        "pairs": [list(pair) for pair in pairs],
                    }
                )
                continue

            relevant = re.split(r"\bafter\b|\bbut\b", lowered, maxsplit=1)[0]
            numbers = [int(value) for value in re.findall(r"\b\d+\b", relevant)]
            numbers = list(
                dict.fromkeys(
                    value for value in numbers if 1 <= value <= subtask_count
                )
            )
            if len(numbers) >= 2:
                claims.append(
                    {
                        "line": sentence,
                        "kind": "explicit_parallel_group",
                        "pairs": [list(pair) for pair in combinations(numbers, 2)],
                    }
                )
            elif "after or in parallel" in lowered:
                claims.append(
                    {
                        "line": sentence,
                        "kind": "ambiguous_after_or_parallel",
                        "pairs": [],
                    }
                )
    return claims


def narrative_claim_violations(
    claims: Sequence[Dict[str, Any]],
    semantic_edges: Set[Edge],
) -> List[Dict[str, Any]]:
    forced_unordered = {frozenset(edge) for edge in semantic_edges}
    violations: List[Dict[str, Any]] = []
    for claim in claims:
        bad_pairs = [
            pair
            for pair in claim["pairs"]
            if frozenset(pair) in forced_unordered
        ]
        if bad_pairs or (
            claim["kind"] == "ambiguous_after_or_parallel" and semantic_edges
        ):
            violations.append({**claim, "violating_pairs": bad_pairs})
    return violations


def all_pair_keys(subtasks: Sequence[SubtaskView]) -> Set[frozenset[int]]:
    indices = [item.decomposed_index for item in subtasks]
    return {frozenset(pair) for pair in combinations(indices, 2)}


def baseline_edges(
    name: str,
    subtasks: Sequence[SubtaskView],
) -> Set[Edge]:
    edges: Set[Edge] = set()
    for left, right in combinations(subtasks, 2):
        if name == "task_file_array_order":
            if left.task_subtask_index < right.task_subtask_index:
                edges.add((left.decomposed_index, right.decomposed_index))
            else:
                edges.add((right.decomposed_index, left.decomposed_index))
        elif name == "decomposition_total_order":
            edges.add((left.decomposed_index, right.decomposed_index))
        elif name == "reference_same_robot_order":
            if (
                left.reference_robot is not None
                and left.reference_robot == right.reference_robot
            ):
                edges.add((left.decomposed_index, right.decomposed_index))
        else:
            raise ValueError(f"unknown baseline {name}")
    return edges


def metric_counts(gold: Set[Edge], predicted: Set[Edge]) -> Dict[str, int]:
    return {
        "tp": len(gold & predicted),
        "fp": len(predicted - gold),
        "fn": len(gold - predicted),
    }


def metric_summary(counts: Dict[str, int]) -> Dict[str, Any]:
    tp = counts["tp"]
    fp = counts["fp"]
    fn = counts["fn"]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        **counts,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def repair_schedule(
    semantic_stages: Sequence[Sequence[int]],
    local_robot_by_subtask: Dict[int, int],
) -> List[List[int]]:
    """Build barrier-respecting waves while avoiding one robot twice per wave."""
    waves: List[List[int]] = []
    for semantic_stage in semantic_stages:
        stage_waves: List[List[int]] = []
        robots_by_wave: List[Set[int]] = []
        for subtask_index in semantic_stage:
            robot = local_robot_by_subtask.get(subtask_index)
            placed = False
            for wave_index, used_robots in enumerate(robots_by_wave):
                if robot is None or robot not in used_robots:
                    stage_waves[wave_index].append(subtask_index)
                    if robot is not None:
                        used_robots.add(robot)
                    placed = True
                    break
            if not placed:
                stage_waves.append([subtask_index])
                robots_by_wave.append({robot} if robot is not None else set())
        waves.extend(stage_waves)
    return waves


def resource_collisions(
    stage_by_subtask: Dict[int, int],
    local_robot_by_subtask: Dict[int, int],
) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[int, int], List[int]] = {}
    for subtask_index, stage_index in stage_by_subtask.items():
        robot = local_robot_by_subtask.get(subtask_index)
        if robot is None:
            continue
        groups.setdefault((stage_index, robot), []).append(subtask_index)

    collisions: List[Dict[str, Any]] = []
    for (stage_index, robot), subtasks in sorted(groups.items()):
        if len(subtasks) < 2:
            continue
        collisions.append(
            {
                "stage": stage_index + 1,
                "local_robot": robot,
                "subtasks": sorted(subtasks),
                "colliding_pairs": [
                    list(pair) for pair in combinations(sorted(subtasks), 2)
                ],
            }
        )
    return collisions


def schedule_lines(
    waves: Sequence[Sequence[int]],
    local_robot_by_subtask: Dict[int, int],
) -> List[str]:
    lines: List[str] = []
    for wave in waves:
        assignments = []
        for subtask_index in wave:
            robot = local_robot_by_subtask.get(subtask_index)
            if robot is None:
                assignments.append(f"Subtask {subtask_index}: Robot ?;")
            else:
                assignments.append(f"Subtask {subtask_index}: Robot {robot};")
        lines.append("".join(assignments))
    return lines


def analyze_record(record: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    task_record, task_path = read_task_record(record, repo_root)
    subtasks = build_subtask_views(record, task_record)
    by_index = {item.decomposed_index: item for item in subtasks}
    semantic = infer_semantic_stages(str(record.get("task") or ""), subtasks)
    semantic_edges = semantic.closure_edges
    direct_edges = semantic.direct_edges

    allocation = parse_allocation_schedule(record, repo_root)
    parallel_violations, reversed_violations = allocation_violations(
        semantic_edges,
        allocation["stage_by_subtask"],
    )
    claims = extract_parallel_claims(
        str(record.get("decomposition_output") or ""),
        len(subtasks),
    )
    claim_violations = narrative_claim_violations(claims, semantic_edges)

    robot_list = task_record.get("robot list") or []
    actual_global_robot_by_subtask: Dict[int, int] = {}
    for subtask_index, local_robot in allocation["local_robot_by_subtask"].items():
        local_offset = local_robot - 1
        if 0 <= local_offset < len(robot_list):
            actual_global_robot_by_subtask[subtask_index] = robot_list[local_offset]

    reference_matches = 0
    reference_comparisons = 0
    for subtask in subtasks:
        actual_global = actual_global_robot_by_subtask.get(subtask.decomposed_index)
        if actual_global is None or subtask.reference_robot is None:
            continue
        reference_comparisons += 1
        reference_matches += int(actual_global == subtask.reference_robot)

    same_robot_forced = sum(
        1
        for source, target in semantic_edges
        if by_index[source].reference_robot is not None
        and by_index[source].reference_robot == by_index[target].reference_robot
    )
    pair_keys = all_pair_keys(subtasks)
    forced_pair_keys = {frozenset(edge) for edge in semantic_edges}
    unordered_pair_keys = pair_keys - forced_pair_keys
    same_robot_unordered = 0
    for pair in unordered_pair_keys:
        source, target = sorted(pair)
        if (
            by_index[source].reference_robot is not None
            and by_index[source].reference_robot == by_index[target].reference_robot
        ):
            same_robot_unordered += 1

    coverage = len(allocation["stage_by_subtask"])
    repaired_waves = repair_schedule(
        semantic.stages,
        allocation["local_robot_by_subtask"],
    )
    collisions = resource_collisions(
        allocation["stage_by_subtask"],
        allocation["local_robot_by_subtask"],
    )
    return {
        "id": record.get("id"),
        "task": record.get("task"),
        "source": record.get("source"),
        "task_jsonl_path": str(task_path.relative_to(repo_root)),
        "subtasks": [item.as_dict() for item in subtasks],
        "semantic": {
            "rule": "then_stage_barrier",
            "clauses": semantic.clauses,
            "stages": semantic.stages,
            "stage_scores": {
                str(key): value for key, value in sorted(semantic.scores.items())
            },
            "issues": semantic.issues,
            "direct_edges": [
                edge_dict(edge, by_index) for edge in sorted(direct_edges)
            ],
            "closure_edges": [
                edge_dict(edge, by_index) for edge in sorted(semantic_edges)
            ],
        },
        "decomposition_narrative": {
            "parallel_claims": claims,
            "high_confidence_violations": claim_violations,
        },
        "allocation": {
            **allocation,
            "coverage": coverage,
            "expected_subtasks": len(subtasks),
            "parallelized_forced_edges": [
                edge_dict(edge, by_index) for edge in parallel_violations
            ],
            "reversed_forced_edges": [
                edge_dict(edge, by_index) for edge in reversed_violations
            ],
            "suggested_repair_lines": schedule_lines(
                repaired_waves,
                allocation["local_robot_by_subtask"],
            ),
            "global_robot_by_subtask": actual_global_robot_by_subtask,
            "resource_collisions": collisions,
        },
        "reference_assignment": {
            "robot_list": robot_list,
            "reference_matches_actual": reference_matches,
            "reference_comparisons": reference_comparisons,
            "same_robot_forced_edges": same_robot_forced,
            "forced_edges": len(semantic_edges),
            "same_robot_unordered_pairs": same_robot_unordered,
            "unordered_pairs": len(unordered_pair_keys),
        },
        "baseline_edges": {
            name: [list(edge) for edge in sorted(baseline_edges(name, subtasks))]
            for name in (
                "task_file_array_order",
                "decomposition_total_order",
                "reference_same_robot_order",
            )
        },
    }


def aggregate(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    then_counts: Counter[int] = Counter()
    stage_shapes: Counter[str] = Counter()
    skill_edges: Counter[str] = Counter()
    violation_skill_edges: Counter[str] = Counter()
    allocation_status: Counter[str] = Counter()
    baseline_totals = {
        name: {"tp": 0, "fp": 0, "fn": 0}
        for name in (
            "task_file_array_order",
            "decomposition_total_order",
            "reference_same_robot_order",
        )
    }

    total_subtasks = 0
    total_pairs = 0
    semantic_edges = 0
    direct_edges = 0
    ordered_records = 0
    semantic_issue_records = 0
    allocation_complete_records = 0
    allocation_violation_records = 0
    allocation_parallel_violations = 0
    allocation_reversed_violations = 0
    allocation_resource_collision_records = 0
    allocation_resource_collision_pairs = 0
    narrative_violation_records = 0
    narrative_violation_claims = 0
    narrative_and_allocation_violation_records = 0
    narrative_only_violation_records = 0
    allocation_only_violation_records = 0
    same_robot_forced = 0
    forced_reference_edges = 0
    same_robot_unordered = 0
    unordered_reference_pairs = 0
    reference_matches = 0
    reference_comparisons = 0

    for record in records:
        subtasks = record["subtasks"]
        subtask_count = len(subtasks)
        total_subtasks += subtask_count
        total_pairs += math.comb(subtask_count, 2)
        then_count = max(0, len(record["semantic"]["clauses"]) - 1)
        then_counts[then_count] += 1
        shape = "→".join(str(len(stage)) for stage in record["semantic"]["stages"])
        stage_shapes[shape] += 1
        semantic_issue_records += int(bool(record["semantic"]["issues"]))

        gold = {
            (edge["before"], edge["after"])
            for edge in record["semantic"]["closure_edges"]
        }
        ordered_records += int(bool(gold))
        semantic_edges += len(gold)
        direct_edges += len(record["semantic"]["direct_edges"])
        skill_edges.update(edge["skill_pair"] for edge in record["semantic"]["closure_edges"])

        for name, edges in record["baseline_edges"].items():
            counts = metric_counts(gold, {tuple(edge) for edge in edges})
            for key in baseline_totals[name]:
                baseline_totals[name][key] += counts[key]

        allocation = record["allocation"]
        allocation_status[allocation["status"]] += 1
        complete = allocation["coverage"] == allocation["expected_subtasks"]
        allocation_complete_records += int(complete)
        parallel_bad = allocation["parallelized_forced_edges"]
        reversed_bad = allocation["reversed_forced_edges"]
        bad = parallel_bad + reversed_bad
        allocation_violation_records += int(bool(bad))
        allocation_parallel_violations += len(parallel_bad)
        allocation_reversed_violations += len(reversed_bad)
        violation_skill_edges.update(edge["skill_pair"] for edge in bad)
        collisions = allocation["resource_collisions"]
        allocation_resource_collision_records += int(bool(collisions))
        allocation_resource_collision_pairs += sum(
            len(collision["colliding_pairs"]) for collision in collisions
        )

        narrative_bad = record["decomposition_narrative"]["high_confidence_violations"]
        narrative_violation_records += int(bool(narrative_bad))
        narrative_violation_claims += len(narrative_bad)
        narrative_and_allocation_violation_records += int(bool(narrative_bad) and bool(bad))
        narrative_only_violation_records += int(bool(narrative_bad) and not bad)
        allocation_only_violation_records += int(not narrative_bad and bool(bad))

        reference = record["reference_assignment"]
        same_robot_forced += reference["same_robot_forced_edges"]
        forced_reference_edges += reference["forced_edges"]
        same_robot_unordered += reference["same_robot_unordered_pairs"]
        unordered_reference_pairs += reference["unordered_pairs"]
        reference_matches += reference["reference_matches_actual"]
        reference_comparisons += reference["reference_comparisons"]

    return {
        "records": len(records),
        "subtasks": total_subtasks,
        "subtask_pairs": total_pairs,
        "then_count_distribution": {
            str(key): value for key, value in sorted(then_counts.items())
        },
        "stage_shape_distribution": [
            {"shape": key, "records": value}
            for key, value in stage_shapes.most_common()
        ],
        "semantic": {
            "ordered_records": ordered_records,
            "records_with_stage_inference_issues": semantic_issue_records,
            "direct_edges": direct_edges,
            "closure_edges": semantic_edges,
            "unordered_pairs": total_pairs - semantic_edges,
            "top_skill_edges": [
                {"skill_pair": key, "edges": value}
                for key, value in skill_edges.most_common(12)
            ],
            "top_violation_skill_rates": [
                {
                    "skill_pair": key,
                    "violations": violations,
                    "semantic_edges": skill_edges[key],
                    "violation_rate": violations / skill_edges[key],
                }
                for key, violations in violation_skill_edges.most_common(12)
            ],
        },
        "baselines": {
            name: metric_summary(counts)
            for name, counts in baseline_totals.items()
        },
        "allocation": {
            "status_distribution": dict(sorted(allocation_status.items())),
            "complete_records": allocation_complete_records,
            "violation_records": allocation_violation_records,
            "parallelized_forced_edges": allocation_parallel_violations,
            "reversed_forced_edges": allocation_reversed_violations,
            "resource_collision_records": allocation_resource_collision_records,
            "resource_collision_pairs": allocation_resource_collision_pairs,
            "top_violation_skill_edges": [
                {"skill_pair": key, "edges": value}
                for key, value in violation_skill_edges.most_common(12)
            ],
        },
        "decomposition_narrative": {
            "high_confidence_violation_records": narrative_violation_records,
            "high_confidence_violation_claims": narrative_violation_claims,
            "also_violates_allocation": narrative_and_allocation_violation_records,
            "narrative_only": narrative_only_violation_records,
            "allocation_only": allocation_only_violation_records,
        },
        "reference_assignment": {
            "same_robot_forced_edges": same_robot_forced,
            "forced_edges": forced_reference_edges,
            "same_robot_forced_rate": (
                same_robot_forced / forced_reference_edges
                if forced_reference_edges
                else None
            ),
            "same_robot_unordered_pairs": same_robot_unordered,
            "unordered_pairs": unordered_reference_pairs,
            "same_robot_unordered_rate": (
                same_robot_unordered / unordered_reference_pairs
                if unordered_reference_pairs
                else None
            ),
            "actual_assignment_matches": reference_matches,
            "actual_assignment_comparisons": reference_comparisons,
            "actual_assignment_match_rate": (
                reference_matches / reference_comparisons
                if reference_comparisons
                else None
            ),
        },
    }


def percent(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def metric_cell(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def markdown_report(
    input_path: Path,
    summary: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    repo_root: Path,
) -> str:
    violation_records = [
        record
        for record in records
        if (
            record["allocation"]["parallelized_forced_edges"]
            or record["allocation"]["reversed_forced_edges"]
        )
    ]
    narrative_records = [
        record
        for record in records
        if record["decomposition_narrative"]["high_confidence_violations"]
    ]
    semantic = summary["semantic"]
    allocation = summary["allocation"]
    reference = summary["reference_assignment"]

    lines = [
        "# DeepSeek-V3.2 任务分解语义顺序审查",
        "",
        f"- 输入：`{input_path.relative_to(repo_root)}`",
        f"- 样本：{summary['records']} 条任务，{summary['subtasks']} 个子任务，"
        f"{summary['subtask_pairs']} 个子任务对",
        f"- 原有匹配标签：全部样本均为 `fully_matched=true`，但匹配策略明确为"
        f" `order_independent`，因此它不能证明时序正确",
        "",
        "## 结论",
        "",
        "1. 当前数据中可作为“硬语义前后关系”的稳定规则是 `then` 阶段屏障："
        "左侧阶段全部完成后，右侧阶段才可开始；同一阶段内由 `and` 或逗号连接的"
        "子任务不产生硬顺序。",
        "2. 子任务数组顺序、分解输出中的编号顺序、以及任务文件里的"
        " `assigned_robots` 都不能单独充当语义顺序真值。",
        f"3. 该规则抽取到 {semantic['direct_edges']} 条直接屏障边、"
        f"{semantic['closure_edges']} 条传递闭包边；"
        f"{semantic['unordered_pairs']} 个任务对在纯语义层面无硬顺序。",
        f"4. 下游分配结果中有 {allocation['violation_records']} / "
        f"{summary['records']} 条任务违反至少一条硬语义边："
        f"{allocation['parallelized_forced_edges']} 条被放在同一并行行，"
        f"{allocation['reversed_forced_edges']} 条被反向安排。若只看含硬语义边的"
        f" {semantic['ordered_records']} 条任务，任务级违例率为 "
        f"{percent(allocation['violation_records'] / semantic['ordered_records'])}。",
        f"5. 分解文字的高置信并行声明中，至少有 "
        f"{summary['decomposition_narrative']['high_confidence_violation_records']} 条"
        "任务直接与 `then` 屏障冲突；其中 "
        f"{summary['decomposition_narrative']['also_violates_allocation']} 条传播到"
        "最终安排，"
        f"{summary['decomposition_narrative']['narrative_only']} 条被分配阶段纠正，另有 "
        f"{summary['decomposition_narrative']['allocation_only']} 条只在最终安排中出现。"
        "文字冲突数是保守下界，不代表所有文字错误。",
        f"6. 另有 {allocation['resource_collision_records']} 条安排把同一机器人"
        f"在同一行重复使用，涉及 {allocation['resource_collision_pairs']} 个任务对；"
        "这是资源串行问题，应与语义强制关系分开处理。",
        "",
        "## 模式",
        "",
        "| `then` 数量 | 任务数 |",
        "|---:|---:|",
    ]
    lines.extend(
        f"| {then_count} | {count} |"
        for then_count, count in summary["then_count_distribution"].items()
    )
    lines.extend(
        [
            "",
            "高频阶段形状（数字表示每个阶段内的子任务数）：",
            "",
            "| 阶段形状 | 任务数 |",
            "|---|---:|",
        ]
    )
    lines.extend(
        f"| {item['shape']} | {item['records']} |"
        for item in summary["stage_shape_distribution"][:12]
    )
    lines.extend(
        [
            "",
            "高频违例技能对（含传递闭包边）：",
            "",
            "| 技能对 | 违例边 | 语义边 | 违例率 |",
            "|---|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {item['skill_pair']} | {item['violations']} | "
        f"{item['semantic_edges']} | {percent(item['violation_rate'])} |"
        for item in semantic["top_violation_skill_rates"]
    )
    lines.extend(
        [
            "",
            "规则解释：",
            "",
            "- `A and B, then C and D` 推出 `A→C`、`A→D`、`B→C`、`B→D`；"
            "不推出 `A→B` 或 `C→D`。",
            "- 连续多个 `then` 形成阶段链，并具有传递性。",
            "- `it` 只做对象共指；若它位于 `then` 右侧，顺序来自 `then`，"
            "不是来自共指本身。",
            "- 共享对象、共享技能或共享机器人只能触发进一步审查，不能在"
            "“纯语义、无先决条件”设定下自动升级为硬边。",
            "",
            "## 迭代回测",
            "",
            "以 `then` 阶段边为语义真值，对三种弱启发式做微平均回测：",
            "",
            "| 启发式 | Precision | Recall | F1 | TP | FP | FN |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    baseline_labels = {
        "task_file_array_order": "任务文件数组全序",
        "decomposition_total_order": "分解编号全序",
        "reference_same_robot_order": "参考同机器人 + 分解编号",
    }
    for name, values in summary["baselines"].items():
        lines.append(
            f"| {baseline_labels[name]} | {metric_cell(values['precision'])} | "
            f"{metric_cell(values['recall'])} | {metric_cell(values['f1'])} | "
            f"{values['tp']} | {values['fp']} | {values['fn']} |"
        )

    lines.extend(
        [
            "",
            "参考机器人安排的弱证据：",
            "",
            f"- 硬语义边中同机器人占 "
            f"{reference['same_robot_forced_edges']} / {reference['forced_edges']} "
            f"（{percent(reference['same_robot_forced_rate'])}）。",
            f"- 无硬语义关系的任务对中，同机器人仍占 "
            f"{reference['same_robot_unordered_pairs']} / {reference['unordered_pairs']} "
            f"（{percent(reference['same_robot_unordered_rate'])}）。",
            f"- 实际分配与任务文件参考机器人完全一致 "
            f"{reference['actual_assignment_matches']} / "
            f"{reference['actual_assignment_comparisons']} "
            f"（{percent(reference['actual_assignment_match_rate'])}）。",
            "- 这与生成代码一致：参考机器人是按 `robot list` 顺序选择第一个"
            "满足技能/质量约束的机器人。它适合做能力可行性参考，不适合做时序标签。",
            "",
            "## 下游安排中的语义违例",
            "",
        ]
    )
    if not violation_records:
        lines.append("未发现可解析安排中的硬语义违例。")
    else:
        lines.extend(
            [
                "| ID | 任务 | 并行违例 | 反向违例 | 当前安排 | 建议安排 |",
                "|---|---|---:|---:|---|---|",
            ]
        )
        for record in violation_records:
            allocation_record = record["allocation"]
            task = str(record["task"]).replace("|", "\\|")
            current = "<br>".join(allocation_record["lines"]).replace("|", "\\|")
            repaired = "<br>".join(
                allocation_record["suggested_repair_lines"]
            ).replace("|", "\\|")
            lines.append(
                f"| `{record['id']}` | {task} | "
                f"{len(allocation_record['parallelized_forced_edges'])} | "
                f"{len(allocation_record['reversed_forced_edges'])} | "
                f"`{current}` | `{repaired}` |"
            )

    lines.extend(
        [
            "",
            "## 分解文字的高置信冲突",
            "",
        ]
    )
    if not narrative_records:
        lines.append("未发现高置信的并行文字冲突。")
    else:
        lines.extend(
            [
                "| ID | 任务 | 冲突声明 |",
                "|---|---|---|",
            ]
        )
        for record in narrative_records:
            claims = record["decomposition_narrative"]["high_confidence_violations"]
            claim_text = "<br>".join(
                str(claim["line"]).replace("|", "\\|") for claim in claims
            )
            task = str(record["task"]).replace("|", "\\|")
            lines.append(f"| `{record['id']}` | {task} | {claim_text} |")

    lines.extend(
        [
            "",
            "## 建议落地规则",
            "",
            "1. 在任务分解输出中增加机器可读的 `semantic_stages`，不要从自然语言"
            "并行说明二次猜测。",
            "2. 分配阶段把相邻语义阶段之间的笛卡尔积作为 barrier；机器人能力和"
            "资源冲突只在阶段内部继续拆 wave。",
            "3. 评测拆成三项：子任务集合双射、语义边保持率、机器人能力可行率。"
            "现有 `fully_matched` 只覆盖第一项。",
            "4. 若未来任务文本引入 `before`、`after`、`while`，应先扩展语义语法"
            "并人工标注小型验证集；不要直接用 PDDL 先决条件替代语义标签。",
            "",
            "## 审查边界",
            "",
            "- 文字并行冲突抽取采用保守规则，因此其数量是下界。",
            "- `then` 阶段规则适用于本批生成式任务语言；它不是通用自然语言"
            "时序解析器。",
            "- 建议安排保留模型选择的机器人，只插入语义 barrier，并在同一机器人"
            "重复出现时拆分 wave；它没有优化路径成本。",
            "",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def run_audit(
    input_path: Path,
    repo_root: Path = REPO_ROOT,
) -> Dict[str, Any]:
    source_records = read_jsonl(input_path)
    records = [analyze_record(record, repo_root) for record in source_records]
    return {
        "schema_version": 1,
        "method": {
            "semantic_rule": "then_stage_barrier",
            "uses_pddl_preconditions": False,
            "same_stage_connectors": ["and", "comma"],
            "reference_assignment_role": "weak_resource_and_capability_evidence",
        },
        "input": str(input_path.relative_to(repo_root)),
        "summary": aggregate(records),
        "records": records,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    input_path = resolve_path(args.input)
    output_json = resolve_path(args.output_json)
    output_markdown = resolve_path(args.output_markdown)
    audit = run_audit(input_path)
    write_json(output_json, audit)
    write_markdown(
        output_markdown,
        markdown_report(
            input_path,
            audit["summary"],
            audit["records"],
            REPO_ROOT,
        ),
    )
    print(f"Wrote JSON audit to {output_json}")
    print(f"Wrote Markdown report to {output_markdown}")
    print(json.dumps(audit["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
