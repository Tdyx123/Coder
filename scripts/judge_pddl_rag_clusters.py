#!/usr/bin/env python3
"""Select one retrieval document to keep for each repeated PDDL RAG cluster."""

from __future__ import annotations

import argparse
import heapq
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLUSTERS_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_similarity_clusters.json"
DEFAULT_CORPUS_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_corpus.jsonl"
DEFAULT_OUTPUT_JSONL = REPO_ROOT / "data" / "rag" / "pddlrun_cluster_winners.jsonl"
DEFAULT_SUMMARY_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_cluster_winners_summary.json"
DEFAULT_SHARDS_DIR = REPO_ROOT / "data" / "rag" / "pddlrun_cluster_judge_shards"
DEFAULT_SKIP_CLUSTER_IDS = ("cluster:problem_generation:000001",)

QUALITY_RANK = {
    "success": 3,
    "partial": 2,
    "failed": 1,
}


@dataclass(frozen=True)
class DocSignals:
    doc_id: str
    stage: str
    quality: str
    retrieval_eligible: bool
    content_score: int
    content_length: int
    reason: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Judge duplicate PDDL RAG similarity clusters and write one winner "
            "doc_id per multi-document cluster."
        )
    )
    parser.add_argument("--clusters-path", default=str(DEFAULT_CLUSTERS_PATH))
    parser.add_argument("--corpus-path", default=str(DEFAULT_CORPUS_PATH))
    parser.add_argument("--output-jsonl", default=str(DEFAULT_OUTPUT_JSONL))
    parser.add_argument("--summary-path", default=str(DEFAULT_SUMMARY_PATH))
    parser.add_argument("--shards-dir", default=str(DEFAULT_SHARDS_DIR))
    parser.add_argument("--agent-count", type=int, default=10)
    parser.add_argument(
        "--agent-index",
        type=int,
        help="Optional 1-based shard index to write only one agent shard.",
    )
    parser.add_argument(
        "--skip-cluster-id",
        action="append",
        default=list(DEFAULT_SKIP_CLUSTER_IDS),
        help=(
            "Cluster id to skip. Can be passed more than once. Defaults to "
            "cluster:problem_generation:000001."
        ),
    )
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def write_json(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as file_obj:
        for record in records:
            file_obj.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def cluster_members(cluster: Dict[str, Any]) -> List[Dict[str, Any]]:
    members = cluster.get("members")
    return members if isinstance(members, list) else []


def cluster_member_count(cluster: Dict[str, Any]) -> int:
    value = cluster.get("member_count")
    if isinstance(value, int):
        return value
    return len(cluster_members(cluster))


def load_cluster_plan(
    clusters_path: Path,
    skip_cluster_ids: Set[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    data = read_json(clusters_path)
    clusters = data.get("clusters") if isinstance(data, dict) else data
    if not isinstance(clusters, list):
        raise ValueError(f"{clusters_path} does not contain a clusters list")

    processable: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    singleton_count = 0
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        cluster_id = str(cluster.get("id", ""))
        member_count = cluster_member_count(cluster)
        if member_count <= 1:
            singleton_count += 1
            continue
        if cluster_id in skip_cluster_ids:
            skipped.append(
                {
                    "cluster_id": cluster_id,
                    "stage": cluster.get("stage"),
                    "member_count": member_count,
                    "reason": "explicit_skip",
                }
            )
            continue
        processable.append(cluster)
    return processable, skipped, singleton_count


def assign_clusters(
    clusters: Sequence[Dict[str, Any]],
    agent_count: int,
) -> List[List[Dict[str, Any]]]:
    if agent_count <= 0:
        raise ValueError("agent_count must be positive")

    assignments: List[List[Dict[str, Any]]] = [[] for _ in range(agent_count)]
    heap: List[Tuple[int, int]] = [(0, index) for index in range(agent_count)]
    heapq.heapify(heap)

    for cluster in sorted(clusters, key=lambda item: (-cluster_member_count(item), str(item.get("id", "")))):
        load, index = heapq.heappop(heap)
        assignments[index].append(cluster)
        heapq.heappush(heap, (load + cluster_member_count(cluster), index))

    return assignments


def required_doc_ids(clusters: Iterable[Dict[str, Any]]) -> Set[str]:
    doc_ids: Set[str] = set()
    for cluster in clusters:
        for member in cluster_members(cluster):
            doc_id = member.get("doc_id")
            if isinstance(doc_id, str) and doc_id:
                doc_ids.add(doc_id)
    return doc_ids


def section_text(content: str, heading: str) -> str:
    pattern = re.compile(
        r"(?ms)^#\s+" + re.escape(heading) + r"\s*\n(.*?)(?=^#\s+|\Z)"
    )
    match = pattern.search(content)
    return match.group(1).strip() if match else ""


def count_action_like_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if re.match(r"\s*\([^;\s][^)]*\)", line))


def score_decompose(content: str) -> Tuple[int, str]:
    score = 0
    reasons: List[str] = []
    lower = content.casefold()
    if "# decomposition output" in lower:
        score += 15
        reasons.append("decomposition output present")
    subtask_count = len(re.findall(r"\bsub\s*task\b|\bsubtask\b", content, flags=re.IGNORECASE))
    if subtask_count:
        score += min(subtask_count, 8) * 3
        reasons.append("subtasks described")
    for marker, value, label in [
        ("preconditions", 5, "preconditions"),
        ("effects", 5, "effects"),
        ("parameters", 3, "parameters"),
    ]:
        if marker in lower:
            score += value
            reasons.append(label)
    action_mentions = len(
        re.findall(
            r"\b(GoToObject|OpenObject|CloseObject|PickupObject|PutObject|"
            r"SwitchOn|SwitchOff|CleanObject|SliceObject|BreakObject)\b",
            content,
        )
    )
    if action_mentions:
        score += min(action_mentions, 12)
        reasons.append("action details")
    score += min(len(content) // 800, 10)
    return score, ", ".join(reasons[:4]) or "decomposition content scored"


def score_allocate(content: str) -> Tuple[int, str]:
    score = 0
    reasons: List[str] = []
    lower = content.casefold()
    if "# allocation output" in lower:
        score += 15
        reasons.append("allocation output present")
    sequence = section_text(content, "Sequence of Operations")
    if sequence:
        score += 20
        reasons.append("sequence of operations present")
    subtask_count = len(re.findall(r"\bsubtask\b", content, flags=re.IGNORECASE))
    if subtask_count:
        score += min(subtask_count, 10) * 2
        reasons.append("subtasks covered")
    if re.search(r"\brobot\s*\d*|robot\d+\b", content, flags=re.IGNORECASE):
        score += 8
        reasons.append("robot assignment present")
    if any(marker in lower for marker in ["can do", "assigned", "allocate", "capable"]):
        score += 5
        reasons.append("assignment rationale")
    score += min(len(content) // 1000, 8)
    return score, ", ".join(reasons[:4]) or "allocation content scored"


def score_problem_generation(content: str, metadata: Dict[str, Any]) -> Tuple[int, str]:
    score = 0
    reasons: List[str] = []
    raw_problem = section_text(content, "Generated Problem")
    validated_problem = section_text(content, "Validated Problem")
    plan_text = section_text(content, "Plan")
    problem_text = "\n".join([raw_problem, validated_problem])
    if "(define (problem" in raw_problem.casefold():
        score += 15
        reasons.append("generated problem present")
    if "(define (problem" in validated_problem.casefold():
        score += 25
        reasons.append("validated problem present")
    if ":init" in problem_text.casefold():
        score += 8
        reasons.append("init facts present")
    if ":goal" in problem_text.casefold():
        score += 8
        reasons.append("goal present")
    if metadata.get("planner_return_code") == 0:
        score += 20
        reasons.append("planner succeeded")
    if metadata.get("validation_status"):
        score += 5
        reasons.append("validation recorded")
    if plan_text and count_action_like_lines(plan_text):
        score += 15
        reasons.append("plan actions present")
    score += min(len(content) // 1200, 8)
    return score, ", ".join(reasons[:5]) or "problem-generation content scored"


def build_doc_signals(doc: Dict[str, Any]) -> DocSignals:
    doc_id = str(doc.get("id", ""))
    stage = str(doc.get("stage", ""))
    quality = str(doc.get("quality", ""))
    retrieval_eligible = bool(doc.get("retrieval_eligible"))
    content = str(doc.get("content", ""))
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}

    if stage == "decompose":
        content_score, reason = score_decompose(content)
    elif stage == "allocate":
        content_score, reason = score_allocate(content)
    elif stage == "problem_generation":
        content_score, reason = score_problem_generation(content, metadata)
    else:
        content_score = min(len(content) // 1000, 10)
        reason = "generic content scored"

    return DocSignals(
        doc_id=doc_id,
        stage=stage,
        quality=quality,
        retrieval_eligible=retrieval_eligible,
        content_score=content_score,
        content_length=len(content),
        reason=reason,
    )


def load_doc_signals(corpus_path: Path, doc_ids: Set[str]) -> Dict[str, DocSignals]:
    if not doc_ids:
        return {}

    signals: Dict[str, DocSignals] = {}
    with corpus_path.open("rb") as file_obj:
        for line in file_obj:
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            doc_id = doc.get("id")
            if isinstance(doc_id, str) and doc_id in doc_ids:
                signals[doc_id] = build_doc_signals(doc)
                if len(signals) == len(doc_ids):
                    break
    return signals


def member_quality(member: Dict[str, Any], signals: Dict[str, DocSignals]) -> str:
    value = member.get("quality")
    if isinstance(value, str) and value:
        return value
    doc_id = str(member.get("doc_id", ""))
    signal = signals.get(doc_id)
    return signal.quality if signal else ""


def member_retrieval_eligible(member: Dict[str, Any], signals: Dict[str, DocSignals]) -> bool:
    value = member.get("retrieval_eligible")
    if isinstance(value, bool):
        return value
    doc_id = str(member.get("doc_id", ""))
    signal = signals.get(doc_id)
    return bool(signal.retrieval_eligible) if signal else False


def safe_similarity(member: Dict[str, Any]) -> float:
    value = member.get("similarity_to_representative")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def candidate_sort_key(
    member: Dict[str, Any],
    cluster: Dict[str, Any],
    signals: Dict[str, DocSignals],
) -> Tuple[int, int, int, int, float, str]:
    doc_id = str(member.get("doc_id", ""))
    quality = member_quality(member, signals)
    signal = signals.get(doc_id)
    return (
        -QUALITY_RANK.get(quality, 0),
        -(1 if member_retrieval_eligible(member, signals) else 0),
        -(signal.content_score if signal else 0),
        -(1 if doc_id == cluster.get("representative_doc_id") else 0),
        -safe_similarity(member),
        doc_id,
    )


def choose_winner(
    cluster: Dict[str, Any],
    judge_agent: str,
    signals: Dict[str, DocSignals],
) -> Dict[str, Any]:
    members = [member for member in cluster_members(cluster) if isinstance(member, dict)]
    if not members:
        raise ValueError(f"Cluster {cluster.get('id')} has no members")

    winner = sorted(members, key=lambda member: candidate_sort_key(member, cluster, signals))[0]
    winner_doc_id = str(winner.get("doc_id", ""))
    signal = signals.get(winner_doc_id)
    quality = member_quality(winner, signals)
    retrieval_eligible = member_retrieval_eligible(winner, signals)
    reason_parts: List[str] = []
    if quality == "success" and retrieval_eligible:
        reason_parts.append("success/retrieval_eligible")
    elif quality:
        reason_parts.append(f"{quality}/{'retrieval_eligible' if retrieval_eligible else 'not_retrieval_eligible'}")
    else:
        reason_parts.append("quality unknown")
    if signal:
        reason_parts.append(signal.reason)
    else:
        reason_parts.append("content missing from corpus scan")
    if winner_doc_id == cluster.get("representative_doc_id"):
        reason_parts.append("representative_doc_id")
    else:
        reason_parts.append(f"similarity={safe_similarity(winner):.4f}")

    return {
        "cluster_id": cluster.get("id"),
        "stage": cluster.get("stage"),
        "winner_doc_id": winner_doc_id,
        "decision": "keep",
        "judge_agent": judge_agent,
        "reason": "; ".join(reason_parts),
        "reviewed_member_count": len(members),
        "member_count": cluster_member_count(cluster),
        "representative_doc_id": cluster.get("representative_doc_id"),
        "winner_quality": quality,
        "winner_retrieval_eligible": retrieval_eligible,
        "winner_similarity_to_representative": safe_similarity(winner),
        "winner_content_score": signal.content_score if signal else 0,
    }


def summarize_assignment(agent_name: str, clusters: Sequence[Dict[str, Any]], output_path: Path) -> Dict[str, Any]:
    stage_counts = Counter(str(cluster.get("stage", "")) for cluster in clusters)
    return {
        "judge_agent": agent_name,
        "cluster_count": len(clusters),
        "reviewed_member_count": sum(cluster_member_count(cluster) for cluster in clusters),
        "stage_counts": dict(sorted(stage_counts.items())),
        "largest_cluster_member_count": max([cluster_member_count(cluster) for cluster in clusters] or [0]),
        "output_path": str(output_path),
    }


def validate_records(
    records: Sequence[Dict[str, Any]],
    processable_clusters: Sequence[Dict[str, Any]],
    skipped_clusters: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    cluster_by_id = {str(cluster.get("id")): cluster for cluster in processable_clusters}
    record_cluster_ids = [str(record.get("cluster_id")) for record in records]
    duplicate_cluster_ids = sorted(
        cluster_id for cluster_id, count in Counter(record_cluster_ids).items() if count > 1
    )
    missing_cluster_ids = sorted(set(cluster_by_id).difference(record_cluster_ids))
    skipped_cluster_ids = {str(item.get("cluster_id")) for item in skipped_clusters}
    processed_skipped_cluster_ids = sorted(skipped_cluster_ids.intersection(record_cluster_ids))

    winner_not_in_cluster: List[str] = []
    for record in records:
        cluster_id = str(record.get("cluster_id"))
        cluster = cluster_by_id.get(cluster_id)
        if not cluster:
            continue
        member_doc_ids = {
            str(member.get("doc_id"))
            for member in cluster_members(cluster)
            if isinstance(member, dict)
        }
        if str(record.get("winner_doc_id")) not in member_doc_ids:
            winner_not_in_cluster.append(cluster_id)

    return {
        "expected_record_count": len(processable_clusters),
        "actual_record_count": len(records),
        "duplicate_cluster_ids": duplicate_cluster_ids,
        "missing_cluster_ids": missing_cluster_ids,
        "winner_not_in_cluster": sorted(winner_not_in_cluster),
        "processed_skipped_cluster_ids": processed_skipped_cluster_ids,
        "ok": (
            len(records) == len(processable_clusters)
            and not duplicate_cluster_ids
            and not missing_cluster_ids
            and not winner_not_in_cluster
            and not processed_skipped_cluster_ids
        ),
    }


def run(argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    args = parse_args(argv)
    clusters_path = Path(args.clusters_path).expanduser()
    corpus_path = Path(args.corpus_path).expanduser()
    output_jsonl = Path(args.output_jsonl).expanduser()
    summary_path = Path(args.summary_path).expanduser()
    shards_dir = Path(args.shards_dir).expanduser()
    skip_cluster_ids = {cluster_id for cluster_id in args.skip_cluster_id if cluster_id}

    processable_clusters, skipped_clusters, singleton_count = load_cluster_plan(
        clusters_path,
        skip_cluster_ids,
    )
    assignments = assign_clusters(processable_clusters, args.agent_count)
    if args.agent_index is not None and not 1 <= args.agent_index <= args.agent_count:
        raise ValueError("--agent-index must be between 1 and --agent-count")

    assignment_indexes = (
        [args.agent_index - 1] if args.agent_index is not None else list(range(args.agent_count))
    )
    selected_clusters = [
        cluster
        for index in assignment_indexes
        for cluster in assignments[index]
    ]
    signals = load_doc_signals(corpus_path, required_doc_ids(selected_clusters))

    all_records: List[Dict[str, Any]] = []
    shard_summaries: List[Dict[str, Any]] = []
    for index in assignment_indexes:
        agent_name = f"agent_{index + 1:02d}"
        shard_path = shards_dir / f"pddlrun_cluster_winners_{agent_name}.jsonl"
        shard_records = [
            choose_winner(cluster, agent_name, signals)
            for cluster in sorted(assignments[index], key=lambda item: str(item.get("id", "")))
        ]
        write_jsonl(shard_path, shard_records)
        all_records.extend(shard_records)
        shard_summaries.append(summarize_assignment(agent_name, assignments[index], shard_path))

    if args.agent_index is None:
        all_records.sort(key=lambda record: str(record.get("cluster_id", "")))
        write_jsonl(output_jsonl, all_records)
        validation = validate_records(all_records, processable_clusters, skipped_clusters)
        missing_doc_count = len(required_doc_ids(processable_clusters).difference(signals))
        summary = {
            "version": 1,
            "clusters_path": str(clusters_path),
            "corpus_path": str(corpus_path),
            "output_jsonl": str(output_jsonl),
            "summary_path": str(summary_path),
            "shards_dir": str(shards_dir),
            "agent_count": args.agent_count,
            "processed_cluster_count": len(processable_clusters),
            "processed_member_count": sum(cluster_member_count(cluster) for cluster in processable_clusters),
            "singleton_cluster_count": singleton_count,
            "skipped_clusters": skipped_clusters,
            "skipped_cluster_count": len(skipped_clusters),
            "skip_cluster_ids": sorted(skip_cluster_ids),
            "doc_signal_count": len(signals),
            "missing_doc_signal_count": missing_doc_count,
            "agent_shards": [
                summarize_assignment(
                    f"agent_{index + 1:02d}",
                    assignments[index],
                    shards_dir / f"pddlrun_cluster_winners_agent_{index + 1:02d}.jsonl",
                )
                for index in range(args.agent_count)
            ],
            "validation": validation,
        }
        write_json(summary_path, summary)
        return summary

    shard_summary_path = shards_dir / f"pddlrun_cluster_winners_agent_{args.agent_index:02d}_summary.json"
    summary = {
        "version": 1,
        "agent_count": args.agent_count,
        "agent_index": args.agent_index,
        "processed_cluster_count": len(selected_clusters),
        "processed_member_count": sum(cluster_member_count(cluster) for cluster in selected_clusters),
        "skipped_clusters": skipped_clusters,
        "doc_signal_count": len(signals),
        "missing_doc_signal_count": len(required_doc_ids(selected_clusters).difference(signals)),
        "agent_shards": shard_summaries,
    }
    write_json(shard_summary_path, summary)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    summary = run(argv)
    if "validation" in summary and not summary["validation"]["ok"]:
        print(json.dumps(summary["validation"], ensure_ascii=False, indent=2))
        return 1
    print(
        "Wrote "
        f"{summary['processed_cluster_count']} cluster winner(s) "
        f"across {len(summary['agent_shards'])} shard(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
