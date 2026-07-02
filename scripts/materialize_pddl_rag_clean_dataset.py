#!/usr/bin/env python3
"""Materialize a deduplicated PDDL RAG corpus from cluster winner decisions."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_corpus.jsonl"
DEFAULT_CLUSTERS_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_similarity_clusters.json"
DEFAULT_WINNERS_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_cluster_winners.jsonl"
DEFAULT_CLEAN_CORPUS_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_clean_corpus.jsonl"
DEFAULT_CLEAN_INDEX_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_clean_index.json"
DEFAULT_CLEAN_SUMMARY_PATH = REPO_ROOT / "data" / "rag" / "pddlrun_clean_summary.json"
DEFAULT_SKIP_CLUSTER_IDS = ("cluster:problem_generation:000001",)
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class CleanDatasetError(ValueError):
    """Raised when cluster winners cannot produce a valid clean dataset."""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a clean PDDL RAG corpus by keeping singleton docs and cluster winners."
    )
    parser.add_argument("--corpus-path", default=str(DEFAULT_CORPUS_PATH))
    parser.add_argument("--clusters-path", default=str(DEFAULT_CLUSTERS_PATH))
    parser.add_argument("--winners-path", default=str(DEFAULT_WINNERS_PATH))
    parser.add_argument("--clean-corpus-path", default=str(DEFAULT_CLEAN_CORPUS_PATH))
    parser.add_argument("--clean-index-path", default=str(DEFAULT_CLEAN_INDEX_PATH))
    parser.add_argument("--clean-summary-path", default=str(DEFAULT_CLEAN_SUMMARY_PATH))
    parser.add_argument(
        "--skip-cluster-id",
        action="append",
        default=list(DEFAULT_SKIP_CLUSTER_IDS),
        help=(
            "Cluster id to exclude entirely. Can be passed more than once. "
            "Defaults to cluster:problem_generation:000001."
        ),
    )
    parser.add_argument(
        "--expected-clean-count",
        type=int,
        default=123296,
        help="Expected number of documents in the clean corpus. Use 0 to disable.",
    )
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def write_json(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def tokenize(text: str) -> Counter:
    tokens = [token.lower() for token in TOKEN_RE.findall(text)]
    return Counter(token for token in tokens if len(token) > 1)


def cluster_members(cluster: Dict[str, Any]) -> List[Dict[str, Any]]:
    members = cluster.get("members")
    return members if isinstance(members, list) else []


def member_doc_ids(cluster: Dict[str, Any]) -> List[str]:
    return [
        str(member.get("doc_id"))
        for member in cluster_members(cluster)
        if isinstance(member, dict) and member.get("doc_id")
    ]


def cluster_member_count(cluster: Dict[str, Any]) -> int:
    value = cluster.get("member_count")
    if isinstance(value, int):
        return value
    return len(member_doc_ids(cluster))


def read_winners(path: Path) -> Dict[str, Dict[str, Any]]:
    winners: Dict[str, Dict[str, Any]] = {}
    duplicates: List[str] = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line_number, line in enumerate(file_obj, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            cluster_id = str(record.get("cluster_id", ""))
            if not cluster_id:
                raise CleanDatasetError(f"Winner record on line {line_number} has no cluster_id")
            if cluster_id in winners:
                duplicates.append(cluster_id)
            winners[cluster_id] = record
    if duplicates:
        raise CleanDatasetError(f"Duplicate winner records: {sorted(set(duplicates))[:10]}")
    return winners


def load_clusters(path: Path) -> List[Dict[str, Any]]:
    data = read_json(path)
    clusters = data.get("clusters") if isinstance(data, dict) else data
    if not isinstance(clusters, list):
        raise CleanDatasetError(f"{path} does not contain a clusters list")
    return [cluster for cluster in clusters if isinstance(cluster, dict)]


def build_keep_plan(
    clusters: Sequence[Dict[str, Any]],
    winners: Dict[str, Dict[str, Any]],
    skip_cluster_ids: Set[str],
) -> Tuple[Set[str], Set[str], Dict[str, Any]]:
    keep_doc_ids: Set[str] = set()
    skipped_doc_ids: Set[str] = set()
    all_cluster_doc_ids: Set[str] = set()
    expected_winner_cluster_ids: Set[str] = set()
    singleton_cluster_count = 0
    singleton_doc_count = 0
    multicluster_count = 0
    multicluster_member_count = 0
    skipped_clusters: List[Dict[str, Any]] = []
    invalid_winner_cluster_ids: List[str] = []
    clusters_missing_members: List[str] = []

    for cluster in clusters:
        cluster_id = str(cluster.get("id", ""))
        doc_ids = member_doc_ids(cluster)
        member_count = cluster_member_count(cluster)
        all_cluster_doc_ids.update(doc_ids)

        if cluster_id in skip_cluster_ids:
            skipped_doc_ids.update(doc_ids)
            skipped_clusters.append(
                {
                    "cluster_id": cluster_id,
                    "stage": cluster.get("stage"),
                    "member_count": member_count,
                    "doc_count": len(doc_ids),
                    "reason": "explicit_skip",
                }
            )
            continue

        if member_count <= 1:
            if len(doc_ids) != 1:
                clusters_missing_members.append(cluster_id)
                continue
            keep_doc_ids.add(doc_ids[0])
            singleton_cluster_count += 1
            singleton_doc_count += 1
            continue

        expected_winner_cluster_ids.add(cluster_id)
        multicluster_count += 1
        multicluster_member_count += len(doc_ids)
        winner = winners.get(cluster_id)
        if not winner:
            continue
        winner_doc_id = str(winner.get("winner_doc_id", ""))
        if winner_doc_id not in set(doc_ids):
            invalid_winner_cluster_ids.append(cluster_id)
            continue
        keep_doc_ids.add(winner_doc_id)

    winner_cluster_ids = set(winners)
    missing_winner_cluster_ids = sorted(expected_winner_cluster_ids.difference(winner_cluster_ids))
    unexpected_winner_cluster_ids = sorted(winner_cluster_ids.difference(expected_winner_cluster_ids))
    kept_skipped_doc_ids = sorted(keep_doc_ids.intersection(skipped_doc_ids))
    if (
        missing_winner_cluster_ids
        or unexpected_winner_cluster_ids
        or invalid_winner_cluster_ids
        or clusters_missing_members
        or kept_skipped_doc_ids
    ):
        raise CleanDatasetError(
            json.dumps(
                {
                    "missing_winner_cluster_ids": missing_winner_cluster_ids[:20],
                    "unexpected_winner_cluster_ids": unexpected_winner_cluster_ids[:20],
                    "invalid_winner_cluster_ids": sorted(invalid_winner_cluster_ids)[:20],
                    "clusters_missing_members": sorted(clusters_missing_members)[:20],
                    "kept_skipped_doc_ids": kept_skipped_doc_ids[:20],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    summary = {
        "cluster_count": len(clusters),
        "cluster_document_count": len(all_cluster_doc_ids),
        "singleton_cluster_count": singleton_cluster_count,
        "singleton_doc_count": singleton_doc_count,
        "multicluster_count": multicluster_count,
        "multicluster_member_count": multicluster_member_count,
        "multicluster_winner_count": len(expected_winner_cluster_ids),
        "skipped_clusters": skipped_clusters,
        "skipped_cluster_count": len(skipped_clusters),
        "skipped_doc_count": len(skipped_doc_ids),
        "keep_doc_count": len(keep_doc_ids),
    }
    return keep_doc_ids, skipped_doc_ids, summary


def update_index(index: Dict[str, Any], doc: Dict[str, Any]) -> None:
    doc_id = str(doc["id"])
    tokens = tokenize(f"{doc.get('query_text', '')}\n{doc.get('content', '')}")
    for token, count in sorted(tokens.items()):
        index["postings"][token].append([doc_id, count])

    metadata = safe_dict(doc.get("metadata"))
    index["documents"][doc_id] = {
        "stage": doc.get("stage"),
        "quality": doc.get("quality"),
        "retrieval_eligible": doc.get("retrieval_eligible"),
        "task": metadata.get("task"),
        "task_run_dir": metadata.get("task_run_dir"),
    }


def materialize_clean_dataset(
    corpus_path: Path,
    clean_corpus_path: Path,
    keep_doc_ids: Set[str],
    skipped_doc_ids: Set[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    clean_corpus_path.parent.mkdir(parents=True, exist_ok=True)
    kept_doc_ids: Set[str] = set()
    duplicate_corpus_doc_ids: List[str] = []
    skipped_doc_ids_seen: Set[str] = set()
    original_document_count = 0
    by_stage_quality: Dict[str, Counter] = defaultdict(Counter)
    index: Dict[str, Any] = {
        "version": 1,
        "tokenizer": "lowercase alnum underscore tokens; min length 2",
        "document_count": 0,
        "documents": {},
        "postings": defaultdict(list),
    }

    with corpus_path.open("r", encoding="utf-8") as source, clean_corpus_path.open(
        "w",
        encoding="utf-8",
    ) as output:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            doc = json.loads(line)
            doc_id = str(doc.get("id", ""))
            original_document_count += 1
            if doc_id in skipped_doc_ids:
                skipped_doc_ids_seen.add(doc_id)
                continue
            if doc_id not in keep_doc_ids:
                continue
            if doc_id in kept_doc_ids:
                duplicate_corpus_doc_ids.append(doc_id)
                continue
            kept_doc_ids.add(doc_id)
            output.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
            by_stage_quality[str(doc.get("stage"))][str(doc.get("quality"))] += 1
            update_index(index, doc)

    missing_keep_doc_ids = sorted(keep_doc_ids.difference(kept_doc_ids))
    missing_skipped_doc_ids = sorted(skipped_doc_ids.difference(skipped_doc_ids_seen))
    if missing_keep_doc_ids or duplicate_corpus_doc_ids:
        raise CleanDatasetError(
            json.dumps(
                {
                    "missing_keep_doc_ids": missing_keep_doc_ids[:20],
                    "duplicate_corpus_doc_ids": sorted(set(duplicate_corpus_doc_ids))[:20],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    index["document_count"] = len(kept_doc_ids)
    index["postings"] = dict(sorted(index["postings"].items()))
    corpus_summary = {
        "original_document_count": original_document_count,
        "clean_document_count": len(kept_doc_ids),
        "deduplicated_removed_document_count": original_document_count
        - len(kept_doc_ids)
        - len(skipped_doc_ids_seen),
        "skipped_doc_ids_seen_count": len(skipped_doc_ids_seen),
        "missing_skipped_doc_ids_count": len(missing_skipped_doc_ids),
        "missing_skipped_doc_ids": missing_skipped_doc_ids[:20],
        "by_stage_quality": {
            stage: dict(sorted(counter.items()))
            for stage, counter in sorted(by_stage_quality.items())
        },
    }
    return index, corpus_summary


def run(argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    args = parse_args(argv)
    corpus_path = Path(args.corpus_path).expanduser()
    clusters_path = Path(args.clusters_path).expanduser()
    winners_path = Path(args.winners_path).expanduser()
    clean_corpus_path = Path(args.clean_corpus_path).expanduser()
    clean_index_path = Path(args.clean_index_path).expanduser()
    clean_summary_path = Path(args.clean_summary_path).expanduser()
    skip_cluster_ids = {cluster_id for cluster_id in args.skip_cluster_id if cluster_id}

    winners = read_winners(winners_path)
    clusters = load_clusters(clusters_path)
    keep_doc_ids, skipped_doc_ids, cluster_summary = build_keep_plan(
        clusters,
        winners,
        skip_cluster_ids,
    )
    index, corpus_summary = materialize_clean_dataset(
        corpus_path,
        clean_corpus_path,
        keep_doc_ids,
        skipped_doc_ids,
    )

    clean_document_count = corpus_summary["clean_document_count"]
    expected_clean_count = args.expected_clean_count if args.expected_clean_count > 0 else None
    validation = {
        "expected_clean_count": expected_clean_count,
        "actual_clean_count": clean_document_count,
        "clean_count_matches_expected": (
            expected_clean_count is None or clean_document_count == expected_clean_count
        ),
        "skipped_docs_not_kept": not keep_doc_ids.intersection(skipped_doc_ids),
        "index_document_count_matches": index["document_count"] == clean_document_count,
        "ok": False,
    }
    validation["ok"] = all(
        [
            validation["clean_count_matches_expected"],
            validation["skipped_docs_not_kept"],
            validation["index_document_count_matches"],
            corpus_summary["missing_skipped_doc_ids_count"] == 0,
        ]
    )
    if not validation["ok"]:
        raise CleanDatasetError(json.dumps(validation, ensure_ascii=False, sort_keys=True))

    summary = {
        "version": 1,
        "corpus_path": str(corpus_path),
        "clusters_path": str(clusters_path),
        "winners_path": str(winners_path),
        "clean_corpus_path": str(clean_corpus_path),
        "clean_index_path": str(clean_index_path),
        "clean_summary_path": str(clean_summary_path),
        "skip_cluster_ids": sorted(skip_cluster_ids),
        **cluster_summary,
        **corpus_summary,
        "validation": validation,
    }
    write_json(clean_index_path, index)
    write_json(clean_summary_path, summary)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        summary = run(argv)
    except CleanDatasetError as exc:
        print(f"Failed to materialize clean PDDL RAG dataset: {exc}")
        return 1

    print(
        "Wrote clean PDDL RAG dataset with "
        f"{summary['clean_document_count']} documents to {summary['clean_corpus_path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
