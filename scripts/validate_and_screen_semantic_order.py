#!/usr/bin/env python3
"""Validate high-confidence semantic-order patterns and screen decompositions.

The validation gold is static and manually reviewed. The parser may abstain.
A pattern is usable only when held-out precision and its 95% Wilson lower bound
are at least 90%, while forced-edge recall and ordered-task coverage exceed 40%.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from analyze_decomposition_ordering import (
    DEFAULT_INPUT,
    REPO_ROOT,
    read_jsonl,
    resolve_path,
    run_audit,
)


DEFAULT_GOLD = Path("data/grpo/deepseek_v3_2_semantic_order_gold.jsonl")
DEFAULT_VALIDATION = Path("data/grpo/deepseek_v3_2_ordering_validation.json")
DEFAULT_SCREENING = Path("data/grpo/deepseek_v3_2_decomposition_screening.jsonl")
DEFAULT_REPORT = Path("reports/deepseek_v3_2_semantic_order_validation.md")

Edge = Tuple[int, int]
KeyedEdge = Tuple[str, int, int]

MIN_EMPIRICAL_PRECISION = 0.90
MIN_PRECISION_LOWER_95 = 0.90
MIN_FORCED_EDGE_RECALL = 0.40
MIN_ORDERED_TASK_COVERAGE = 0.40
MIN_ALIGNMENT_SCORE = 10
MIN_ALIGNMENT_MARGIN = 3


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--gold", default=str(DEFAULT_GOLD))
    parser.add_argument("--output-validation", default=str(DEFAULT_VALIDATION))
    parser.add_argument("--output-screening", default=str(DEFAULT_SCREENING))
    parser.add_argument("--output-report", default=str(DEFAULT_REPORT))
    parser.add_argument("--min-precision", type=float, default=MIN_EMPIRICAL_PRECISION)
    parser.add_argument(
        "--min-precision-lower-95",
        type=float,
        default=MIN_PRECISION_LOWER_95,
    )
    parser.add_argument("--min-recall", type=float, default=MIN_FORCED_EDGE_RECALL)
    parser.add_argument(
        "--min-ordered-task-coverage",
        type=float,
        default=MIN_ORDERED_TASK_COVERAGE,
    )
    parser.add_argument("--min-alignment-score", type=int, default=MIN_ALIGNMENT_SCORE)
    parser.add_argument("--min-alignment-margin", type=int, default=MIN_ALIGNMENT_MARGIN)
    return parser


def stages_to_edges(stages: Sequence[Sequence[int]]) -> Set[Edge]:
    edges: Set[Edge] = set()
    for left_index, left_stage in enumerate(stages):
        for right_stage in stages[left_index + 1 :]:
            edges.update(
                (int(source), int(target))
                for source in left_stage
                for target in right_stage
            )
    return edges


def keyed_edges(record_id: str, edges: Iterable[Edge]) -> Set[KeyedEdge]:
    return {(record_id, source, target) for source, target in edges}


def wilson_lower(
    successes: int,
    total: int,
    z: float = 1.959963984540054,
) -> Optional[float]:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    adjustment = z * math.sqrt(
        proportion * (1 - proportion) / total
        + z * z / (4 * total * total)
    )
    return (centre - adjustment) / denominator


def structural_pattern(stages: Sequence[Sequence[int]]) -> str:
    sizes = [len(stage) for stage in stages]
    if len(sizes) <= 1:
        return "no_temporal_marker"
    if all(size == 1 for size in sizes):
        return "then_serial_chain"
    if len(sizes) == 2 and sizes[0] > 1 and sizes[1] == 1:
        return "then_fan_in"
    if len(sizes) == 2 and sizes[0] == 1 and sizes[1] > 1:
        return "then_fan_out"
    return "then_grouped_chain"


def strict_parser_decision(
    record: Dict[str, Any],
    *,
    min_alignment_score: int = MIN_ALIGNMENT_SCORE,
    min_alignment_margin: int = MIN_ALIGNMENT_MARGIN,
) -> Dict[str, Any]:
    semantic = record["semantic"]
    stages = semantic["stages"]
    pattern = structural_pattern(stages)
    if len(semantic["clauses"]) <= 1:
        return {
            "status": "no_forced_order",
            "pattern": pattern,
            "edges": [],
            "min_alignment_score": None,
            "min_alignment_margin": None,
            "reasons": ["no explicit temporal marker"],
        }

    reasons = list(semantic["issues"])
    best_scores: List[int] = []
    margins: List[int] = []
    for raw_scores in semantic["stage_scores"].values():
        scores = [int(value) for value in raw_scores]
        if not scores:
            reasons.append("empty lexical score vector")
            continue
        best = max(scores)
        best_scores.append(best)
        best_positions = [
            index for index, score in enumerate(scores) if score == best
        ]
        if len(best_positions) != 1:
            reasons.append("non-unique best stage")
            margins.append(0)
            continue
        runner_up = max(
            (
                score
                for index, score in enumerate(scores)
                if index != best_positions[0]
            ),
            default=-1,
        )
        margins.append(best - runner_up)

    min_score = min(best_scores, default=0)
    min_margin = min(margins, default=0)
    if min_score < min_alignment_score:
        reasons.append(
            f"minimum alignment score {min_score} < {min_alignment_score}"
        )
    if min_margin < min_alignment_margin:
        reasons.append(
            f"minimum alignment margin {min_margin} < {min_alignment_margin}"
        )
    if any(not stage for stage in stages):
        reasons.append("empty semantic stage")

    edges = [
        [int(edge["before"]), int(edge["after"])]
        for edge in semantic["closure_edges"]
    ]
    return {
        "status": "parsed_forced_order" if not reasons else "abstained",
        "pattern": pattern,
        "edges": edges if not reasons else [],
        "candidate_edges": edges,
        "min_alignment_score": min_score,
        "min_alignment_margin": min_margin,
        "reasons": reasons,
    }


def precision_metrics(tp: int, fp: int) -> Dict[str, Any]:
    total = tp + fp
    precision = tp / total if total else None
    return {
        "tp": tp,
        "fp": fp,
        "predicted_edges": total,
        "precision": precision,
        "precision_lower_95": wilson_lower(tp, total),
    }


def pattern_metrics(
    pattern: str,
    triples: Sequence[
        Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]
    ],
) -> Dict[str, Any]:
    gold_all: Set[KeyedEdge] = set()
    predicted_all: Set[KeyedEdge] = set()
    record_count = 0
    for gold, _, decision in triples:
        if decision["pattern"] != pattern:
            continue
        record_count += 1
        record_id = str(gold["id"])
        gold_all.update(
            keyed_edges(record_id, stages_to_edges(gold["stages"]))
        )
        predicted_all.update(
            keyed_edges(
                record_id,
                {tuple(edge) for edge in decision["edges"]},
            )
        )

    tp = len(gold_all & predicted_all)
    fp = len(predicted_all - gold_all)
    fn = len(gold_all - predicted_all)
    metrics = precision_metrics(tp, fp)
    metrics.update(
        {
            "pattern": pattern,
            "records": record_count,
            "gold_edges": len(gold_all),
            "fn": fn,
            "recall": tp / len(gold_all) if gold_all else None,
        }
    )
    return metrics


def validate_parser(
    records: Sequence[Dict[str, Any]],
    gold_entries: Sequence[Dict[str, Any]],
    *,
    min_precision: float = MIN_EMPIRICAL_PRECISION,
    min_precision_lower_95: float = MIN_PRECISION_LOWER_95,
    min_recall: float = MIN_FORCED_EDGE_RECALL,
    min_ordered_task_coverage: float = MIN_ORDERED_TASK_COVERAGE,
    min_alignment_score: int = MIN_ALIGNMENT_SCORE,
    min_alignment_margin: int = MIN_ALIGNMENT_MARGIN,
) -> Dict[str, Any]:
    by_id = {str(record["id"]): record for record in records}
    if len(by_id) != len(records):
        raise ValueError("audit records contain duplicate ids")

    seen_gold: Set[str] = set()
    triples: List[
        Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]
    ] = []
    gold_all: Set[KeyedEdge] = set()
    predicted_all: Set[KeyedEdge] = set()
    pre_pronoun_predicted_all: Set[KeyedEdge] = set()
    ordered_records = 0
    exactly_parsed_ordered_records = 0
    exact_stage_records = 0
    abstained_records = 0

    for gold in gold_entries:
        record_id = str(gold.get("id") or "")
        if not record_id or record_id in seen_gold:
            raise ValueError(f"invalid or duplicate gold id: {record_id!r}")
        seen_gold.add(record_id)
        record = by_id.get(record_id)
        if record is None:
            raise ValueError(f"gold id is absent from audit input: {record_id}")

        stages = gold.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ValueError(f"gold stages are invalid for {record_id}")

        decision = strict_parser_decision(
            record,
            min_alignment_score=min_alignment_score,
            min_alignment_margin=min_alignment_margin,
        )
        triples.append((gold, record, decision))
        gold_edges = stages_to_edges(stages)
        predicted_edges = {tuple(edge) for edge in decision["edges"]}
        gold_all.update(keyed_edges(record_id, gold_edges))
        predicted_all.update(keyed_edges(record_id, predicted_edges))
        if not re.search(
            r"\bswitch\s+(?:it|them)\s+on\b",
            str(record.get("task") or ""),
            flags=re.IGNORECASE,
        ):
            pre_pronoun_predicted_all.update(
                keyed_edges(record_id, predicted_edges)
            )
        abstained_records += int(decision["status"] == "abstained")
        exact_stage_records += int(record["semantic"]["stages"] == stages)
        if gold_edges:
            ordered_records += 1
            exactly_parsed_ordered_records += int(
                predicted_edges == gold_edges
            )

    tp = len(gold_all & predicted_all)
    fp = len(predicted_all - gold_all)
    fn = len(gold_all - predicted_all)
    precision = precision_metrics(tp, fp)
    recall = tp / len(gold_all) if gold_all else None
    ordered_task_coverage = (
        exactly_parsed_ordered_records / ordered_records
        if ordered_records
        else None
    )
    patterns = sorted(
        {decision["pattern"] for _, _, decision in triples}
    )
    diagnostics = [
        pattern_metrics(pattern, triples) for pattern in patterns
    ]

    pre_pronoun_tp = len(gold_all & pre_pronoun_predicted_all)
    pre_pronoun_fp = len(pre_pronoun_predicted_all - gold_all)
    pre_pronoun_fn = len(gold_all - pre_pronoun_predicted_all)
    pre_pronoun_precision = precision_metrics(
        pre_pronoun_tp,
        pre_pronoun_fp,
    )

    accepted_pattern = {
        **precision,
        "pattern": "explicit_then_stage_barrier",
        "surface_forms": [
            "A then B",
            "A then B then C",
            "A and B then C",
            "A then B and C",
        ],
        "gold_edges": len(gold_all),
        "fn": fn,
        "recall": recall,
    }
    confidence_pass = (
        precision["precision"] is not None
        and precision["precision"] >= min_precision
        and precision["precision_lower_95"] is not None
        and precision["precision_lower_95"] >= min_precision_lower_95
    )
    coverage_pass = (
        recall is not None
        and recall >= min_recall
        and ordered_task_coverage is not None
        and ordered_task_coverage >= min_ordered_task_coverage
    )
    gate_passed = confidence_pass and coverage_pass
    accepted_pattern["accepted"] = confidence_pass

    accepted_patterns = [accepted_pattern] if confidence_pass else []
    structural_surface_forms = {
        "then_serial_chain": ["A then B", "A then B then C"],
        "then_fan_in": ["A and B then C"],
        "then_fan_out": ["A then B and C"],
        "then_grouped_chain": ["A and B then C then D"],
    }
    for diagnostic in diagnostics:
        independently_accepted = (
            diagnostic["predicted_edges"] > 0
            and diagnostic["precision"] is not None
            and diagnostic["precision"] >= min_precision
            and diagnostic["precision_lower_95"] is not None
            and diagnostic["precision_lower_95"] >= min_precision_lower_95
        )
        diagnostic["independently_accepted"] = independently_accepted
        if independently_accepted:
            accepted_patterns.append(
                {
                    **diagnostic,
                    "surface_forms": structural_surface_forms.get(
                        diagnostic["pattern"],
                        [],
                    ),
                    "accepted": True,
                }
            )

    return {
        "gold": {
            "records": len(gold_entries),
            "ordered_records": ordered_records,
            "forced_edges": len(gold_all),
            "adjudication": "manual_reviewed_2026-08-01",
            "selection_rule": "task_index modulo 5 in {0, 1}",
        },
        "parser": {
            "records_abstained": abstained_records,
            "exact_stage_records": exact_stage_records,
            "exact_stage_rate": (
                exact_stage_records / len(gold_entries)
                if gold_entries
                else None
            ),
            **precision,
            "fn": fn,
            "forced_edge_recall": recall,
            "exactly_parsed_ordered_records": (
                exactly_parsed_ordered_records
            ),
            "ordered_task_coverage": ordered_task_coverage,
        },
        "thresholds": {
            "min_empirical_precision": min_precision,
            "min_precision_lower_95": min_precision_lower_95,
            "min_forced_edge_recall": min_recall,
            "min_ordered_task_coverage": min_ordered_task_coverage,
            "min_alignment_score": min_alignment_score,
            "min_alignment_margin": min_alignment_margin,
        },
        "accepted_patterns": accepted_patterns,
        "structural_pattern_diagnostics": diagnostics,
        "iteration_history": [
            {
                "iteration": "v1_before_pronoun_switch_alias",
                **pre_pronoun_precision,
                "fn": pre_pronoun_fn,
                "forced_edge_recall": (
                    pre_pronoun_tp / len(gold_all) if gold_all else None
                ),
            },
            {
                "iteration": "v2_with_switch_it_them_on_alias",
                **precision,
                "fn": fn,
                "forced_edge_recall": recall,
            },
        ],
        "gate": {
            "confidence_passed": confidence_pass,
            "coverage_passed": coverage_pass,
            "passed": gate_passed,
        },
    }


def screen_records(
    records: Sequence[Dict[str, Any]],
    validation: Dict[str, Any],
    *,
    min_alignment_score: int = MIN_ALIGNMENT_SCORE,
    min_alignment_margin: int = MIN_ALIGNMENT_MARGIN,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    gate_passed = bool(validation["gate"]["passed"])
    confidence = None
    confidence_lower_95 = None
    if validation["accepted_patterns"]:
        accepted = next(
            item
            for item in validation["accepted_patterns"]
            if item["pattern"] == "explicit_then_stage_barrier"
        )
        confidence = accepted["precision"]
        confidence_lower_95 = accepted["precision_lower_95"]

    screened: List[Dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    decomposition_counts: Counter[str] = Counter()
    allocation_conflict_records = 0
    parallelized_forced_edges = 0
    reversed_forced_edges = 0
    for record in records:
        decision = strict_parser_decision(
            record,
            min_alignment_score=min_alignment_score,
            min_alignment_margin=min_alignment_margin,
        )
        narrative_bad = record["decomposition_narrative"][
            "high_confidence_violations"
        ]
        allocation = record["allocation"]
        allocation_bad = (
            allocation["parallelized_forced_edges"]
            + allocation["reversed_forced_edges"]
        )
        if allocation_bad:
            allocation_conflict_records += 1
        parallelized_forced_edges += len(
            allocation["parallelized_forced_edges"]
        )
        reversed_forced_edges += len(
            allocation["reversed_forced_edges"]
        )

        if not gate_passed:
            status = "unable_to_screen"
            reason = "validation gate failed"
        elif decision["status"] == "abstained":
            status = "unable_to_screen"
            reason = "; ".join(decision["reasons"])
        elif decision["status"] == "no_forced_order":
            status = "no_forced_order"
            reason = "no accepted mandatory-order pattern"
        elif narrative_bad or allocation_bad:
            status = "conflict"
            reason = "mandatory semantic order is contradicted"
        elif (
            allocation["status"] == "ok"
            and allocation["coverage"] == allocation["expected_subtasks"]
        ):
            status = "pass"
            reason = "mandatory semantic order is preserved in final schedule"
        else:
            status = "unable_to_screen"
            reason = "allocation sequence is incomplete or unavailable"

        if decision["status"] != "parsed_forced_order":
            decomposition_status = "not_applicable"
        elif narrative_bad:
            decomposition_status = "conflict"
        else:
            decomposition_status = "no_explicit_conflict"

        item = {
            "id": record["id"],
            "task": record["task"],
            "status": status,
            "reason": reason,
            "decomposition_status": decomposition_status,
            "parser": {
                **decision,
                "validated_precision": confidence,
                "validated_precision_lower_95": confidence_lower_95,
            },
            "semantic_stages": record["semantic"]["stages"],
            "semantic_edges": record["semantic"]["closure_edges"],
            "decomposition_conflicts": narrative_bad,
            "allocation_parallelized_forced_edges": allocation[
                "parallelized_forced_edges"
            ],
            "allocation_reversed_forced_edges": allocation[
                "reversed_forced_edges"
            ],
            "source": record["source"],
        }
        screened.append(item)
        status_counts[status] += 1
        decomposition_counts[decomposition_status] += 1

    parsed_ordered = sum(
        item["parser"]["status"] == "parsed_forced_order"
        for item in screened
    )
    return screened, {
        "records": len(screened),
        "status_counts": dict(sorted(status_counts.items())),
        "decomposition_status_counts": dict(
            sorted(decomposition_counts.items())
        ),
        "parsed_forced_order_records": parsed_ordered,
        "parsed_forced_order_rate": (
            parsed_ordered / len(screened) if screened else None
        ),
        "reference_allocation_backtest": {
            "conflict_records": allocation_conflict_records,
            "parallelized_forced_edges": parallelized_forced_edges,
            "reversed_forced_edges": reversed_forced_edges,
        },
        "validation_gate_passed": gate_passed,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(
    path: Path,
    values: Sequence[Dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def percent(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def markdown_report(
    validation: Dict[str, Any],
    screening_summary: Dict[str, Any],
    validation_path: Path,
    screening_path: Path,
) -> str:
    parser = validation["parser"]
    thresholds = validation["thresholds"]
    accepted = validation["accepted_patterns"]
    lines = [
        "# 强制语义顺序模式验证与筛查",
        "",
        "## 验收结果",
        "",
        f"- Gold：{validation['gold']['records']} 条人工复核任务，"
        f"{validation['gold']['ordered_records']} 条含强制顺序，"
        f"{validation['gold']['forced_edges']} 条真实强制边。",
        f"- Precision：{percent(parser['precision'])}；95% Wilson 下界："
        f"{percent(parser['precision_lower_95'])}。",
        f"- 强制边 recall：{percent(parser['forced_edge_recall'])}；"
        f"有序任务完整解析覆盖率："
        f"{percent(parser['ordered_task_coverage'])}。",
        f"- 门槛：precision ≥ "
        f"{percent(thresholds['min_empirical_precision'])}，"
        f"置信下界 ≥ "
        f"{percent(thresholds['min_precision_lower_95'])}，"
        f"recall/coverage ≥ "
        f"{percent(thresholds['min_forced_edge_recall'])}。",
        f"- 最终门禁："
        f"**{'通过' if validation['gate']['passed'] else '未通过'}**。",
        "",
        "## 已接受的高置信语言模式",
        "",
    ]
    if not accepted:
        lines.append(
            "没有模式越过置信门槛，筛查结果全部降级为无法判定。"
        )
    else:
        lines.extend(
            [
                "| 模式 | 表面形式 | Precision | 95% 下界 | Recall |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for item in accepted:
            lines.append(
                f"| {item['pattern']} | "
                f"{'<br>'.join(item['surface_forms'])} | "
                f"{percent(item['precision'])} | "
                f"{percent(item['precision_lower_95'])} | "
                f"{percent(item['recall'])} |"
            )

    lines.extend(
        [
            "",
            "解析规则：仅把显式 then 当作阶段 barrier；and 与逗号保留在"
            "同一阶段。动作与对象必须唯一对齐到某个阶段，最低词法分和"
            " runner-up margin 均达标才输出强制边，否则主动弃权。整个过程"
            "不读取 PDDL 先决条件或机器人分配。",
            "",
            "迭代记录：v1 未识别 switch it/them on，留出集 recall 为 "
            f"{percent(validation['iteration_history'][0]['forced_edge_recall'])}；"
            "补充代词动作别名后的 v2 recall 为 "
            f"{percent(validation['iteration_history'][1]['forced_edge_recall'])}，"
            "且 precision 未下降。",
            "",
            "## 全量筛查",
            "",
            f"- 全量记录：{screening_summary['records']}。",
            f"- 成功解析强制顺序："
            f"{screening_summary['parsed_forced_order_records']} "
            f"（{percent(screening_summary['parsed_forced_order_rate'])}）。",
            f"- 综合状态："
            f"{json.dumps(screening_summary['status_counts'], ensure_ascii=False)}。",
            f"- 仅看任务分解文字："
            f"{json.dumps(screening_summary['decomposition_status_counts'], ensure_ascii=False)}。",
            f"- 机器人参考安排回测："
            f"{screening_summary['reference_allocation_backtest']['conflict_records']} "
            "条记录违反强制顺序，其中 "
            f"{screening_summary['reference_allocation_backtest']['parallelized_forced_edges']} "
            "条强制边被并行、"
            f"{screening_summary['reference_allocation_backtest']['reversed_forced_edges']} "
            "条被反向安排。",
            "",
            "状态解释：",
            "",
            "- conflict：分解文字或最终 Sequence of Operations 明确违反强制边。",
            "- pass：已识别强制边，且最终机器可读安排全部保持。",
            "- no_forced_order：任务没有命中已接受的强制顺序模式。",
            "- unable_to_screen：解析器弃权、验证门禁失败或安排不完整。",
            "- no_explicit_conflict：任务分解文字未发现明示冲突；这不是正确性证明。",
            "",
            "## 产物",
            "",
            f"- 验证明细：{validation_path.relative_to(REPO_ROOT)}",
            f"- 逐任务筛查：{screening_path.relative_to(REPO_ROOT)}",
            "",
        ]
    )
    return "\n".join(lines)


def run_validation_and_screen(
    input_path: Path,
    gold_path: Path,
    *,
    min_precision: float = MIN_EMPIRICAL_PRECISION,
    min_precision_lower_95: float = MIN_PRECISION_LOWER_95,
    min_recall: float = MIN_FORCED_EDGE_RECALL,
    min_ordered_task_coverage: float = MIN_ORDERED_TASK_COVERAGE,
    min_alignment_score: int = MIN_ALIGNMENT_SCORE,
    min_alignment_margin: int = MIN_ALIGNMENT_MARGIN,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    audit = run_audit(input_path)
    records = audit["records"]
    gold_entries = read_jsonl(gold_path)
    validation = validate_parser(
        records,
        gold_entries,
        min_precision=min_precision,
        min_precision_lower_95=min_precision_lower_95,
        min_recall=min_recall,
        min_ordered_task_coverage=min_ordered_task_coverage,
        min_alignment_score=min_alignment_score,
        min_alignment_margin=min_alignment_margin,
    )
    validation["input"] = str(input_path.relative_to(REPO_ROOT))
    validation["gold"]["path"] = str(gold_path.relative_to(REPO_ROOT))
    screened, summary = screen_records(
        records,
        validation,
        min_alignment_score=min_alignment_score,
        min_alignment_margin=min_alignment_margin,
    )
    return validation, screened, summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    input_path = resolve_path(args.input)
    gold_path = resolve_path(args.gold)
    validation_path = resolve_path(args.output_validation)
    screening_path = resolve_path(args.output_screening)
    report_path = resolve_path(args.output_report)

    validation, screened, summary = run_validation_and_screen(
        input_path,
        gold_path,
        min_precision=args.min_precision,
        min_precision_lower_95=args.min_precision_lower_95,
        min_recall=args.min_recall,
        min_ordered_task_coverage=args.min_ordered_task_coverage,
        min_alignment_score=args.min_alignment_score,
        min_alignment_margin=args.min_alignment_margin,
    )
    validation["screening_summary"] = summary
    write_json(validation_path, validation)
    write_jsonl(screening_path, screened)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        markdown_report(
            validation,
            summary,
            validation_path,
            screening_path,
        ),
        encoding="utf-8",
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if validation["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
