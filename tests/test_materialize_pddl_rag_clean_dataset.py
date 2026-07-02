import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from materialize_pddl_rag_clean_dataset import CleanDatasetError, run


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


def doc(doc_id, stage="allocate", quality="success"):
    return {
        "id": doc_id,
        "stage": stage,
        "query_text": f"Task: {doc_id}",
        "content": f"# Task\n{doc_id}\n# Content\nGoToObject robot object",
        "metadata": {
            "task": f"task {doc_id}",
            "task_run_dir": f"/tmp/{doc_id}",
        },
        "quality": quality,
        "retrieval_eligible": quality == "success",
    }


def member(doc_id):
    return {
        "doc_id": doc_id,
        "quality": "success",
        "retrieval_eligible": True,
        "similarity_to_representative": 1.0,
    }


def cluster(cluster_id, doc_ids, stage="allocate"):
    return {
        "id": cluster_id,
        "stage": stage,
        "member_count": len(doc_ids),
        "representative_doc_id": doc_ids[0],
        "members": [member(doc_id) for doc_id in doc_ids],
    }


class MaterializePDDLRagCleanDatasetTest(unittest.TestCase):
    def test_materializes_singletons_winners_and_skips_large_cluster(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "corpus.jsonl"
            clusters_path = root / "clusters.json"
            winners_path = root / "winners.jsonl"
            clean_corpus_path = root / "clean.jsonl"
            clean_index_path = root / "clean_index.json"
            clean_summary_path = root / "clean_summary.json"

            write_jsonl(
                corpus_path,
                [
                    doc("singleton:a"),
                    doc("multi:loser"),
                    doc("multi:winner"),
                    doc("skipped:a", stage="problem_generation"),
                    doc("skipped:b", stage="problem_generation"),
                ],
            )
            write_json(
                clusters_path,
                {
                    "clusters": [
                        cluster("cluster:singleton:000001", ["singleton:a"]),
                        cluster("cluster:multi:000001", ["multi:loser", "multi:winner"]),
                        cluster(
                            "cluster:problem_generation:000001",
                            ["skipped:a", "skipped:b"],
                            stage="problem_generation",
                        ),
                    ]
                },
            )
            write_jsonl(
                winners_path,
                [
                    {
                        "cluster_id": "cluster:multi:000001",
                        "winner_doc_id": "multi:winner",
                    }
                ],
            )

            summary = run(
                [
                    "--corpus-path",
                    str(corpus_path),
                    "--clusters-path",
                    str(clusters_path),
                    "--winners-path",
                    str(winners_path),
                    "--clean-corpus-path",
                    str(clean_corpus_path),
                    "--clean-index-path",
                    str(clean_index_path),
                    "--clean-summary-path",
                    str(clean_summary_path),
                    "--expected-clean-count",
                    "2",
                ]
            )

            clean_docs = read_jsonl(clean_corpus_path)
            self.assertEqual([doc["id"] for doc in clean_docs], ["singleton:a", "multi:winner"])
            self.assertEqual(summary["clean_document_count"], 2)
            self.assertEqual(summary["singleton_doc_count"], 1)
            self.assertEqual(summary["multicluster_winner_count"], 1)
            self.assertEqual(summary["skipped_doc_count"], 2)
            self.assertEqual(summary["deduplicated_removed_document_count"], 1)
            self.assertTrue(summary["validation"]["ok"])

            index = read_json(clean_index_path)
            self.assertEqual(index["document_count"], 2)
            self.assertEqual(set(index["documents"]), {"singleton:a", "multi:winner"})
            self.assertIn("gotoobject", index["postings"])
            self.assertEqual(read_json(clean_summary_path)["clean_document_count"], 2)

    def test_fails_when_winner_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "corpus.jsonl"
            clusters_path = root / "clusters.json"
            winners_path = root / "winners.jsonl"

            write_jsonl(corpus_path, [doc("a"), doc("b")])
            write_json(
                clusters_path,
                {"clusters": [cluster("cluster:multi:000001", ["a", "b"])]},
            )
            write_jsonl(winners_path, [])

            with self.assertRaises(CleanDatasetError):
                run(
                    [
                        "--corpus-path",
                        str(corpus_path),
                        "--clusters-path",
                        str(clusters_path),
                        "--winners-path",
                        str(winners_path),
                        "--clean-corpus-path",
                        str(root / "clean.jsonl"),
                        "--clean-index-path",
                        str(root / "index.json"),
                        "--clean-summary-path",
                        str(root / "summary.json"),
                        "--expected-clean-count",
                        "0",
                    ]
                )

    def test_fails_when_winner_is_not_a_cluster_member(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            corpus_path = root / "corpus.jsonl"
            clusters_path = root / "clusters.json"
            winners_path = root / "winners.jsonl"

            write_jsonl(corpus_path, [doc("a"), doc("b"), doc("outside")])
            write_json(
                clusters_path,
                {"clusters": [cluster("cluster:multi:000001", ["a", "b"])]},
            )
            write_jsonl(
                winners_path,
                [{"cluster_id": "cluster:multi:000001", "winner_doc_id": "outside"}],
            )

            with self.assertRaises(CleanDatasetError):
                run(
                    [
                        "--corpus-path",
                        str(corpus_path),
                        "--clusters-path",
                        str(clusters_path),
                        "--winners-path",
                        str(winners_path),
                        "--clean-corpus-path",
                        str(root / "clean.jsonl"),
                        "--clean-index-path",
                        str(root / "index.json"),
                        "--clean-summary-path",
                        str(root / "summary.json"),
                        "--expected-clean-count",
                        "0",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
