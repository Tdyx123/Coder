import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from judge_pddl_rag_clusters import assign_clusters, run


def write_json(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for record in records:
            file_obj.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def doc(doc_id, stage, quality, eligible, content, metadata=None):
    return {
        "id": doc_id,
        "stage": stage,
        "quality": quality,
        "retrieval_eligible": eligible,
        "query_text": "Task: test",
        "content": content,
        "metadata": metadata or {},
    }


def member(doc_id, quality, eligible, similarity):
    return {
        "doc_id": doc_id,
        "quality": quality,
        "retrieval_eligible": eligible,
        "similarity_to_representative": similarity,
    }


class JudgePDDLRagClustersTest(unittest.TestCase):
    def test_assigns_clusters_by_member_count(self):
        clusters = [
            {"id": "c1", "member_count": 7, "members": []},
            {"id": "c2", "member_count": 4, "members": []},
            {"id": "c3", "member_count": 3, "members": []},
            {"id": "c4", "member_count": 2, "members": []},
        ]

        assignments = assign_clusters(clusters, 2)
        loads = sorted(sum(cluster["member_count"] for cluster in shard) for shard in assignments)

        self.assertEqual(loads, [7, 9])

    def test_writes_winners_summary_and_skips_configured_cluster(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "corpus.jsonl"
            clusters_path = root / "clusters.json"
            output_jsonl = root / "winners.jsonl"
            summary_path = root / "summary.json"
            shards_dir = root / "shards"

            write_jsonl(
                corpus_path,
                [
                    doc(
                        "allocate:a",
                        "allocate",
                        "partial",
                        False,
                        "# Allocation Output\npartial\n# Sequence of Operations\nSubtask 1: robot1",
                    ),
                    doc(
                        "allocate:b",
                        "allocate",
                        "success",
                        True,
                        "# Allocation Output\nRobot 1 can do it.\n# Sequence of Operations\nSubtask 1: robot1",
                    ),
                    doc(
                        "problem_generation:a",
                        "problem_generation",
                        "success",
                        True,
                        "# Generated Problem\n(define (problem raw) (:init) (:goal))\n"
                        "# Validated Problem\n(define (problem valid) (:init) (:goal))\n"
                        "# Plan\n(gotoobject robot1 apple)",
                        {"planner_return_code": 0, "validation_status": "ok"},
                    ),
                    doc(
                        "problem_generation:b",
                        "problem_generation",
                        "success",
                        True,
                        "# Generated Problem\n(define (problem raw))",
                    ),
                ],
            )
            write_json(
                clusters_path,
                {
                    "clusters": [
                        {
                            "id": "cluster:allocate:000001",
                            "stage": "allocate",
                            "member_count": 2,
                            "representative_doc_id": "allocate:a",
                            "members": [
                                member("allocate:a", "partial", False, 1.0),
                                member("allocate:b", "success", True, 0.97),
                            ],
                        },
                        {
                            "id": "cluster:problem_generation:000002",
                            "stage": "problem_generation",
                            "member_count": 2,
                            "representative_doc_id": "problem_generation:b",
                            "members": [
                                member("problem_generation:a", "success", True, 0.91),
                                member("problem_generation:b", "success", True, 1.0),
                            ],
                        },
                        {
                            "id": "cluster:problem_generation:000001",
                            "stage": "problem_generation",
                            "member_count": 2,
                            "representative_doc_id": "problem_generation:b",
                            "members": [
                                member("problem_generation:a", "success", True, 0.91),
                                member("problem_generation:b", "success", True, 1.0),
                            ],
                        },
                        {
                            "id": "cluster:singleton:000001",
                            "stage": "allocate",
                            "member_count": 1,
                            "members": [member("allocate:b", "success", True, 1.0)],
                        },
                    ]
                },
            )

            summary = run(
                [
                    "--clusters-path",
                    str(clusters_path),
                    "--corpus-path",
                    str(corpus_path),
                    "--output-jsonl",
                    str(output_jsonl),
                    "--summary-path",
                    str(summary_path),
                    "--shards-dir",
                    str(shards_dir),
                    "--agent-count",
                    "2",
                ]
            )

            winners = read_jsonl(output_jsonl)
            self.assertEqual(summary["processed_cluster_count"], 2)
            self.assertEqual(summary["skipped_cluster_count"], 1)
            self.assertTrue(summary["validation"]["ok"])
            self.assertEqual(len(winners), 2)
            self.assertEqual(
                {record["cluster_id"]: record["winner_doc_id"] for record in winners},
                {
                    "cluster:allocate:000001": "allocate:b",
                    "cluster:problem_generation:000002": "problem_generation:a",
                },
            )
            self.assertEqual(read_json(summary_path)["singleton_cluster_count"], 1)
            self.assertTrue((shards_dir / "pddlrun_cluster_winners_agent_01.jsonl").exists())

    def test_agent_index_writes_only_one_shard(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "corpus.jsonl"
            clusters_path = root / "clusters.json"
            shards_dir = root / "shards"

            write_jsonl(
                corpus_path,
                [
                    doc(
                        "decompose:a",
                        "decompose",
                        "success",
                        True,
                        "# Decomposition Output\nSubtask 1\nPreconditions\nEffects\nGoToObject",
                    ),
                    doc(
                        "decompose:b",
                        "decompose",
                        "failed",
                        False,
                        "# Decomposition Output",
                    ),
                ],
            )
            write_json(
                clusters_path,
                {
                    "clusters": [
                        {
                            "id": "cluster:decompose:000001",
                            "stage": "decompose",
                            "member_count": 2,
                            "representative_doc_id": "decompose:b",
                            "members": [
                                member("decompose:a", "success", True, 0.95),
                                member("decompose:b", "failed", False, 1.0),
                            ],
                        }
                    ]
                },
            )

            summary = run(
                [
                    "--clusters-path",
                    str(clusters_path),
                    "--corpus-path",
                    str(corpus_path),
                    "--shards-dir",
                    str(shards_dir),
                    "--agent-count",
                    "2",
                    "--agent-index",
                    "1",
                ]
            )

            self.assertEqual(summary["processed_cluster_count"], 1)
            self.assertTrue((shards_dir / "pddlrun_cluster_winners_agent_01.jsonl").exists())
            self.assertFalse((root / "pddlrun_cluster_winners.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
