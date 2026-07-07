"""Local SQLite-backed RAG retrieval for task decomposition prompts."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from run_config import RunConfig


SCHEMA_VERSION = 1
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
DEFAULT_MAX_QUERY_TOKENS = 12
DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0
DEFAULT_CANDIDATE_LIMIT = 5000
RAG_PROMPT_TITLE = "# Retrieved Task Decomposition Examples (RAG)"
DECOMPOSE_RAG_SAFETY_RULES = (
    "# Use these examples only as few-shot references for decomposition structure and reasoning style.\n"
    "# Do not copy object names, robot tokens, floor-plan facts, or PDDL facts "
    "unless they are present in the current task context."
)
ALLOCATE_RAG_SAFETY_RULES = (
    "# Use these examples only as few-shot references for allocation reasoning style and output format.\n"
    "# Do not copy object names, robot tokens, floor-plan facts, or PDDL facts "
    "unless they are present in the current task context."
)
RAG_SAFETY_RULES = DECOMPOSE_RAG_SAFETY_RULES
LOW_VALUE_QUERY_TOKENS = {
    "a",
    "an",
    "analyze",
    "and",
    "are",
    "as",
    "at",
    "available",
    "based",
    "by",
    "content",
    "context",
    "current",
    "condition",
    "coverage",
    "due",
    "effects",
    "false",
    "for",
    "from",
    "full",
    "in",
    "initial",
    "is",
    "it",
    "json",
    "key",
    "mass",
    "mass_capacity",
    "metadata",
    "name",
    "no_skills",
    "none",
    "null",
    "object",
    "objects",
    "of",
    "on",
    "or",
    "parameters",
    "pddl",
    "please",
    "preconditions",
    "previous",
    "problem",
    "quality",
    "query",
    "required",
    "robot",
    "robots",
    "skill",
    "skills",
    "stage",
    "states",
    "subtask",
    "subtasks",
    "summaries",
    "summary",
    "success",
    "task",
    "the",
    "then",
    "to",
    "true",
    "using",
    "value",
    "with",
}
ROBOT_SKILL_QUERY_TOKENS = {
    "breakegg",
    "breakobject",
    "closeobject",
    "coldobject",
    "cookbystoveburner",
    "fillwater",
    "gotoobject",
    "heatbystoveburner",
    "openobject",
    "pickupobject",
    "putobject",
    "runcoffeemachine",
    "runmicrowave",
    "sliceobject",
    "switchoff",
    "switchon",
}


class PDDLRagError(Exception):
    """Raised when the local PDDL RAG store cannot be loaded or queried."""


class PDDLRagTimeoutError(PDDLRagError):
    """Raised when a local PDDL RAG query exceeds its configured timeout."""


@dataclass(frozen=True)
class PDDLRagExample:
    doc_id: str
    stage: str
    quality: str
    retrieval_eligible: bool
    task: str
    query_text: str
    content: str
    metadata: Dict[str, Any]
    score: float

    def to_manifest_record(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "stage": self.stage,
            "quality": self.quality,
            "retrieval_eligible": self.retrieval_eligible,
            "task": self.task,
            "score": self.score,
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _quality_values(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence):
        values = [str(item).strip() for item in value]
    else:
        values = ["success"]
    return tuple(item for item in values if item) or ("success",)


def _is_low_value_query_token(token: str, stage: Optional[str] = None) -> bool:
    if len(token) <= 1:
        return True
    if token.isdigit():
        return True
    if re.fullmatch(r"robot\d+", token):
        return True
    if token in LOW_VALUE_QUERY_TOKENS:
        return True
    if str(stage or "").lower() == "allocate":
        return False
    return token in ROBOT_SKILL_QUERY_TOKENS


def _tokenize_query(
    text: str,
    limit: int = DEFAULT_MAX_QUERY_TOKENS,
    stage: Optional[str] = None,
) -> List[str]:
    seen = set()
    tokens: List[str] = []
    for raw_token in TOKEN_RE.findall(text):
        token = raw_token.lower()
        if token in seen or _is_low_value_query_token(token, stage=stage):
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _source_signature(corpus_path: Path, index_path: Path) -> str:
    def stat_record(path: Path) -> Dict[str, Any]:
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    return json.dumps(
        {
            "corpus": stat_record(corpus_path),
            "index": stat_record(index_path),
        },
        sort_keys=True,
    )


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _rag_safety_rules(stage: str) -> str:
    if str(stage).lower() == "allocate":
        return ALLOCATE_RAG_SAFETY_RULES
    return DECOMPOSE_RAG_SAFETY_RULES


class PDDLRagRetriever:
    """Retrieve task-decomposition examples using a local SQLite FTS cache."""

    def __init__(
        self,
        corpus_path: Path,
        index_path: Path,
        runtime_db_path: Path,
        *,
        quality: Sequence[str] = ("success",),
        retrieval_eligible_only: bool = True,
        top_k: int = 3,
        max_example_chars: int = 3000,
        max_block_chars: int = 8000,
        max_query_tokens: int = DEFAULT_MAX_QUERY_TOKENS,
        query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    ):
        self.corpus_path = corpus_path
        self.index_path = index_path
        self.runtime_db_path = runtime_db_path
        self.quality = tuple(quality) or ("success",)
        self.retrieval_eligible_only = retrieval_eligible_only
        self.top_k = top_k
        self.max_example_chars = max_example_chars
        self.max_block_chars = max_block_chars
        self.max_query_tokens = max_query_tokens
        self.query_timeout_seconds = query_timeout_seconds
        self._validated = False

    @classmethod
    def from_config(
        cls,
        config: RunConfig,
        section: str = "decompose_rag",
    ) -> Optional["PDDLRagRetriever"]:
        if not _as_bool(config.get(section, "enabled", False)):
            return None

        retriever = cls(
            corpus_path=config.path(section, "corpus_path"),
            index_path=config.path(section, "index_path"),
            runtime_db_path=config.path(section, "runtime_db_path"),
            quality=_quality_values(config.get(section, "quality", ["success"])),
            retrieval_eligible_only=_as_bool(config.get(section, "retrieval_eligible_only", True)),
            top_k=_as_positive_int(config.get(section, "top_k", 3), 3),
            max_example_chars=_as_positive_int(config.get(section, "max_example_chars", 3000), 3000),
            max_block_chars=_as_positive_int(config.get(section, "max_block_chars", 8000), 8000),
            max_query_tokens=_as_positive_int(
                config.get(section, "max_query_tokens", DEFAULT_MAX_QUERY_TOKENS),
                DEFAULT_MAX_QUERY_TOKENS,
            ),
            query_timeout_seconds=_as_positive_float(
                config.get(section, "query_timeout_seconds", DEFAULT_QUERY_TIMEOUT_SECONDS),
                DEFAULT_QUERY_TIMEOUT_SECONDS,
            ),
        )
        retriever._validate_sources()
        return retriever

    def query_tokens(self, query_text: str, stage: Optional[str] = None) -> List[str]:
        return _tokenize_query(query_text, self.max_query_tokens, stage=stage)

    def retrieve(self, stage: str, query_text: str, top_k: Optional[int] = None) -> List[PDDLRagExample]:
        self.ensure_runtime_db()
        tokens = self.query_tokens(query_text, stage=stage)
        if not tokens:
            return []

        limit = top_k if top_k is not None else self.top_k
        limit = max(1, int(limit))
        match_query = " OR ".join(tokens)
        quality_placeholders = ",".join("?" for _ in self.quality)
        candidate_limit = max(DEFAULT_CANDIDATE_LIMIT, limit * 500)
        where_parts = [
            f"docs.quality IN ({quality_placeholders})",
        ]
        params: List[Any] = [match_query, candidate_limit, *self.quality]
        if self.retrieval_eligible_only:
            where_parts.append("docs.retrieval_eligible = 1")

        sql = (
            "WITH candidates(rowid) AS ("
            "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? LIMIT ?"
            ") "
            "SELECT docs.doc_id, docs.stage, docs.quality, docs.retrieval_eligible, "
            "docs.task, docs.query_text, docs.content, docs.metadata_json, "
            "0.0 AS score "
            "FROM candidates JOIN docs ON candidates.rowid = docs.rowid "
            f"WHERE {' AND '.join(where_parts)}"
        )

        try:
            with sqlite3.connect(self.runtime_db_path) as connection:
                rows = self._execute_query_with_timeout(connection, sql, params)
        except PDDLRagTimeoutError:
            raise
        except sqlite3.Error as exc:
            raise PDDLRagError(f"Failed to query RAG runtime DB {self.runtime_db_path}: {exc}") from exc

        ranked_rows = self._rank_rows(rows, tokens, limit)
        return [self._row_to_example(row) for row in ranked_rows]

    def _execute_query_with_timeout(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: Sequence[Any],
    ) -> List[Tuple[Any, ...]]:
        timeout_seconds = self.query_timeout_seconds
        if timeout_seconds <= 0:
            return connection.execute(sql, params).fetchall()

        deadline = time.monotonic() + timeout_seconds

        def abort_when_expired() -> int:
            return 1 if time.monotonic() >= deadline else 0

        connection.set_progress_handler(abort_when_expired, 10000)
        try:
            return connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise PDDLRagTimeoutError(
                    f"RAG query exceeded {timeout_seconds:g}s for runtime DB {self.runtime_db_path}"
                ) from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)

    def _rank_rows(
        self,
        rows: Sequence[Tuple[Any, ...]],
        tokens: Sequence[str],
        limit: int,
    ) -> List[Tuple[Any, ...]]:
        scored_rows: List[Tuple[Any, ...]] = []
        for row in rows:
            haystack = " ".join(str(value).lower() for value in (row[4], row[5], row[6]))
            match_count = sum(haystack.count(token) for token in tokens)
            unique_count = sum(1 for token in tokens if token in haystack)
            score = float(-(unique_count * 1000 + match_count))
            scored_rows.append((*row[:8], score))

        scored_rows.sort(key=lambda item: (item[8], str(item[0])))
        return scored_rows[:limit]

    def format_prompt_block(self, stage: str, examples: Sequence[PDDLRagExample]) -> str:
        if not examples:
            return ""

        parts = [_rag_safety_rules(stage)]
        for example in examples[:3]:
            parts.extend(
                [
                    "\n# Example",
                    example.content,
                ]
            )
        return "\n".join(parts).strip() + "\n"

    def _validate_sources(self) -> None:
        if self._validated:
            return
        missing = [
            str(path)
            for path in (self.corpus_path, self.index_path)
            if not path.exists()
        ]
        if missing:
            raise PDDLRagError("Missing PDDL RAG source file(s): " + ", ".join(missing))
        self._validated = True

    def ensure_runtime_db(self) -> None:
        self._validate_sources()
        signature = _source_signature(self.corpus_path, self.index_path)
        if self._db_is_current(signature):
            return
        self._build_runtime_db(signature)

    def _db_is_current(self, signature: str) -> bool:
        if not self.runtime_db_path.exists():
            return False

        try:
            with sqlite3.connect(self.runtime_db_path) as connection:
                rows = dict(connection.execute("SELECT key, value FROM rag_metadata").fetchall())
        except sqlite3.Error:
            return False

        return (
            rows.get("schema_version") == str(SCHEMA_VERSION)
            and rows.get("source_signature") == signature
        )

    def _build_runtime_db(self, signature: str) -> None:
        self.runtime_db_path.parent.mkdir(parents=True, exist_ok=True)
        temp_db_path = self.runtime_db_path.with_name(self.runtime_db_path.name + ".tmp")
        if temp_db_path.exists():
            temp_db_path.unlink()

        try:
            with sqlite3.connect(temp_db_path) as connection:
                self._initialize_schema(connection)
                self._insert_corpus_documents(connection)
                connection.execute(
                    "INSERT INTO rag_metadata(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                connection.execute(
                    "INSERT INTO rag_metadata(key, value) VALUES (?, ?)",
                    ("source_signature", signature),
                )
                connection.commit()
        except Exception:
            if temp_db_path.exists():
                temp_db_path.unlink()
            raise

        temp_db_path.replace(self.runtime_db_path)

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute(
            "CREATE TABLE rag_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE docs ("
            "rowid INTEGER PRIMARY KEY, "
            "doc_id TEXT NOT NULL UNIQUE, "
            "stage TEXT NOT NULL, "
            "quality TEXT NOT NULL, "
            "retrieval_eligible INTEGER NOT NULL, "
            "task TEXT NOT NULL, "
            "query_text TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "metadata_json TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX docs_stage_quality_idx ON docs(stage, quality, retrieval_eligible)")
        connection.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(search_text, content='')")

    def _insert_corpus_documents(self, connection: sqlite3.Connection) -> None:
        with self.corpus_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PDDLRagError(
                        f"Invalid JSON in RAG corpus {self.corpus_path} line {line_number}: {exc}"
                    ) from exc

                metadata = doc.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                doc_id = str(doc.get("id", ""))
                if not doc_id:
                    continue
                query_text = str(doc.get("query_text", ""))
                content = str(doc.get("content", ""))
                cursor = connection.execute(
                    "INSERT INTO docs("
                    "doc_id, stage, quality, retrieval_eligible, task, query_text, content, metadata_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        str(doc.get("stage", "")),
                        str(doc.get("quality", "")),
                        1 if doc.get("retrieval_eligible") else 0,
                        str(metadata.get("task", "")),
                        query_text,
                        content,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                search_text = f"{query_text}\n{content}"
                connection.execute(
                    "INSERT INTO docs_fts(rowid, search_text) VALUES (?, ?)",
                    (cursor.lastrowid, search_text),
                )

    def _row_to_example(self, row: Tuple[Any, ...]) -> PDDLRagExample:
        try:
            metadata = json.loads(row[7])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return PDDLRagExample(
            doc_id=str(row[0]),
            stage=str(row[1]),
            quality=str(row[2]),
            retrieval_eligible=bool(row[3]),
            task=str(row[4]),
            query_text=str(row[5]),
            content=str(row[6]),
            metadata=metadata,
            score=float(row[8]),
        )
