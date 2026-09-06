import copy
import ast
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import argparse
from pathlib import Path
from datetime import datetime
import random
import subprocess
import threading
import time
import re
import shutil
import sys
from typing import Callable, Iterable, List, Dict, Tuple, Optional, Union, Any, Set, Sequence
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

from ai2thor_object_cache import get_ai2_thor_objects_cached
from llm_client import get_available_models as get_litellm_models
from file_processor import FileProcessor, PDDLError
from llm_handler import LLMError, LLMHandler
from llm_logger import get_llm_logger
from pddl_rag import PDDLRagError, PDDLRagRetriever, PDDLRagTimeoutError
from pddl_problem_repair import ProblemRepairResult, repair_problem_pddl
from pddl_noop_audit import (
    audit_problem_initial_state,
    build_key_object_evidence,
    plan_has_actions,
    verify_zero_action_plan,
)
from parsing_utils import ParsingUtils
from run_config import (
    RunConfig,
    apply_allocate_rag_cli_override,
    apply_decompose_rag_cli_override,
    apply_problem_repair_cli_override,
    apply_problem_rag_cli_override,
    load_run_config as _load_run_config,
    normalize_floor_plan,
)
from special_task_skills import (
    SPECIAL_TASK_SKILL_ALIASES,
    SPECIAL_TASK_SKILL_PROMPT_RULE,
    robot_skill_for_special_task_skill,
)

import sys
sys.path.append(".")

import resources.actions as actions
import resources.robots as robots


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_run_config(base_path: Union[str, Path], config_path: Optional[Union[str, Path]] = None) -> RunConfig:
    """Load and resolve the shared runtime config for single and parallel runs."""
    return _load_run_config(base_path, config_path, error_cls=PDDLError)


def _match_subtask_header_line(
    line: str,
    allow_bare: bool = False,
    allow_hash_space_separator: bool = True,
) -> Optional[re.Match]:
    return ParsingUtils.match_subtask_header_line(
        line,
        allow_bare=allow_bare,
        allow_hash_space_separator=allow_hash_space_separator,
    )


def _is_subtask_header_line(
    line: str,
    allow_bare: bool = False,
    allow_hash_space_separator: bool = True,
) -> bool:
    return ParsingUtils.is_subtask_header_line(
        line,
        allow_bare=allow_bare,
        allow_hash_space_separator=allow_hash_space_separator,
    )


def _normalize_subtask_header_line(
    line: str,
    allow_bare: bool = False,
    allow_hash_space_separator: bool = True,
) -> str:
    return ParsingUtils.normalize_subtask_header_line(
        line,
        allow_bare=allow_bare,
        allow_hash_space_separator=allow_hash_space_separator,
    )


def _strip_legacy_inaction_prompt_text(text: Any) -> str:
    """Remove legacy ``inaction`` references from non-authoritative prompt text."""
    if text is None:
        return ""

    raw_text = str(text)
    keep_trailing_newline = raw_text.endswith(("\n", "\r"))

    canonical_not_pattern = re.compile(
        r"\(\s*not\s*\(\s*inaction\b[^()]*\)\s*\)",
        re.IGNORECASE,
    )
    loose_not_pattern = re.compile(
        r"\bnot\s*\(\s*inaction\b[^()]*\)",
        re.IGNORECASE,
    )
    positive_pattern = re.compile(
        r"\(\s*inaction\b[^()]*\)",
        re.IGNORECASE,
    )

    cleaned_lines: List[str] = []
    for original_line in raw_text.splitlines():
        line = canonical_not_pattern.sub("", original_line)
        line = loose_not_pattern.sub("", line)
        line = positive_pattern.sub("", line)

        # Drop prose left over from historical examples such as
        # "the robot initiates as not inaction".
        if re.search(r"\binaction\b", line, re.IGNORECASE):
            continue

        line = re.sub(r"(:)\s*,\s*", r"\1 ", line)
        line = re.sub(r",\s*,+", ",", line)
        line = re.sub(r",\s*$", "", line)

        if re.fullmatch(r"\s*Preconditions:\s*", line, re.IGNORECASE):
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}Preconditions: None."
        elif re.fullmatch(r"\s*:precondition\s*", line, re.IGNORECASE):
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}:precondition ()"

        if original_line.strip() and not line.strip():
            continue
        cleaned_lines.append(line.rstrip())

    cleaned_text = "\n".join(cleaned_lines)
    if cleaned_text and keep_trailing_newline:
        cleaned_text += "\n"
    return cleaned_text


def _extract_subtask_blocks(
    text: str,
    allow_bare: bool = False,
    allow_hash_space_separator: bool = True,
    normalize_headers: bool = False,
) -> List[str]:
    return ParsingUtils.extract_subtask_blocks(
        text,
        allow_bare=allow_bare,
        allow_hash_space_separator=allow_hash_space_separator,
        normalize_headers=normalize_headers,
    )


def _is_subtask_trailing_line(line: str) -> bool:
    return ParsingUtils.is_subtask_trailing_line(line)


def get_available_models():
    """Get list of available models from providers.yaml"""
    return get_litellm_models(load_run_config(_repo_root()).providers_file)


def load_run_storage_config(base_path: str) -> Dict[str, Any]:
    """Load runtime storage configuration for intermediate artifacts."""
    config = load_run_config(base_path)
    return {"storage": {"base_dir": str(config.storage_base_dir)}}


_RAG_RETRIEVAL_LOCK = threading.RLock()


@contextmanager
def _locked_rag_retrieval(retriever: Any):
    """Serialize RAG retrieval and explicit runtime DB builds across workers."""
    with _RAG_RETRIEVAL_LOCK:
        runtime_db_path = getattr(retriever, "runtime_db_path", None)
        if fcntl is None or runtime_db_path is None:
            yield
            return

        runtime_db_path = Path(runtime_db_path)
        lock_path = runtime_db_path.with_name(runtime_db_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _prewarm_rag_runtime_db(config: RunConfig, section: str, label: str) -> bool:
    """Explicitly rebuild a shared RAG runtime DB before workers start."""
    if not _config_bool(config.get(section, "enabled", False)):
        return False
    if not _config_bool(config.get(section, "prewarm_runtime_db", False), False):
        return False

    try:
        retriever = PDDLRagRetriever.from_config(config, section=section)
    except PDDLRagError as exc:
        raise PDDLError(f"Error loading {label} RAG configuration: {exc}") from exc
    if retriever is None:
        return False

    try:
        with _locked_rag_retrieval(retriever):
            retriever.build_runtime_db()
    except PDDLRagError as exc:
        raise PDDLError(f"Error building {label} RAG runtime DB: {exc}") from exc
    return True


def prewarm_decompose_rag_runtime_db(config: RunConfig) -> bool:
    """Explicitly rebuild the shared decomposition RAG runtime DB before workers start."""
    return _prewarm_rag_runtime_db(config, "decompose_rag", "task decomposition")


def prewarm_allocate_rag_runtime_db(config: RunConfig) -> bool:
    """Explicitly rebuild the shared allocation RAG runtime DB before workers start."""
    return _prewarm_rag_runtime_db(config, "allocate_rag", "task allocation")


def prewarm_problem_rag_runtime_db(config: RunConfig) -> bool:
    """Explicitly rebuild the shared problem-generation RAG runtime DB before workers start."""
    return _prewarm_rag_runtime_db(config, "problem_rag", "problem generation")


LLM_TOKEN_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def empty_llm_token_usage() -> Dict[str, int]:
    """Return the fixed token usage shape used in run summaries."""
    return {key: 0 for key in LLM_TOKEN_USAGE_KEYS}


def _safe_token_count(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def summarize_llm_token_usage(llm_calls_path: Union[str, Path]) -> Dict[str, int]:
    """Sum token usage entries from an LLM calls JSONL log."""
    totals = empty_llm_token_usage()
    path = Path(llm_calls_path)
    if not path.exists():
        return totals

    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                usage = entry.get("usage")
                if not isinstance(usage, dict):
                    continue
                for key in LLM_TOKEN_USAGE_KEYS:
                    totals[key] += _safe_token_count(usage.get(key))
    except OSError:
        return empty_llm_token_usage()

    return totals

# Action mapping from actions module

class ValidationError(PDDLError):
    """Exception raised for PDDL validation errors."""
    pass

class PlanningError(PDDLError):
    """Exception raised for PDDL planning errors."""
    pass

class TaskProcessingResult(Dict[str, Any]):
    """Typed dictionary-like container for per-task execution results."""
    pass


@dataclass
class ProblemGenerationResult:
    """In-memory result for one problem-generation subtask."""

    subtask_index: int
    normalized_robot_name: str
    real_robot_name: str
    prompt: str
    raw_output: str
    problem: str
    repair: Optional[ProblemRepairResult] = None


class PDDLUtils:
    """Utility functions for PDDL operations."""
    
    @staticmethod
    def convert_to_dict_objprop(objs: List[str], obj_mass: List[float]) -> List[Dict[str, Union[str, float]]]:
        """Convert object list to dictionary format with mass.
        
        Args:
            objs (List[str]): List of object names
            obj_mass (List[float]): List of object masses
            
        Returns:
            List[Dict[str, Union[str, float]]]: List of dictionaries containing object properties
        """
        return [{'name': obj, 'mass': mass} for obj, mass in zip(objs, obj_mass)]

    @staticmethod
    def extract_floor_plan_number(floor_plan: Union[int, str]) -> str:
        """Extract the numeric scene identifier from a floor plan value."""
        floor_plan_str = str(floor_plan)
        if floor_plan_str.startswith("FloorPlan"):
            floor_plan_str = floor_plan_str[len("FloorPlan"):]

        match = re.match(r"(\d+)", floor_plan_str)
        if not match:
            raise ValidationError(f"Invalid floor plan value: {floor_plan}")

        return match.group(1)
    
    @staticmethod
    def get_ai2_thor_objects(floor_plan: int, config: Optional[RunConfig] = None) -> List[Dict[str, Any]]:
        """Get objects from AI2Thor environment.
        
        Args:
            floor_plan (int): Floor plan number
            
        Returns:
            List[Dict[str, Any]]: List of objects with their properties
        """
        run_config = config or load_run_config(_repo_root())
        return get_ai2_thor_objects_cached(
            floor_plan,
            PDDLUtils.convert_to_dict_objprop,
            cache_dir=run_config.ai2thor_objects_cache_dir,
        )

class PDDLPlanner:
    
    def __init__(self, base_path: str, file_processor: FileProcessor, config: Optional[RunConfig] = None):
        """Initialize the PDDL planner.

        """
        self.base_path = base_path
        self.file_processor = file_processor
        self.config = config or load_run_config(base_path)
        self.planner_path = str(self.config.planner_executable)
        self.planner_alias = str(self.config.get("planner", "alias", "seq-opt-lmcut"))
        self.timeout_seconds = int(self.config.get("planner", "timeout_seconds", 300))
    
    def run_plan(self, domain_file: str, problem_file: str) -> None:
        """
        
        Args:
            domain_file (str)
            problem_file (str)
    
        """
        try:
            command = [
                self.planner_path,
                "--alias",
                self.planner_alias,
                domain_file,
                problem_file
            ]
            
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds
            )
            
            # Save the plan output
            output_file = problem_file.replace('.pddl', '_plan.txt')
            self.file_processor.write_file(output_file, result.stdout)
            
            if result.stderr:
                print(f"Warnings/Errors for {problem_file}:", result.stderr)
                
        except Exception as e:
            raise PlanningError(f"Error running PDDL planner: {str(e)}")

class TaskManager:
    """Manages task processing and coordination.
    
    This is the main orchestrator class that coordinates all operations
 result logging.
    """
    
    def __init__(
        self,
        base_path: str,
        model: str,
        prompt_decompse_set: str = "pddl_train_task_decomposesep",
        prompt_allocation_set: str = "pddl_train_task_allocationsep",
        config: Optional[RunConfig] = None,
        test_set: Optional[str] = None,
        floor_plan: Optional[Union[int, str]] = None,
        allocate_model: Optional[str] = None,
    ):
        """Initialize the task manager.
        
        Args:
            base_path (str): Base path for all operations
            model (str): Model to use
            prompt_decompse_set (str): Name of the decomposition prompt set
            prompt_allocation_set (str): Name of the allocation prompt set
            allocate_model (Optional[str]): Model to use for task allocation;
                defaults to ``model``
        """
        self.base_path = base_path
        self.model = model
        self.allocate_model = allocate_model or model
        self.prompt_decompse_set = prompt_decompse_set
        self.prompt_allocation_set = prompt_allocation_set
        self.config = config or load_run_config(base_path)
        self.test_set = test_set
        self.floor_plan = normalize_floor_plan(str(floor_plan)) if floor_plan is not None else None
        self.problem_repair_enabled = _config_bool(
            self.config.get("problem_repair", "enabled", False),
            False,
        )
        self.runtime_config = {"storage": {"base_dir": str(self.config.storage_base_dir)}}
        self.instance_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        
        # Initialize components
        self.llm = LLMHandler(self.config)
        self.file_processor = FileProcessor(base_path, config=self.config)
        self.planner = PDDLPlanner(base_path, self.file_processor, self.config)
        try:
            self.decompose_rag_retriever = PDDLRagRetriever.from_config(
                self.config,
                section="decompose_rag",
            )
        except PDDLRagError as exc:
            raise PDDLError(f"Error loading task decomposition RAG configuration: {exc}") from exc
        try:
            self.allocate_rag_retriever = PDDLRagRetriever.from_config(
                self.config,
                section="allocate_rag",
            )
        except PDDLRagError as exc:
            raise PDDLError(f"Error loading task allocation RAG configuration: {exc}") from exc
        try:
            self.problem_rag_retriever = PDDLRagRetriever.from_config(
                self.config,
                section="problem_rag",
            )
        except PDDLRagError as exc:
            raise PDDLError(f"Error loading problem generation RAG configuration: {exc}") from exc
        
        # Initialize paths
        self.resources_path = str(self.config.resources_dir)
        self.logs_path = str(self.config.task_manager_runs_dir / self.instance_id)
        self.intermediate_base_path = str(self.config.storage_base_dir)
        os.makedirs(self.intermediate_base_path, exist_ok=True)
        
        # Initialize result storage
        self.decomposed_plan: List[str] = []
        self.allocated_plan: List[str] = []
        self.code_plan: List[str] = []
        self.combined_plan: List[str] = []
        self.code_planpddl: List[str] = []
        self.sequence_operations: str = ""  # Initialize sequence_operations
        self.tc: List[int] = []
        self.total_subtasks: List[int] = []
        self.task_results: List[TaskProcessingResult] = []
        
        # Get action mapping from actions module
        
        # Initialize objects_ai as None, will be set in process_tasks
        self.objects_ai = None
        self.current_task_run_dir: Optional[str] = None
        self.current_task_manifest: Dict[str, Any] = {}
        self.current_generated_subtask_dir: Optional[str] = None
        self.current_each_run_dir: Optional[str] = None
        self.current_robot_domain_names: Dict[str, str] = {}
        self.dataset_robot_domain_name_maps: List[Dict[str, str]] = []

    def _sanitize_filename(self, value: str) -> str:
        """Convert a value into a filesystem-safe filename fragment."""
        sanitized = re.sub(r'[<>:"/\\|?*\s]+', '_', value.strip())
        sanitized = sanitized.strip('._')
        return sanitized or "task"

    def _next_run_sequence(self, task_run_parent: str, date_prefix: str) -> int:
        """Return the next run sequence for a task/date directory."""
        max_sequence = 0
        if os.path.isdir(task_run_parent):
            for entry in os.listdir(task_run_parent):
                match = re.fullmatch(rf"{re.escape(date_prefix)}_(\d{{3}})", entry)
                if match and os.path.isdir(os.path.join(task_run_parent, entry)):
                    max_sequence = max(max_sequence, int(match.group(1)))
        return max_sequence + 1

    def _create_dataset_task_run_dir(
        self,
        task: str,
        date_prefix: str,
    ) -> Tuple[str, int]:
        """Create a dataset-scoped task run directory with a three-digit sequence."""
        test_set_dir = self._sanitize_filename(self.test_set or "dataset")
        floor_plan_dir = self._sanitize_filename(self.floor_plan or "floorplan")
        task_dir = self._sanitize_filename(task)[:80]
        task_run_parent = os.path.join(
            self.intermediate_base_path,
            f"{test_set_dir}___{floor_plan_dir}",
            task_dir,
        )
        os.makedirs(task_run_parent, exist_ok=True)

        run_sequence = self._next_run_sequence(task_run_parent, date_prefix)
        while True:
            task_run_dir = os.path.join(task_run_parent, f"{date_prefix}_{run_sequence:03d}")
            try:
                os.makedirs(task_run_dir)
                return task_run_dir, run_sequence
            except FileExistsError:
                run_sequence += 1

    def _write_text_artifact(self, relative_path: str, content: Any) -> Optional[str]:
        """Write a text artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        artifact_path = os.path.join(self.current_task_run_dir, relative_path)
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        self.file_processor.write_file(artifact_path, str(content))
        return artifact_path

    def _get_raw_problem_file_path(self) -> Optional[str]:
        """Write a text artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        return os.path.join(self.current_task_run_dir, "05_problem_generation/outputs")

    def _ensure_raw_problem_output_dir(self) -> Optional[str]:
        """Ensure the raw problem output directory exists for the current task run."""
        raw_problem_file_path = self._get_raw_problem_file_path()
        if not raw_problem_file_path:
            return None

        os.makedirs(raw_problem_file_path, exist_ok=True)
        return raw_problem_file_path
    
    def _get_plan_file_path(self) -> Optional[str]:
        """Write a text artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        return os.path.join(self.current_task_run_dir, "08_planner/outputs")

    def _get_validated_problem_file_path(self) -> Optional[str]:
        """Return the task-local directory for validated allaction problems."""
        if not self.current_task_run_dir:
            return None

        return os.path.join(self.current_task_run_dir, "07_validate/outputs")

    def _write_json_artifact(self, relative_path: str, content: Any) -> Optional[str]:
        """Write a JSON artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        artifact_path = os.path.join(self.current_task_run_dir, relative_path)
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        self.file_processor.write_json(artifact_path, content)
        return artifact_path

    def _read_json_artifact(self, relative_path: str, default: Any) -> Any:
        """Read a JSON artifact from the current task run, returning a fallback on failure."""
        if not self.current_task_run_dir:
            return copy.deepcopy(default)

        artifact_path = os.path.join(self.current_task_run_dir, relative_path)
        try:
            with open(artifact_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError, TypeError):
            return copy.deepcopy(default)

    def _merge_subtask_records(
        self,
        existing: Sequence[Dict[str, Any]],
        updates: Sequence[Dict[str, Any]],
        filename_key: str = "problem_file",
    ) -> List[Dict[str, Any]]:
        """Merge records by their subtask id while preserving deterministic order."""
        merged: Dict[int, Dict[str, Any]] = {}
        unkeyed: List[Dict[str, Any]] = []
        for record in list(existing) + list(updates):
            subtask_id = self._subtask_id_from_filename(str(record.get(filename_key, "")))
            if subtask_id is None:
                unkeyed.append(dict(record))
            else:
                merged[subtask_id] = dict(record)
        return [merged[key] for key in sorted(merged)] + unkeyed

    def _record_artifact(self, section: str, key: str, relative_path: str) -> None:
        """Record an artifact in the current task manifest."""
        if not self.current_task_manifest:
            return

        if "artifacts" not in self.current_task_manifest:
            self.current_task_manifest["artifacts"] = {}
        if section not in self.current_task_manifest["artifacts"]:
            self.current_task_manifest["artifacts"][section] = {}
        self.current_task_manifest["artifacts"][section][key] = relative_path

    def _write_problem_repair_manifest(
        self,
        results: Sequence[ProblemGenerationResult],
        replace_all: bool,
    ) -> None:
        """Merge per-subtask repair records and register the resulting artifact."""
        if not self.problem_repair_enabled:
            return

        relative_path = self.config.artifact(
            "problem_repair_manifest",
            "05_problem_generation/problem_repair_manifest.json",
        )
        existing = [] if replace_all else self._read_json_artifact(relative_path, [])
        if not isinstance(existing, list):
            existing = []
        by_index = {
            int(record["index"]): dict(record)
            for record in existing
            if isinstance(record, dict) and str(record.get("index", "")).isdigit()
        }
        for result in results:
            if result.repair is None:
                continue
            by_index[result.subtask_index] = result.repair.to_manifest_record(result.subtask_index)

        self._write_json_artifact(
            relative_path,
            [by_index[index] for index in sorted(by_index)],
        )
        self._record_artifact("problem_generation", "problem_repair_manifest", relative_path)
        self._persist_manifest()

    def _json_for_prompt(self, value: Any) -> str:
        """Serialize prompt context without failing on unusual runtime objects."""
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _record_rag_retrieval(
        self,
        manifest_section: str,
        key: str,
        stage: str,
        examples: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record retrieved RAG examples in the current manifest."""
        if not isinstance(self.current_task_manifest, dict):
            return

        rag_section = self.current_task_manifest.setdefault(manifest_section, {})
        retrievals = rag_section.setdefault("retrievals", {})
        record = {
            "stage": stage,
            "examples": [example.to_manifest_record() for example in examples],
        }
        if metadata:
            record.update(metadata)
        retrievals[key] = record

    def _record_decompose_rag_retrieval(
        self,
        key: str,
        stage: str,
        examples: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record retrieved task-decomposition RAG examples in the current manifest."""
        self._record_rag_retrieval("decompose_rag", key, stage, examples, metadata)

    def _record_allocate_rag_retrieval(
        self,
        key: str,
        stage: str,
        examples: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record retrieved task-allocation RAG examples in the current manifest."""
        self._record_rag_retrieval("allocate_rag", key, stage, examples, metadata)

    def _rag_prompt_block(
        self,
        retriever: Any,
        manifest_section: str,
        stage: str,
        query_text: str,
        manifest_key: str,
        error_label: str,
    ) -> str:
        """Return formatted RAG examples, or empty string when disabled/unavailable."""
        if not retriever:
            return ""

        started_at = time.monotonic()
        query_tokens = []
        if hasattr(retriever, "query_tokens"):
            try:
                try:
                    query_tokens = list(retriever.query_tokens(query_text, stage=stage))
                except TypeError:
                    query_tokens = list(retriever.query_tokens(query_text))
            except Exception:
                query_tokens = []

        try:
            with _locked_rag_retrieval(retriever):
                examples = retriever.retrieve(stage, query_text)
        except PDDLRagTimeoutError as exc:
            self._record_rag_retrieval(
                manifest_section,
                manifest_key,
                stage,
                [],
                {
                    "query_tokens": query_tokens,
                    "elapsed_seconds": round(time.monotonic() - started_at, 3),
                    "timeout": True,
                    "error": str(exc),
                },
            )
            return ""
        except PDDLRagError as exc:
            raise PDDLError(f"Error retrieving {error_label} RAG examples: {exc}") from exc

        metadata = {
            "query_tokens": query_tokens,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "timeout": False,
        }
        if not examples:
            self._record_rag_retrieval(manifest_section, manifest_key, stage, [], metadata)
            return ""

        self._record_rag_retrieval(manifest_section, manifest_key, stage, examples, metadata)
        return "\n" + retriever.format_prompt_block(stage, examples) + "\n"

    def _decompose_rag_prompt_block(self, query_text: str, manifest_key: str = "decompose") -> str:
        """Return formatted decomposition RAG examples, or empty string when disabled."""
        return self._rag_prompt_block(
            self.decompose_rag_retriever,
            "decompose_rag",
            "decompose",
            query_text,
            manifest_key,
            "task decomposition",
        )

    def _allocate_rag_prompt_block(self, query_text: str, manifest_key: str = "allocate") -> str:
        """Return formatted allocation RAG examples, or empty string when disabled."""
        return self._rag_prompt_block(
            self.allocate_rag_retriever,
            "allocate_rag",
            "allocate",
            query_text,
            manifest_key,
            "task allocation",
        )

    def _problem_rag_prompt_block(self, query_text: str, manifest_key: str = "problem_generation") -> str:
        """Return formatted problem-generation RAG examples, or empty string when disabled."""
        return self._rag_prompt_block(
            self.problem_rag_retriever,
            "problem_rag",
            "problem_generation",
            query_text,
            manifest_key,
            "problem generation",
        )

    def _rag_retrieval_timed_out(self, manifest_section: str, manifest_key: str) -> bool:
        if not isinstance(self.current_task_manifest, dict):
            return False
        retrievals = self.current_task_manifest.get(manifest_section, {}).get("retrievals", {})
        record = retrievals.get(manifest_key, {})
        return bool(record.get("timeout")) if isinstance(record, dict) else False

    def _read_decompose_static_prompt(self) -> str:
        prompt_file = self.config.prompt_file(f"{self.prompt_decompse_set}.txt")
        return self.file_processor.read_file(str(prompt_file))

    def _read_allocation_static_prompt(self) -> str:
        prompt_file = self.config.prompt_file(f"{self.prompt_allocation_set}_solution.txt")
        with open(prompt_file, "r", encoding="utf-8") as allocated_prompt_file:
            return allocated_prompt_file.read()

    def _rag_or_static_prompt_block(
        self,
        retriever: Any,
        rag_prompt_builder: Callable[[str], str],
        static_prompt_reader: Callable[[], str],
        query_text: str,
    ) -> str:
        if retriever:
            rag_block = rag_prompt_builder(query_text)
            if rag_block:
                return rag_block
        return static_prompt_reader()

    def _allocation_rag_query(
        self,
        decomposed_plan: str,
        robots: List[dict],
        key_objects: List[Dict[str, Any]],
        subtasks: Sequence[str],
    ) -> str:
        task = ""
        if isinstance(self.current_task_manifest, dict):
            task = str(self.current_task_manifest.get("task") or "")
        subtask_items = [
            (index, str(subtask).strip())
            for index, subtask in enumerate(subtasks, start=1)
            if str(subtask).strip()
        ]
        if not subtask_items and str(decomposed_plan).strip():
            subtask_items = [(1, str(decomposed_plan).strip())]

        subtask_summary_lines = []
        required_skill_lines = []
        for index, subtask in subtask_items:
            first_line = next((line.strip("# ").strip() for line in subtask.splitlines() if line.strip()), "")
            subtask_summary_lines.append(f"Subtask {index}: {first_line or subtask[:160]}")
            skills = self._extract_required_skill_names(subtask)
            if skills:
                required_skill_lines.append(f"Subtask {index}: {', '.join(skills)}")

        if not required_skill_lines:
            skills = self._extract_required_skill_names(decomposed_plan)
            if skills:
                required_skill_lines.append(f"All subtasks: {', '.join(skills)}")

        robot_skill_lines = []
        for robot in robots:
            if not isinstance(robot, dict):
                continue
            name = str(robot.get("name") or "")
            skills = [str(skill) for skill in robot.get("skills", []) if str(skill).strip()]
            mass_capacity = robot.get("mass_capacity")
            robot_skill_lines.append(
                f"{name}: skills {', '.join(skills)}; mass_capacity {mass_capacity}"
            )

        key_object_lines = []
        for obj in key_objects:
            if not isinstance(obj, dict):
                continue
            name = str(obj.get("name") or obj.get("objectId") or obj.get("objectType") or "")
            mass = obj.get("mass")
            key_object_lines.append(f"{name}: mass {mass}" if mass is not None else name)

        subtask_text = "\n".join(f"Subtask {index}: {text}" for index, text in subtask_items)
        return "\n".join(
            [
                f"Task: {task}",
                "# Subtask summaries",
                "\n".join(subtask_summary_lines),
                "# Required skills",
                "\n".join(required_skill_lines),
                "# Robot skill coverage",
                "\n".join(robot_skill_lines),
                "# Key object summary",
                "\n".join(key_object_lines),
                "# Subtasks",
                subtask_text,
                f"Full robots JSON: {self._json_for_prompt(robots)}",
                f"Full key objects JSON: {self._json_for_prompt(key_objects)}",
            ]
        )

    def _problem_rag_query(
        self,
        subtask_index: int,
        subtask: str,
        real_robot_name: str,
        domain_content: str,
        objects_ai: str,
        key_object_pddl_states: Optional[List[Dict[str, Any]]],
    ) -> str:
        task = ""
        if isinstance(self.current_task_manifest, dict):
            task = str(self.current_task_manifest.get("task") or "")

        domain_symbols = self._domain_symbol_summary(domain_content)
        key_state_text = self._json_for_prompt(key_object_pddl_states or [])
        if len(key_state_text) > 3000:
            key_state_text = key_state_text[:3000].rstrip() + "\n...[truncated]"

        object_names = []
        for obj in self._parse_objects_ai(objects_ai):
            name = obj.get("name")
            if isinstance(name, str) and name.strip():
                object_names.append(name.strip())
            if len(object_names) >= 40:
                break

        return "\n".join(
            [
                f"Task: {task}",
                f"Subtask {subtask_index}: {str(subtask).strip()}",
                f"Robot/domain: {real_robot_name}",
                domain_symbols,
                f"Key object PDDL states: {key_state_text}",
                f"Available object names: {', '.join(object_names)}",
            ]
        )

    def _domain_symbol_summary(self, domain_content: str, max_items: int = 80) -> str:
        actions = []
        for match in re.finditer(r"\(:action\s+([A-Za-z0-9_-]+)", domain_content, flags=re.IGNORECASE):
            action = match.group(1)
            if action not in actions:
                actions.append(action)
            if len(actions) >= max_items:
                break

        predicates: List[str] = []
        predicate_match = re.search(
            r"\(:predicates(?P<body>.*?)(?=\n\s*\(:action|\Z)",
            domain_content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if predicate_match:
            for match in re.finditer(r"\(\s*([A-Za-z][A-Za-z0-9_-]*)", predicate_match.group("body")):
                predicate = match.group(1)
                if predicate not in predicates:
                    predicates.append(predicate)
                if len(predicates) >= max_items:
                    break

        return "\n".join(
            [
                f"Domain actions: {', '.join(actions)}",
                f"Domain predicates: {', '.join(predicates)}",
            ]
        )

    def _extract_required_skill_names(self, text: str) -> List[str]:
        skills: List[str] = []
        seen: Set[str] = set()

        def add_skill(value: str) -> None:
            skill = value.strip().strip(".:;()[]{}")
            if not skill:
                return
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", skill):
                return
            key = skill.lower()
            if key in seen:
                return
            seen.add(key)
            skills.append(skill)

        for match in re.finditer(r"Skills?\s+Required\s*:\s*([^\n#.)]+)", text, flags=re.IGNORECASE):
            for item in re.split(r",|;|/|\band\b", match.group(1), flags=re.IGNORECASE):
                add_skill(item)

        for match in re.finditer(r"^\s*([A-Z][A-Za-z0-9_]+)\s*:", text, flags=re.MULTILINE):
            add_skill(match.group(1))

        return skills

    def _replace_domain_robot_name(self, domain_content: str, real_robot_name: str, normalized_robot_name: str) -> str:
        """Replace a real robot domain token with the task-local robot token."""
        real_robot_name = real_robot_name.replace(" ", "")
        normalized_robot_name = normalized_robot_name.replace(" ", "")
        if not real_robot_name or real_robot_name == normalized_robot_name:
            return domain_content

        token_pattern = rf"(?<![A-Za-z0-9_]){re.escape(real_robot_name)}(?![A-Za-z0-9_])"
        return re.sub(token_pattern, normalized_robot_name, domain_content)

    def _force_problem_domain(self, problem_content: str, domain_name: str) -> str:
        """Ensure a generated problem points to the desired PDDL domain."""
        domain_name = domain_name.replace(" ", "")
        if not domain_name:
            return problem_content

        domain_pattern = re.compile(r'\(\s*:domain\s+[^)\s]+\s*\)', re.IGNORECASE)
        replacement = f"(:domain {domain_name})"
        if domain_pattern.search(problem_content):
            return domain_pattern.sub(replacement, problem_content, count=1)

        define_match = re.search(r'\(define\s+\(problem\s+[^)]+\)', problem_content, re.IGNORECASE)
        if not define_match:
            return problem_content

        insert_at = define_match.end()
        return problem_content[:insert_at] + f"\n  {replacement}" + problem_content[insert_at:]

    def _force_problem_robot_name(
        self,
        problem_content: str,
        local_robot_name: str,
        real_robot_name: str,
    ) -> str:
        """Replace the task-local robot object token with the real robot token."""
        return self._replace_domain_robot_name(
            problem_content,
            local_robot_name,
            real_robot_name,
        )

    def _persist_manifest(self) -> None:
        """Persist the current task manifest to disk."""
        if self.current_task_run_dir and self.current_task_manifest:
            self._write_json_artifact(self.config.artifact("manifest", "run_manifest.json"), self.current_task_manifest)

    def _prepare_task_run_dir(
        self,
        task_idx: int,
        task: str,
        robots: List[dict],
        objects_ai: str,
        domain_content: str,
        manifest_task_index: Optional[int] = None,
    ) -> None:
        """Create and initialize the storage directory for the current task."""
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        date_prefix = now.strftime("%Y%m%d")
        manifest_task_index = task_idx if manifest_task_index is None else int(manifest_task_index)
        run_sequence = None
        if self.test_set and self.floor_plan:
            self.current_task_run_dir, run_sequence = self._create_dataset_task_run_dir(task, date_prefix)
        else:
            folder_name = f"{task_idx + 1:03d}_{self._sanitize_filename(task)[:80]}_{self.instance_id}_{timestamp}"
            self.current_task_run_dir = os.path.join(self.intermediate_base_path, folder_name)
            os.makedirs(self.current_task_run_dir, exist_ok=True)
        generated_artifact_dir = self.config.artifact("generated_subtask_dir", "06_split/generated_subtask")
        each_run_artifact_dir = self.config.artifact("each_run_dir", "artifacts/each_run")
        self.current_generated_subtask_dir = os.path.join(self.current_task_run_dir, generated_artifact_dir)
        self.current_each_run_dir = os.path.join(self.current_task_run_dir, each_run_artifact_dir)
        self.file_processor.configure_workspace(
            subtask_path=self.current_generated_subtask_dir,
            each_run_path=self.current_each_run_dir,
        )
        self.current_task_manifest = {
            "task_index": manifest_task_index,
            "task": task,
            "model": self.model,
            "allocate_model": self.allocate_model,
            "created_at": timestamp,
            "storage_base_dir": self.intermediate_base_path,
            "task_run_dir": self.current_task_run_dir,
            "test_set": self.test_set,
            "floor_plan": self.floor_plan,
            "run_date": date_prefix,
            "run_sequence": run_sequence,
            "artifacts": {},
            "llm": {
                "task_log": self.config.artifact("llm_calls", "00_llm/llm_calls.jsonl")
            }
        }
        get_llm_logger().set_context(
            instance_id=self.instance_id,
            task_index=manifest_task_index,
            task=task,
            task_run_dir=self.current_task_run_dir,
            task_log_file=os.path.join(self.current_task_run_dir, self.config.artifact("llm_calls", "00_llm/llm_calls.jsonl")),
        )

        inputs = {
            "task": task,
            "robots": robots,
            "objects_ai": objects_ai,
            "model": self.model,
            "allocate_model": self.allocate_model,
            "prompt_decompose_set": self.prompt_decompse_set,
            "prompt_allocation_set": self.prompt_allocation_set
        }
        task_context_path = self.config.artifact("task_context", "inputs/task_context.json")
        domain_content_path = self.config.artifact("domain_content", "inputs/domain_content.pddl")
        llm_calls_path = self.config.artifact("llm_calls", "00_llm/llm_calls.jsonl")
        self._write_json_artifact(task_context_path, inputs)
        self._record_artifact("inputs", "task_context", task_context_path)
        self._write_text_artifact(domain_content_path, domain_content)
        self._record_artifact("inputs", "domain_content", domain_content_path)
        self._record_artifact("llm", "calls", llm_calls_path)
        self._persist_manifest()

    def clean_generated_subtask_directory(self) -> None:
        """Clean the generated subtask directory."""
        directory = self.file_processor.subtask_path
        try:
            if os.path.exists(directory):
                for filename in os.listdir(directory):
                    file_path = os.path.join(directory, filename)
                    try:
                        if os.path.isfile(file_path):
                            os.unlink(file_path)
                        elif os.path.isdir(file_path):
                            shutil.rmtree(file_path)
                    except Exception as e:
                        print(f"Error cleaning {file_path}: {str(e)}")
        except Exception as e:
            print(f"Error accessing directory {directory}: {str(e)}")

    @staticmethod
    def _subtask_id_from_filename(filename: str) -> Optional[int]:
        match = re.search(r"subtask[_-]?0*(\d+)", str(filename), re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _planner_error_keywords() -> Tuple[str, ...]:
        return (
            "could not parse task file",
            "error in initial state specification",
            "tokens remaining after parsing",
            "duplicate object",
            "trivially false goal",
            "no relaxed solution",
            "completely explored state space -- no solution",
            "no solution",
            "unsolvable",
            "driver aborting",
            "translate exit code: 31",
            "traceback",
            "filenotfounderror",
            "failed to match magic word",
            "keyerror",
            "error:",
            "planner error",
            "syntax error",
            "parse error",
            "invalid",
        )

    def _planner_output_has_error(
        self,
        stdout_text: str,
        stderr_text: str,
        return_code: Optional[int],
        status: str = "completed",
    ) -> bool:
        if status not in {"completed", "ok"}:
            return True
        if return_code not in (None, 0):
            return True

        combined = "\n".join(part for part in (stdout_text or "", stderr_text or "") if part).lower()
        return any(keyword in combined for keyword in self._planner_error_keywords())

    def _summarize_planner_output(
        self,
        stdout_text: str,
        stderr_text: str,
        max_chars: int = 1200,
    ) -> str:
        combined_lines = []
        for label, text in (("stdout", stdout_text or ""), ("stderr", stderr_text or "")):
            for line in text.splitlines():
                stripped = line.strip()
                if stripped:
                    combined_lines.append(f"{label}: {stripped}")

        if not combined_lines:
            return ""

        keywords = self._planner_error_keywords()
        selected = [
            line
            for line in combined_lines
            if any(keyword in line.lower() for keyword in keywords)
        ]
        if not selected:
            selected = combined_lines[-12:]

        summary = "\n".join(selected)
        if len(summary) > max_chars:
            summary = summary[:max_chars].rstrip() + "\n...[truncated]"
        return summary

    def _planner_record_success(self, record: Dict[str, Any]) -> bool:
        return (
            record.get("status") == "completed"
            and bool(record.get("plan_generated"))
            and not bool(record.get("has_planner_error"))
            and record.get("return_code") in (None, 0)
        )

    def _build_planner_status_fields(
        self,
        output_file: Optional[str],
        stdout_text: str = "",
        stderr_text: str = "",
        return_code: Optional[int] = None,
        status: str = "completed",
    ) -> Dict[str, Any]:
        plan_exists = bool(output_file and os.path.isfile(output_file))
        plan_size_bytes = os.path.getsize(output_file) if plan_exists and output_file else 0
        plan_generated = plan_exists and plan_size_bytes > 0
        has_planner_error = self._planner_output_has_error(stdout_text, stderr_text, return_code, status)

        if not plan_generated:
            failure_reason = "No planner plan was generated."
        elif has_planner_error:
            failure_reason = "Planner produced a plan but reported an error."
        else:
            failure_reason = ""

        return {
            "status": status,
            "plan_exists": plan_exists,
            "plan_size_bytes": plan_size_bytes,
            "plan_generated": plan_generated,
            "has_planner_error": has_planner_error,
            "failure_reason": failure_reason,
            "planner_output_excerpt": self._summarize_planner_output(stdout_text, stderr_text),
        }

    def calculate_completion_rate(self) -> Tuple[int, int]:
        """
        
        Returns:
            Tuple[int, int]: (number of completed tasks, total number of tasks)
        """
        total_subtasks = 0
        problem_file_path = self._get_raw_problem_file_path()
        if problem_file_path and os.path.exists(problem_file_path):
            total_subtasks = len([
                f
                for f in os.listdir(problem_file_path)
                if f.endswith("_problem.pddl")
            ])

        TC = 0
        plan_file_path = self._get_plan_file_path()
        if plan_file_path and os.path.exists(plan_file_path):
            TC = len([f for f in os.listdir(plan_file_path) if f.endswith('_plan.txt')])

        return TC, total_subtasks

    def load_dataset(self, test_file: str) -> Tuple[List[str], List[List[dict]], List[str], List[int], List[int]]:
        """Load dataset from a JSONL file.
        
        Args:
            test_file (str): Path to the test file
            
        """
        test_tasks = []
        robots_test_tasks = []
        gt_test_tasks = []
        trans_cnt_tasks = []
        min_trans_cnt_tasks = []
        
        try:
            with open(test_file, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue

                    record = json.loads(line)
                    test_tasks.append(record["task"])
                    robots_test_tasks.append(record["robot list"])
                    gt_test_tasks.append(record["object_states"])
                    trans_cnt_tasks.append(record["trans"])
                    min_trans_cnt_tasks.append(record.get("min_trans", record.get("max_trans")))
            
            # Prepare robot configurations
            available_robots = []
            self.dataset_robot_domain_name_maps = []
            for robots_list in robots_test_tasks:
                task_robots = []
                for i, r_id in enumerate(robots_list):
                    rob = copy.deepcopy(robots.robots[r_id-1])
                    rob['name'] = f'robot{i+1}'  # Use f-string for consistency
                    task_robots.append(rob)
                available_robots.append(task_robots)
                self.dataset_robot_domain_name_maps.append(build_robot_domain_name_map(robots_list))
            
            return test_tasks, available_robots, gt_test_tasks, trans_cnt_tasks, min_trans_cnt_tasks
            
        except FileNotFoundError:
            raise PDDLError(f"Test file not found: {test_file}")
        except json.JSONDecodeError as e:
            raise PDDLError(f"Error parsing JSON in test file: {str(e)}")
        except Exception as e:
            raise PDDLError(f"Error loading dataset: {str(e)}")
    
    def log_results(self, task: str, idx: int, available_robots: List[dict], 
                   gt_test_tasks: List[str], trans_cnt_tasks: List[int], 
                   min_trans_cnt_tasks: List[int], objects_ai: str):
        """Log task processing results."""
        # print(f"\n[DEBUG] Logging task {idx + 1}")
        # print(f"Current list lengths:")
        # print(f"- code_planpddl: {len(self.code_planpddl)}")
        # print(f"- combined_plan: {len(self.combined_plan)}")
        # print(f"- decomposed_plan: {len(self.decomposed_plan)}")
        # print(f"- allocated_plan: {len(self.allocated_plan)}")
        # print(f"- code_plan: {len(self.code_plan)}")
        date_time = datetime.now().strftime("%m-%d-%Y-%H-%M-%S")
        task_name = "_".join(task.split()).replace('\n', '')
        folder_name = f"{task_name}_plans_{date_time}"
        log_folder = os.path.join(self.logs_path, folder_name)
        task_result = self.task_results[idx] if idx < len(self.task_results) else None
        
        #print(f"Creating log folder: {log_folder}")
        os.makedirs(log_folder)
        
        try:
            print(f"Writing plans for task {idx + 1}")
            self._write_plan(log_folder, "code_planpddl.py", task_result["code_planpddl"] if task_result else self.code_planpddl[idx])
            #print(f"Successfully wrote code_planpddl for task {idx + 1}")
            self._write_plan(log_folder, "combined_plan.py", task_result["combined_plan"] if task_result else self.combined_plan[idx])
            #print(f"Successfully wrote combined_plan for task {idx + 1}")
            self._write_plan(log_folder, "decomposed_plan.py", task_result["decomposed_plan"] if task_result else self.decomposed_plan[idx])
            #print(f"Successfully wrote decomposed_plan for task {idx + 1}")
            self._write_plan(log_folder, "allocated_plan.py", task_result["allocated_plan"] if task_result else self.allocated_plan[idx])
            #print(f"Successfully wrote allocated_plan for task {idx + 1}")
            self._write_plan(log_folder, "code_plan.py", task_result["code_plan"] if task_result else self.code_plan[idx])
            #print(f"Successfully wrote code_plan for task {idx + 1}")
            
            # Log main information
            if task_result:
                TC = task_result["successful_subtasks"]
                total_subtasks = task_result["total_subtasks"]
            else:
                TC, total_subtasks = self.tc[idx], self.total_subtasks[idx]
            print(f"Task {idx + 1} - TC: {TC}, Total Subtasks: {total_subtasks}")


            generated_subtask_dir = task_result["generated_subtask_dir"] if task_result else self.file_processor.subtask_path
            artifact_map = task_result.get("manifest", {}).get("artifacts", {}) if task_result else {}
            task_summary = {
                "task": task,
                "task_index": idx,
                "model": self.model,
                "objects_ai": objects_ai,
                "robots": available_robots[idx],
                "ground_truth": gt_test_tasks[idx],
                "trans": trans_cnt_tasks[idx],
                "min_trans": min_trans_cnt_tasks[idx],
                "successful_subtasks": TC,
                "total_subtasks": total_subtasks,
                "completion_rate": (TC / total_subtasks) if total_subtasks else 0.0,
                "task_run_dir": task_result["task_run_dir"] if task_result else None,
                "generated_subtask_dir": generated_subtask_dir,
                "generated_subtasks": sorted(os.listdir(generated_subtask_dir)) if os.path.exists(generated_subtask_dir) else [],
                "artifacts": artifact_map,
            }
            self.file_processor.write_json(os.path.join(log_folder, "task_summary.json"), task_summary)

            with open(os.path.join(log_folder, "log.txt"), 'w', encoding='utf-8') as f:
                f.write(task)
                f.write(f"\n\nModel: {self.model}")
                f.write(f"\n{objects_ai}")
                f.write(f"\nrobots = {available_robots[idx]}")
                f.write(f"\nground_truth = {gt_test_tasks[idx]}")
                f.write(f"\ntrans = {trans_cnt_tasks[idx]}")
                f.write(f"\nmin_trans = {min_trans_cnt_tasks[idx]}")
                f.write(f"\nTotalsuccesssubtask = {TC}")
                f.write(f"\nTotalsubtask = {total_subtasks}")
                f.write(f"\nTaskRunDir = {task_result['task_run_dir'] if task_result else ''}")
                f.write(f"\nGeneratedSubtaskDir = {generated_subtask_dir}")
                f.write(f"\nPlannerManifest = {artifact_map.get('planner', {}).get('manifest')}")
                f.write(f"\nCombinePrompt = {artifact_map.get('combine', {}).get('prompt')}")
                f.write(f"\nCombineOutput = {artifact_map.get('combine', {}).get('output')}")
            
            # Copy generated subtasks
            subtask_folder = os.path.join(log_folder, "generated_subtask")
            os.makedirs(subtask_folder)
            source_folder = generated_subtask_dir
            for file_name in os.listdir(source_folder):
                full_file_name = os.path.join(source_folder, file_name)
                if os.path.isfile(full_file_name):
                    shutil.copy(full_file_name, subtask_folder)
            
        except Exception as e:
            print(f"Error writing plans for task {idx + 1}: {str(e)}")

    def _write_plan(self, folder: str, filename: str, content: Union[str, List]):
        """Write a plan to a file."""
        if isinstance(content, list):
            for i, item in enumerate(content):
                with open(os.path.join(folder, f"{filename}.{i}"), 'w', encoding='utf-8') as f:
                    f.write(str(item))
        else:
            with open(os.path.join(folder, filename), 'w', encoding='utf-8') as f:
                f.write(content)

    def process_tasks(
        self,
        test_tasks: List[str],
        available_robots: List[dict],
        objects_ai: str,
        robot_domain_name_maps: Optional[List[Dict[str, str]]] = None,
        task_indices: Optional[List[int]] = None,
    ) -> None:
        """Run the plan-first, capability-filtered robot allocation pipeline."""
        try:
            print(f"\n[DIAGNOSTIC] Initial Task Count: {len(test_tasks)}")
            self.objects_ai = objects_ai
            self.decomposed_plan = []
            self.allocated_plan = []
            self.code_plan = []
            self.combined_plan = []
            self.code_planpddl = []
            self.tc = []
            self.total_subtasks = []
            self.task_results = []

            allaction_domain_path = str(self.config.allaction_domain_path())
            domain_content = self.file_processor.read_file(allaction_domain_path)
            effective_robot_domain_name_maps = (
                robot_domain_name_maps
                if robot_domain_name_maps is not None
                else self.dataset_robot_domain_name_maps
            )

            for task_idx, (task, task_robots) in enumerate(zip(test_tasks, available_robots)):
                manifest_task_index = (
                    task_idx if task_indices is None else int(task_indices[task_idx])
                )
                print(f"\n{'=' * 50}")
                print(f"Processing Task: {task}: {task_idx + 1}/{len(test_tasks)}")
                print(f"{'=' * 50}")
                self.current_robot_domain_names = (
                    copy.deepcopy(effective_robot_domain_name_maps[task_idx])
                    if task_idx < len(effective_robot_domain_name_maps)
                    else {}
                )
                self._prepare_task_run_dir(
                    task_idx,
                    task,
                    task_robots,
                    objects_ai,
                    domain_content,
                    manifest_task_index=manifest_task_index,
                )
                self.clean_generated_subtask_directory()

                decomposed_plan = self._generate_decomposed_plan(
                    task,
                    domain_content,
                    task_robots,
                    objects_ai,
                )
                self.decomposed_plan.append(decomposed_plan)
                print("✓ Decomposed plan generated")

                subtasks = ParsingUtils.extract_subtasks(decomposed_plan)
                if not subtasks:
                    raise PDDLError("Task decomposition produced no subtasks")
                print(f"✓ Extracted {len(subtasks)} subtasks")

                pairwise_predecessors = self._generate_pairwise_predecessors(
                    task=task,
                    decomposed_plan=decomposed_plan,
                    subtasks=subtasks,
                )
                if pairwise_predecessors is None:
                    print("! Pairwise precedence unavailable; using deterministic fallback")
                else:
                    print("✓ Pairwise precedence extracted")

                key_objects_by_subtask = self._extract_key_objects_by_subtask(
                    subtasks,
                    objects_ai,
                )
                key_objects = self._combine_key_objects_by_subtask(
                    key_objects_by_subtask,
                )
                self._write_json_artifact("02_allocate/00_key_objects.json", key_objects)
                self._record_artifact(
                    "allocate",
                    "key_objects",
                    "02_allocate/00_key_objects.json",
                )
                self._write_json_artifact(
                    "02_allocate/00_key_objects_by_subtask.json",
                    key_objects_by_subtask,
                )
                self._record_artifact(
                    "allocate",
                    "key_objects_by_subtask",
                    "02_allocate/00_key_objects_by_subtask.json",
                )

                key_object_pddl_context = self._build_key_object_pddl_context(
                    key_objects,
                    domain_content,
                )
                key_object_pddl_states = key_object_pddl_context["states"]
                key_object_pddl_context_by_subtask = {
                    subtask_id: self._build_key_object_pddl_context(
                        subtask_key_objects,
                        domain_content,
                    )
                    for subtask_id, subtask_key_objects in key_objects_by_subtask.items()
                }
                key_object_pddl_states_by_subtask = {
                    subtask_id: context["states"]
                    for subtask_id, context in key_object_pddl_context_by_subtask.items()
                }
                key_object_pddl_evidence_by_subtask = {
                    subtask_id: context.get("evidence", [])
                    for subtask_id, context in key_object_pddl_context_by_subtask.items()
                }
                self._write_json_artifact(
                    "05_problem_generation/key_object_pddl_states.json",
                    key_object_pddl_states,
                )
                self._record_artifact(
                    "problem_files",
                    "key_object_pddl_states",
                    "05_problem_generation/key_object_pddl_states.json",
                )
                self._write_json_artifact(
                    "05_problem_generation/key_object_pddl_states_by_subtask.json",
                    key_object_pddl_states_by_subtask,
                )
                self._record_artifact(
                    "problem_files",
                    "key_object_pddl_states_by_subtask",
                    "05_problem_generation/key_object_pddl_states_by_subtask.json",
                )
                self._write_json_artifact(
                    "05_problem_generation/key_object_pddl_state_evidence_by_subtask.json",
                    key_object_pddl_evidence_by_subtask,
                )
                self._record_artifact(
                    "problem_files",
                    "key_object_pddl_state_evidence_by_subtask",
                    "05_problem_generation/key_object_pddl_state_evidence_by_subtask.json",
                )
                self._persist_manifest()
                print(f"✓ Matched {len(key_objects)} key objects")

                problem_pddl = self._generate_allaction_problem_files(
                    subtasks,
                    objects_ai,
                    key_object_pddl_states=key_object_pddl_states,
                    key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
                )
                print("✓ Full-capability Problem PDDL files generated")

                planner_records = self._validate_and_plan()
                print("✓ Full-capability plans generated")

                allocated_subtasks = self._allocate_subtasks_with_cpsat(
                    subtasks=subtasks,
                    decomposed_plan=decomposed_plan,
                    problem_pddl=problem_pddl,
                    planner_records=planner_records,
                    available_robots=task_robots,
                    objects_ai=objects_ai,
                    preferred_predecessors=pairwise_predecessors,
                )
                self.allocated_plan.append(
                    json.dumps(allocated_subtasks, indent=2, ensure_ascii=False)
                )
                print("✓ Integer-programming robot allocation generated")

                completed = len(allocated_subtasks)
                total = len(subtasks)
                self.current_task_manifest["completion"] = {
                    "successful_subtasks": completed,
                    "total_subtasks": total,
                }
                self._persist_manifest()
                print(f"Task {task_idx + 1} completion rate: {completed}/{total}")
                self.tc = completed
                self.total = total

            print(f"\n{'=' * 50}")
            print(f"All {len(test_tasks)} tasks processed")
            print(f"{'=' * 50}")
        except Exception as exc:
            print("\n[ERROR] Task Processing Failed:")
            print(f"Error type: {type(exc).__name__}")
            print(f"Error message: {exc}")
            print(
                "Current task index: "
                + str(task_idx if "task_idx" in locals() else "Not started")
            )
            raise
        finally:
            get_llm_logger().clear_context()

    def _generate_pairwise_predecessors(
        self,
        task: str,
        decomposed_plan: str,
        subtasks: List[str],
    ) -> Optional[Dict[int, List[int]]]:
        """Classify pairwise subtask dependencies for the scheduling model."""
        subtask_count = len(subtasks)
        output_artifact = self.config.artifact(
            "precedence_output",
            "02_precedence/predecessors.json",
        )
        manifest_artifact = self.config.artifact(
            "precedence_manifest",
            "02_precedence/pairwise_manifest.json",
        )

        if subtask_count <= 1:
            predecessors = {1: []} if subtask_count == 1 else {}
            self._write_json_artifact(output_artifact, predecessors)
            self._write_json_artifact(
                manifest_artifact,
                {"status": "not_required", "pairs": [], "predecessors": predecessors},
            )
            self._record_artifact("precedence", "output", output_artifact)
            self._record_artifact("precedence", "manifest", manifest_artifact)
            self._persist_manifest()
            return predecessors

        pair_records: List[Dict[str, Any]] = []
        try:
            prompt_path = self.config.prompt_file(
                "pddl_train_task_precedence_pairwise.txt"
            )
            if not prompt_path.is_file():
                prompt_path = _repo_root() / "prompts/v2/pddl_train_task_precedence_pairwise.txt"
            prompt_template = self.file_processor.read_file(str(prompt_path))
            edges: Set[Tuple[int, int]] = set()
            call_config = self.config.llm_call("precedence")

            for first_index in range(subtask_count):
                for second_index in range(first_index + 1, subtask_count):
                    subtask_a_id = first_index + 1
                    subtask_b_id = second_index + 1
                    prompt = self._render_pairwise_precedence_prompt(
                        prompt_template,
                        task,
                        decomposed_plan,
                        subtask_a_id,
                        subtasks[first_index],
                        subtask_b_id,
                        subtasks[second_index],
                    )
                    prompt_artifact = (
                        f"02_precedence/prompts/subtask_{subtask_a_id:02d}_"
                        f"subtask_{subtask_b_id:02d}.txt"
                    )
                    response_artifact = (
                        f"02_precedence/outputs/subtask_{subtask_a_id:02d}_"
                        f"subtask_{subtask_b_id:02d}.txt"
                    )
                    self._write_text_artifact(prompt_artifact, prompt)
                    _, response_text = self.llm.query_model(
                        [{"role": "user", "content": prompt}],
                        self.model,
                        max_tokens=call_config.get("max_tokens", 700),
                        frequency_penalty=call_config.get("frequency_penalty", 0.0),
                    )
                    self._write_text_artifact(response_artifact, response_text)
                    parsed = self._parse_pairwise_precedence_response(
                        response_text,
                        subtask_a_id,
                        subtask_b_id,
                    )
                    relation = parsed["relation"]
                    if relation == "A_BEFORE_B":
                        edges.add((subtask_a_id, subtask_b_id))
                    elif relation == "B_BEFORE_A":
                        edges.add((subtask_b_id, subtask_a_id))
                    pair_records.append(
                        {
                            **parsed,
                            "prompt_path": prompt_artifact,
                            "output_path": response_artifact,
                        }
                    )

            predecessors = self._predecessors_from_edges(edges, subtask_count)
            if self._has_precedence_cycle(predecessors, subtask_count):
                raise ValidationError("Pairwise precedence result is invalid or cyclic")
            self._write_json_artifact(output_artifact, predecessors)
            self._write_json_artifact(
                manifest_artifact,
                {
                    "status": "success",
                    "pairs": pair_records,
                    "edges": [list(edge) for edge in sorted(edges)],
                    "predecessors": predecessors,
                },
            )
            self._record_artifact("precedence", "output", output_artifact)
            self._record_artifact("precedence", "manifest", manifest_artifact)
            self._persist_manifest()
            return predecessors
        except Exception as exc:
            self._write_json_artifact(
                manifest_artifact,
                {
                    "status": "fallback",
                    "error": str(exc),
                    "pairs": pair_records,
                },
            )
            self._record_artifact("precedence", "manifest", manifest_artifact)
            self._persist_manifest()
            return None

    @staticmethod
    def _render_pairwise_precedence_prompt(
        prompt_template: str,
        task: str,
        decomposed_plan: str,
        subtask_a_id: int,
        subtask_a: str,
        subtask_b_id: int,
        subtask_b: str,
    ) -> str:
        return prompt_template.format(
            task=task,
            decomposed_plan=decomposed_plan,
            subtask_a_id=subtask_a_id,
            subtask_a=subtask_a,
            subtask_b_id=subtask_b_id,
            subtask_b=subtask_b,
        )

    def _parse_pairwise_precedence_response(
        self,
        text: str,
        expected_subtask_a_id: int,
        expected_subtask_b_id: int,
    ) -> Dict[str, Any]:
        parsed = self._extract_json_object(text)
        try:
            subtask_a_id = int(parsed.get("subtask_a_id"))
            subtask_b_id = int(parsed.get("subtask_b_id"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "Pairwise precedence response has non-numeric subtask ids"
            ) from exc
        if (subtask_a_id, subtask_b_id) != (
            expected_subtask_a_id,
            expected_subtask_b_id,
        ):
            raise ValidationError("Pairwise precedence response ids do not match expected pair")
        relation = str(parsed.get("relation") or "").strip().upper()
        if relation not in {"A_BEFORE_B", "B_BEFORE_A", "NO_ORDER"}:
            raise ValidationError(f"Invalid pairwise precedence relation: {relation}")
        return {
            "subtask_a_id": subtask_a_id,
            "subtask_b_id": subtask_b_id,
            "relation": relation,
            "reason": str(parsed.get("reason") or "").strip(),
        }

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        raw = str(text).strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < start:
            raise ValidationError("Pairwise precedence response does not contain JSON")
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValidationError("Pairwise precedence response contains invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("Pairwise precedence response must be a JSON object")
        return parsed

    @staticmethod
    def _predecessors_from_edges(
        edges: Set[Tuple[int, int]],
        subtask_count: int,
    ) -> Dict[int, List[int]]:
        predecessors = {subtask_id: [] for subtask_id in range(1, subtask_count + 1)}
        for predecessor_id, successor_id in sorted(edges):
            if (
                predecessor_id != successor_id
                and predecessor_id in predecessors
                and successor_id in predecessors
            ):
                predecessors[successor_id].append(predecessor_id)
        return predecessors

    def _normalize_predecessor_map(
        self,
        predecessors: Dict[int, List[int]],
        subtask_count: int,
    ) -> Optional[Dict[int, List[int]]]:
        try:
            normalized = {
                subtask_id: sorted(
                    {
                        int(predecessor_id)
                        for predecessor_id in predecessors.get(
                            subtask_id,
                            predecessors.get(str(subtask_id), []),
                        )
                        if 1 <= int(predecessor_id) <= subtask_count
                        and int(predecessor_id) != subtask_id
                    }
                )
                for subtask_id in range(1, subtask_count + 1)
            }
        except (AttributeError, TypeError, ValueError):
            return None
        if self._has_precedence_cycle(normalized, subtask_count):
            return None
        return normalized

    @staticmethod
    def _has_precedence_cycle(
        predecessors: Dict[int, List[int]],
        subtask_count: int,
    ) -> bool:
        states = {subtask_id: 0 for subtask_id in range(1, subtask_count + 1)}

        def visit(subtask_id: int) -> bool:
            if states[subtask_id] == 1:
                return True
            if states[subtask_id] == 2:
                return False
            states[subtask_id] = 1
            for predecessor_id in predecessors.get(subtask_id, []):
                if predecessor_id in states and visit(predecessor_id):
                    return True
            states[subtask_id] = 2
            return False

        return any(visit(subtask_id) for subtask_id in states)

    def _sequence_assignment_re(self) -> re.Pattern:
        return ParsingUtils.sequence_assignment_re()

    def _is_sequence_header(self, line: str) -> bool:
        return ParsingUtils.is_sequence_header(line)

    def _is_sequence_boundary(self, line: str) -> bool:
        return ParsingUtils.is_sequence_boundary(line)

    def _merge_sequence_lines(self, lines: List[str]) -> List[str]:
        return ParsingUtils.merge_sequence_lines(lines)

    def _extract_sequence_sections(self, allocated_plan: str) -> List[List[str]]:
        return ParsingUtils.extract_sequence_sections(allocated_plan)

    def _parse_sequence_section(
        self,
        lines: List[str],
    ) -> Tuple[List[str], Dict[int, int]]:
        return ParsingUtils.parse_sequence_section(lines)

    def _extract_sequence_operations(self, allocated_plan: str) -> List[str]:
        """从 allocated_plan 输出中提取 sequence operations 列表。

        期望格式:
        # Sequence of Operations:
        Subtask 1: Robot 2;
        Subtask 2: Robot2;
        SubTask 3 (Open cabinet): robot4;
        SubTask4 - Robot 1;
        (每行代表一个子任务及其分配的机器人)

        Returns:
            List[str]: 每行一个规范化字符串，表示 "Subtask X: Robot Y;" 格式
        """
        return ParsingUtils.extract_sequence_operations(allocated_plan)

    def _extract_robot_assignments(
        self,
        sequence_operations: List[str],
    ) -> Dict[int, int]:
        """从 sequence_operations 中提取每个子任务分配的机器人编号。

        Args:
            sequence_operations: List[str]，每行格式如 "Subtask 1: Robot 2;" 或 "Subtask 1: Robot 2;Subtask 2: Robot 2;"

        Returns:
            Dict[int, int]: {subtask_index: robot_number}，例如 {1: 2, 2: 2, 3: 2}
        """
        return ParsingUtils.extract_robot_assignments(sequence_operations)

    @staticmethod
    def _object_match_key(value: str) -> str:
        return re.sub(r'[^a-z0-9]+', '', str(value).lower())

    @staticmethod
    def _literal_objects_from_context(objects_ai: Union[str, List[Any]]) -> List[Any]:
        """Extract the literal objects list from the shared objects context."""
        if not objects_ai:
            return []

        if isinstance(objects_ai, list):
            return objects_ai

        text = str(objects_ai).strip()
        objects_marker = re.search(r'\bobjects\s*=', text, re.IGNORECASE)
        if objects_marker:
            text = text[objects_marker.end():].strip()

        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and start <= end:
            text = text[start:end + 1]

        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return []

        return parsed if isinstance(parsed, list) else []

    def _format_decompose_objects_prompt(self, objects_ai: Union[str, List[Any]]) -> str:
        """Format decomposition objects as a name-only array."""
        names: List[str] = []
        for item in self._literal_objects_from_context(objects_ai):
            if isinstance(item, dict):
                name = item.get("name")
            else:
                name = item

            if isinstance(name, str) and name.strip():
                names.append(name.strip())

        return f"objects = {names!r}"

    def _parse_objects_ai(self, objects_ai: Union[str, List[Any]]) -> List[Dict[str, Any]]:
        """Parse the floorplan object list while preserving object properties."""
        parsed = self._literal_objects_from_context(objects_ai)

        objects: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for item in parsed:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                name = item["name"]
                key = self._object_match_key(name)
                if key and key not in seen:
                    seen.add(key)
                    objects.append(dict(item))
            elif isinstance(item, str):
                key = self._object_match_key(item)
                if key and key not in seen:
                    seen.add(key)
                    objects.append({"name": item})

        return objects

    def _object_name_aliases(self, name: str) -> List[str]:
        """Return text aliases for object-style names such as LightSwitch."""
        raw = str(name).strip()
        if not raw:
            return []

        camel_spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', raw)
        camel_spaced = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', camel_spaced)
        word_spaced = re.sub(r'[\s_\-]+', ' ', camel_spaced).strip()
        compact = re.sub(r'[\s_\-]+', '', raw)
        snake = re.sub(r'\s+', '_', word_spaced.lower())
        hyphen = re.sub(r'\s+', '-', word_spaced.lower())

        aliases = [
            raw,
            raw.lower(),
            word_spaced,
            word_spaced.lower(),
            compact,
            compact.lower(),
            snake,
            hyphen,
        ]

        deduped: List[str] = []
        seen: Set[str] = set()
        for alias in aliases:
            alias = alias.strip()
            key = alias.lower()
            if alias and key not in seen:
                seen.add(key)
                deduped.append(alias)
        return deduped

    def _text_contains_alias(self, text: str, alias: str) -> bool:
        tokens = re.findall(r'[a-z0-9]+', alias.lower())
        if not tokens:
            return False
        pattern = r'(?<![a-z0-9])' + r'[\s_\-]*'.join(re.escape(token) for token in tokens) + r'(?![a-z0-9])'
        return re.search(pattern, text.lower()) is not None

    def _text_contains_name(self, text: str, name: str) -> bool:
        return any(self._text_contains_alias(text, alias) for alias in self._object_name_aliases(name))

    def _extract_key_objects_from_decomposition(
        self,
        decomposed_plan: str,
        objects_ai: Union[str, List[Any]],
    ) -> List[Dict[str, Any]]:
        """Find floorplan objects mentioned in the decomposed plan."""
        floorplan_objects = self._parse_objects_ai(objects_ai)
        if not decomposed_plan or not floorplan_objects:
            return []

        key_objects: List[Dict[str, Any]] = []
        for obj in floorplan_objects:
            name = obj.get("name")
            if isinstance(name, str) and self._text_contains_name(decomposed_plan, name):
                key_objects.append(obj)
        return key_objects

    def _extract_key_objects_by_subtask(
        self,
        subtasks: List[str],
        objects_ai: Union[str, List[Any]],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Find floorplan objects mentioned in each decomposed subtask."""
        key_objects_by_subtask: Dict[int, List[Dict[str, Any]]] = {
            subtask_idx: []
            for subtask_idx, _ in enumerate(subtasks, start=1)
        }
        floorplan_objects = self._parse_objects_ai(objects_ai)
        if not subtasks or not floorplan_objects:
            return key_objects_by_subtask

        for subtask_idx, subtask in enumerate(subtasks, start=1):
            for obj in floorplan_objects:
                name = obj.get("name")
                if isinstance(name, str) and self._text_contains_name(subtask, name):
                    key_objects_by_subtask[subtask_idx].append(obj)

        return key_objects_by_subtask

    def _combine_key_objects_by_subtask(
        self,
        key_objects_by_subtask: Dict[int, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Deduplicate split key objects into the legacy flat key_objects shape."""
        key_objects: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for subtask_key_objects in key_objects_by_subtask.values():
            for obj in subtask_key_objects:
                name = obj.get("name")
                if not isinstance(name, str):
                    continue
                key = self._object_match_key(name)
                if key and key not in seen:
                    seen.add(key)
                    key_objects.append(obj)
        return key_objects

    @staticmethod
    def _pddl_safe_object_token(value: Any, fallback: str = "object") -> str:
        """Convert AI2-THOR identifiers into conservative PDDL object tokens."""
        fallback_token = re.sub(r'[^A-Za-z0-9_]+', '_', str(fallback) or "object").strip('_') or "object"
        raw = str(value).strip() if value is not None else ""
        if not raw:
            raw = fallback_token

        raw = raw.replace("+", "_pos_").replace("-", "_neg_")
        token = re.sub(r'[^A-Za-z0-9_]+', '_', raw)
        token = re.sub(r'_+', '_', token).strip('_')
        if not token:
            token = fallback_token
        if re.match(r'^[0-9]', token):
            token = f"{fallback_token}_{token}"
        return token

    def _scene_name_for_ai2thor_metadata(self) -> Optional[str]:
        if not self.floor_plan:
            return None
        floor_plan_id = normalize_floor_plan(str(self.floor_plan))
        if not floor_plan_id:
            return None
        return f"FloorPlan{floor_plan_id}"

    def _load_floor_ai2thor_metadata(self) -> List[Dict[str, Any]]:
        """Load instance-level AI2-THOR metadata for the current floor plan."""
        scene_name = self._scene_name_for_ai2thor_metadata()
        if not scene_name:
            return []

        try:
            metadata_path = self.config.ai2thor_objects_file
        except AttributeError:
            metadata_path = Path(self.base_path) / "data" / "all_ai2thor_objects.json"

        try:
            with open(metadata_path, "r", encoding="utf-8") as metadata_file:
                raw_metadata = json.load(metadata_file)
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(raw_metadata, list):
            return []

        return [
            dict(item)
            for item in raw_metadata
            if isinstance(item, dict) and item.get("scene") == scene_name
        ]

    def _build_floor_object_numbering(
        self,
        floor_objects: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build one scene-wide numbering table for AI2-THOR objects."""
        object_type_counts: Dict[str, int] = {}
        for item in floor_objects:
            object_type = item.get("objectType")
            if isinstance(object_type, str) and object_type:
                key = self._object_match_key(object_type)
                object_type_counts[key] = object_type_counts.get(key, 0) + 1

        numbered_objects: List[Dict[str, Any]] = []
        by_object_id: Dict[str, Dict[str, Any]] = {}
        object_type_indices: Dict[str, int] = {}
        for item in floor_objects:
            object_type = item.get("objectType")
            if not isinstance(object_type, str) or not object_type:
                continue
            key = self._object_match_key(object_type)
            object_type_indices[key] = object_type_indices.get(key, 0) + 1
            number = object_type_indices[key]
            count = object_type_counts.get(key, 1)
            base_token = self._pddl_safe_object_token(object_type, "object")
            token = (
                base_token
                if count == 1
                else f"{base_token}_{number}"
            )
            object_id = item.get("objectId")
            object_id_text = object_id if isinstance(object_id, str) else ""
            entry = {
                "object": token,
                "object_type": object_type,
                "object_id": object_id_text,
                "number": number,
                "count": count,
                "multiple": count > 1,
            }
            numbered_objects.append(entry)
            if isinstance(object_id, str) and object_id:
                by_object_id[object_id] = entry

        return {
            "objects": numbered_objects,
            "by_object_id": by_object_id,
            "type_counts": object_type_counts,
        }

    def _object_token_maps_for_metadata(
        self,
        floor_objects: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, int], Dict[str, str]]:
        floor_object_numbering = self._build_floor_object_numbering(floor_objects)
        type_counts = floor_object_numbering.get("type_counts", {})
        by_object_id = floor_object_numbering.get("by_object_id", {})
        token_by_object_id = {
            object_id: entry["object"]
            for object_id, entry in by_object_id.items()
            if isinstance(object_id, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("object"), str)
        }
        return type_counts, token_by_object_id

    def _object_numbering_entry_for_metadata(
        self,
        item: Dict[str, Any],
        floor_object_numbering: Dict[str, Any],
    ) -> Dict[str, Any]:
        object_id = item.get("objectId")
        by_object_id = floor_object_numbering.get("by_object_id", {})
        if isinstance(by_object_id, dict) and isinstance(object_id, str) and object_id in by_object_id:
            entry = by_object_id[object_id]
            if isinstance(entry, dict):
                return entry

        object_type = item.get("objectType")
        type_text = object_type if isinstance(object_type, str) and object_type else "object"
        type_key = self._object_match_key(type_text)
        type_counts = floor_object_numbering.get("type_counts", {})
        count = type_counts.get(type_key, 0) if isinstance(type_counts, dict) else 0
        count = count or 1
        base_token = self._pddl_safe_object_token(type_text, "object")
        object_token = base_token if count == 1 else f"{base_token}_1"
        return {
            "object": object_token,
            "object_type": type_text,
            "object_id": object_id if isinstance(object_id, str) else "",
            "number": 1,
            "count": count,
            "multiple": count > 1,
        }

    def _metadata_object_token(
        self,
        item: Dict[str, Any],
        floor_object_numbering: Dict[str, Any],
        token_by_object_id: Optional[Dict[str, str]] = None,
    ) -> str:
        if token_by_object_id is not None:
            object_id = item.get("objectId")
            if isinstance(object_id, str) and object_id in token_by_object_id:
                return token_by_object_id[object_id]

            object_type = item.get("objectType")
            type_text = object_type if isinstance(object_type, str) and object_type else "object"
            type_key = self._object_match_key(type_text)
            count = floor_object_numbering.get(type_key, 0) if isinstance(floor_object_numbering, dict) else 0
            if count == 1:
                return self._pddl_safe_object_token(type_text, "object")
            return f"{self._pddl_safe_object_token(type_text, 'object')}_1"

        return str(self._object_numbering_entry_for_metadata(item, floor_object_numbering)["object"])

    def _object_numbering_entry_for_ai2thor_object_id(
        self,
        object_id: str,
        floor_object_numbering: Dict[str, Any],
    ) -> Dict[str, Any]:
        by_object_id = floor_object_numbering.get("by_object_id", {})
        if isinstance(by_object_id, dict) and object_id in by_object_id:
            entry = by_object_id[object_id]
            if isinstance(entry, dict):
                return entry

        object_type = object_id.split("|", 1)[0].strip() or "object"
        return {
            "object": self._pddl_safe_object_token(object_type, "object"),
            "object_type": object_type,
            "object_id": object_id,
            "number": 1,
            "count": 1,
            "multiple": False,
        }

    def _object_token_for_ai2thor_object_id(
        self,
        object_id: str,
        floor_object_numbering: Dict[str, Any],
    ) -> str:
        if "by_object_id" not in floor_object_numbering:
            token = floor_object_numbering.get(object_id)
            if isinstance(token, str):
                return token

        entry = self._object_numbering_entry_for_ai2thor_object_id(
            object_id,
            floor_object_numbering,
        )
        return str(entry["object"])

    @staticmethod
    def _first_parent_receptacle(item: Dict[str, Any]) -> Optional[str]:
        parents = item.get("parentReceptacles")
        if isinstance(parents, str):
            return parents if parents else None
        if isinstance(parents, list):
            for parent in parents:
                if isinstance(parent, str) and parent:
                    return parent
        return None

    @staticmethod
    def _pddl_predicate_names_from_block(predicates_block: str) -> Set[str]:
        return set(re.findall(r'\(\s*([A-Za-z][A-Za-z0-9_-]*)\b', predicates_block))

    def _extract_pddl_predicate_blocks(self, domain_content: str) -> List[str]:
        """Extract balanced (:predicates ...) blocks from PDDL content."""
        if not domain_content:
            return []

        blocks: List[str] = []
        for start_match in re.finditer(r'\(\s*:predicates\b', domain_content, re.IGNORECASE):
            start = start_match.start()
            depth = 0
            end: Optional[int] = None
            for idx in range(start, len(domain_content)):
                char = domain_content[idx]
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        end = idx + 1
                        break

            if end is not None:
                blocks.append(domain_content[start:end])

        return blocks

    def _extract_pddl_predicate_names(self, domain_content: str) -> Set[str]:
        """Extract declared predicate names from a PDDL domain."""
        predicate_blocks = self._extract_pddl_predicate_blocks(domain_content)
        if not predicate_blocks:
            return set()

        return self._pddl_predicate_names_from_block(predicate_blocks[0])

    def _extract_pddl_type_names(self, domain_content: str) -> Set[str]:
        """Extract declared type names from a PDDL domain."""
        if not domain_content:
            return set()

        start_match = re.search(r'\(\s*:types\b', domain_content, re.IGNORECASE)
        if not start_match:
            return set()

        start = start_match.start()
        depth = 0
        end: Optional[int] = None
        for idx in range(start, len(domain_content)):
            char = domain_content[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    break

        if end is None:
            return set()

        types_block = re.sub(r';.*', '', domain_content[start:end])
        tokens = re.findall(r'[A-Za-z][A-Za-z0-9_-]*|-', types_block)
        return {
            token
            for token in tokens
            if token not in {"types", ":types", "-"}
        }

    def _allaction_pddl_type_names(self) -> Set[str]:
        try:
            domain_content = self.file_processor.read_file(str(self.config.allaction_domain_path()))
        except PDDLError:
            return {"object"}
        return self._extract_pddl_type_names(domain_content) or {"object"}

    @staticmethod
    def _normalize_ai2thor_type_to_pddl_type(object_type: str) -> str:
        spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', str(object_type).strip())
        spaced = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', '_', spaced)
        return re.sub(r'[^A-Za-z0-9_]+', '_', spaced).strip('_').lower()

    def _pddl_type_for_ai2thor_object_type(
        self,
        object_type: str,
        allaction_types: Set[str],
    ) -> str:
        candidate = self._normalize_ai2thor_type_to_pddl_type(object_type)
        return candidate if candidate in allaction_types else "object"

    @staticmethod
    def _predicate_name_from_fact(fact: str) -> Optional[str]:
        match = re.match(r'\(\s*([A-Za-z][A-Za-z0-9_-]*)\b', str(fact).strip())
        return match.group(1) if match else None

    def _build_key_object_facts(
        self,
        item: Dict[str, Any],
        object_token: str,
        parent_token: Optional[str],
        supported_predicates: Set[str],
    ) -> List[str]:
        facts: List[str] = []

        def add_fact(predicate: str, fact: str) -> None:
            if predicate in supported_predicates:
                facts.append(fact)

        object_type_key = self._object_match_key(item.get("objectType", ""))
        if parent_token:
            add_fact("at-location", f"(at-location {object_token} {parent_token})")
        if bool(item.get("isOpen")):
            add_fact("object-open", f"(object-open {object_token})")
        if bool(item.get("isToggled")):
            add_fact("switch-on", f"(switch-on {object_token})")
        if object_type_key in {"kettle", "pan", "pot"}:
            add_fact("placable_on_stove_burner", f"(placable_on_stove_burner {object_token})")
        if object_type_key in {"apple", "bread"}:
            add_fact("cookable-by-microwave", f"(cookable-by-microwave {object_token})")
        if object_type_key in {"potato", "tomato"}:
            add_fact("cookable-by-stove_burner", f"(cookable-by-stove_burner {object_token})")

        return facts

    def _build_key_object_pddl_states(
        self,
        key_objects: List[Dict[str, Any]],
        domain_content: str,
    ) -> List[Dict[str, Any]]:
        """Convert key AI2-THOR objects into PDDL-ready state facts."""
        return self._build_key_object_pddl_context(key_objects, domain_content)["states"]

    def _build_key_object_pddl_context(
        self,
        key_objects: List[Dict[str, Any]],
        domain_content: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Convert key objects into PDDL states plus object-token bindings."""
        if not key_objects:
            return {"states": [], "object_id_bindings": [], "evidence": []}

        key_object_types = {
            self._object_match_key(obj.get("name", ""))
            for obj in key_objects
            if isinstance(obj, dict) and isinstance(obj.get("name"), str)
        }
        key_object_types.discard("")
        if not key_object_types:
            return {"states": [], "object_id_bindings": [], "evidence": []}

        floor_objects = self._load_floor_ai2thor_metadata()
        if not floor_objects:
            return {"states": [], "object_id_bindings": [], "evidence": []}

        supported_predicates = self._extract_pddl_predicate_names(domain_content)
        floor_object_numbering = self._build_floor_object_numbering(floor_objects)
        floor_object_by_id = {
            item["objectId"]: item
            for item in floor_objects
            if isinstance(item.get("objectId"), str) and item.get("objectId")
        }
        allaction_types = self._allaction_pddl_type_names()
        states: List[Dict[str, Any]] = []
        evidence: List[Dict[str, Any]] = []
        bindings_by_object_id: Dict[str, Dict[str, Any]] = {}

        def record_binding(entry: Dict[str, Any], role: str) -> None:
            object_id = entry.get("object_id")
            object_token = entry.get("object")
            object_type = entry.get("object_type")
            if not isinstance(object_id, str) or not object_id or not isinstance(object_token, str):
                return

            binding = bindings_by_object_id.get(object_id)
            if binding is None:
                binding = {
                    "object": object_token,
                    "object_type": object_type if isinstance(object_type, str) and object_type else "object",
                    "object_id": object_id,
                    "number": entry.get("number", 1),
                    "count": entry.get("count", 1),
                    "multiple": bool(entry.get("multiple", False)),
                    "roles": [],
                }
                bindings_by_object_id[object_id] = binding

            roles = binding.setdefault("roles", [])
            if isinstance(roles, list) and role not in roles:
                roles.append(role)

        for item in floor_objects:
            object_type = item.get("objectType")
            if not isinstance(object_type, str) or self._object_match_key(object_type) not in key_object_types:
                continue

            object_entry = self._object_numbering_entry_for_metadata(item, floor_object_numbering)
            object_token = str(object_entry["object"])
            record_binding(object_entry, "key_object")
            parent_id = self._first_parent_receptacle(item)
            parent_entry = (
                self._object_numbering_entry_for_ai2thor_object_id(parent_id, floor_object_numbering)
                if parent_id
                else None
            )
            if (
                parent_entry
                and self._object_match_key(str(parent_entry.get("object_type", ""))) == "floor"
            ):
                parent_entry = None
            parent_token = (
                str(parent_entry["object"])
                if parent_entry
                else None
            )
            facts = self._build_key_object_facts(
                item,
                object_token,
                parent_token,
                supported_predicates,
            )
            evidence.extend(
                build_key_object_evidence(
                    item,
                    object_token,
                    parent_token,
                    facts,
                    supported_predicates,
                )
            )
            state_entry: Dict[str, Any] = {
                "object": object_token,
                "object_type": self._pddl_type_for_ai2thor_object_type(object_type, allaction_types),
                "facts": facts,
            }
            if parent_id and parent_token:
                parent_item = floor_object_by_id.get(parent_id, {})
                parent_object_type = parent_item.get("objectType")
                parent_pddl_type = (
                    self._pddl_type_for_ai2thor_object_type(parent_object_type, allaction_types)
                    if isinstance(parent_object_type, str)
                    else "object"
                )
                state_entry["related_objects"] = [
                    {
                        "object": parent_token,
                        "object_type": parent_pddl_type,
                        "role": "parentReceptacle",
                    }
                ]
                if parent_entry:
                    record_binding(parent_entry, "parentReceptacle")
            states.append(state_entry)

        return {
            "states": states,
            "object_id_bindings": list(bindings_by_object_id.values()),
            "evidence": evidence,
        }

    def _filter_key_object_pddl_states_for_domain(
        self,
        key_object_pddl_states: Optional[List[Dict[str, Any]]],
        domain_content: str,
    ) -> List[Dict[str, Any]]:
        supported_predicates = self._extract_pddl_predicate_names(domain_content)
        return self._filter_key_object_pddl_states_by_predicates(
            key_object_pddl_states,
            supported_predicates,
        )

    def _filter_key_object_pddl_states_by_predicates(
        self,
        key_object_pddl_states: Optional[List[Dict[str, Any]]],
        supported_predicates: Set[str],
    ) -> List[Dict[str, Any]]:
        if not key_object_pddl_states:
            return []

        filtered_states: List[Dict[str, Any]] = []
        for entry in key_object_pddl_states:
            if not isinstance(entry, dict):
                continue
            facts = entry.get("facts", [])
            filtered_entry = {
                key: value
                for key, value in entry.items()
                if key != "object_id"
            }
            related_objects = filtered_entry.get("related_objects")
            if isinstance(related_objects, list):
                filtered_entry["related_objects"] = [
                    {
                        key: value
                        for key, value in related_object.items()
                        if key != "object_id"
                    }
                    for related_object in related_objects
                    if isinstance(related_object, dict)
                ]
            filtered_entry["facts"] = [
                fact
                for fact in facts
                if isinstance(fact, str)
                and self._predicate_name_from_fact(fact) in supported_predicates
            ]
            filtered_states.append(filtered_entry)

        return filtered_states

    def _run_decompose_generation(
        self,
        task: str,
        domain_content: str,
        robots: List[dict],
        objects_ai: str,
    ) -> Dict[str, str]:
        """Build the decomposition prompt and run the LLM without writing artifacts."""
        try:
            decompose_prompt = self._rag_or_static_prompt_block(
                self.decompose_rag_retriever,
                self._decompose_rag_prompt_block,
                self._read_decompose_static_prompt,
                f"Task: {task}",
            )
            decompose_prompt = _strip_legacy_inaction_prompt_text(decompose_prompt)
            
            # Construct the prompt incrementally like the original
            prompt = f"from pddl domain file with all possible actions: \n{domain_content}\n\n"
            prompt += self._format_decompose_objects_prompt(objects_ai)
            prompt += "\n\n"
            prompt += decompose_prompt
            prompt += "# GENERAL TASK DECOMPOSITION \n"
            prompt += "Decompose and parallel subtasks where ever possible.\n"
            prompt += "Strictly follow the format in the examples above when examples are provided.\n"
            prompt += f"# Task Description: {task}"
            
            messages = [{"role": "user", "content": prompt}]
            call_config = self.config.llm_call("decompose")
            _, text = self.llm.query_model(
                messages,
                self.model,
                max_tokens=call_config.get("max_tokens", 1300),
                frequency_penalty=call_config.get("frequency_penalty", 0.0),
            )

            return {"prompt": prompt, "text": text}
            
        except Exception as e:
            raise PDDLError(f"Error generating decomposed plan: {str(e)}")

    def _generate_decomposed_plan(
        self,
        task: str,
        domain_content: str,
        robots: List[dict],
        objects_ai: str,
        write_artifacts: bool = True,
    ) -> str:
        """Generate decomposed plan for a task."""
        try:
            result = self._run_decompose_generation(task, domain_content, robots, objects_ai)
            prompt = result["prompt"]
            text = result["text"]

            if not write_artifacts:
                return text

            decompose_prompt_artifact = self.config.artifact("decompose_prompt", "01_decompose/01_decompose_prompt.txt")
            decompose_output_artifact = self.config.artifact("decompose_output", "01_decompose/02_decompose_output.txt")
            self._write_text_artifact(decompose_prompt_artifact, prompt)
            self._record_artifact("decompose", "prompt", decompose_prompt_artifact)
            self._write_text_artifact(decompose_output_artifact, text)
            self._record_artifact("decompose", "output", decompose_output_artifact)
            self._persist_manifest()
            
            return text
            
        except Exception as e:
            raise PDDLError(f"Error generating decomposed plan: {str(e)}")

    @staticmethod
    def _strip_decomposition_completion_sentence(decomposed_plan: str) -> str:
        """Remove decomposition completion lines before allocation prompting."""
        completion_line_pattern = re.compile(
            r'^\s*(?:\*\*)?\s*#?\s*Task\s+(?!Description\b).+\s+is done\.?\s*(?:\*\*)?\s*$',
            re.IGNORECASE,
        )
        lines = str(decomposed_plan).splitlines()
        return "\n".join(
            line for line in lines if not completion_line_pattern.match(line)
        ).strip()

    def _allocate_subtasks_with_cpsat(
        self,
        subtasks: List[str],
        decomposed_plan: str,
        problem_pddl: List[str],
        planner_records: List[Dict[str, Any]],
        available_robots: List[dict],
        objects_ai: str,
        preferred_predecessors: Optional[Dict[int, List[int]]] = None,
    ) -> List[Dict[str, Any]]:
        """Filter capable robots from generated plans and solve their allocation."""
        requirements = self._build_subtask_requirements(
            subtasks=subtasks,
            decomposed_plan=decomposed_plan,
            problem_pddl=problem_pddl,
            planner_records=planner_records,
            objects_ai=objects_ai,
            preferred_predecessors=preferred_predecessors,
        )
        actionable_requirements = [
            requirement
            for requirement in requirements
            if requirement.get("requires_execution", True)
        ]
        if actionable_requirements:
            actionable_candidates = self._filter_candidate_robots(
                actionable_requirements,
                available_robots,
            )
            assignments = self._solve_subtask_assignment(
                actionable_requirements,
                available_robots,
            )
        else:
            actionable_candidates = {}
            assignments = {}
        candidates = {
            requirement["subtask_id"]: actionable_candidates.get(
                requirement["subtask_id"],
                [],
            )
            for requirement in requirements
        }

        output: List[Dict[str, Any]] = []
        for requirement in requirements:
            subtask_id = requirement["subtask_id"]
            requires_execution = requirement.get("requires_execution", True)
            if requires_execution:
                assignment = assignments[subtask_id]
                assigned_robot = assignment["robot_name"]
                assigned_robot_domain = self.current_robot_domain_names.get(
                    assigned_robot,
                    assigned_robot,
                )
                start = assignment["start"]
                end = assignment["end"]
            else:
                assigned_robot = None
                assigned_robot_domain = None
                start = 0
                end = 0
            output.append(
                {
                    "subtask_id": subtask_id,
                    "name": requirement["name"],
                    "predecessor_ids": requirement["predecessor_ids"],
                    "requires_execution": requires_execution,
                    "required_skills": requirement["required_skills"],
                    "min_mass_capacity": requirement["min_mass_capacity"],
                    "required_locations": requirement["required_locations"],
                    "unresolved_location_actions": requirement[
                        "unresolved_location_actions"
                    ],
                    "candidate_robots": candidates[subtask_id],
                    "assigned_robot": assigned_robot,
                    "assigned_robot_domain": assigned_robot_domain,
                    "start": start,
                    "end": end,
                }
            )

        allocate_subtasks_artifact = self.config.artifact(
            "allocate_subtasks",
            "02_allocate/subtasks.json",
        )
        requirements_artifact = "02_allocate/requirements.json"
        candidates_artifact = "02_allocate/candidate_robots.json"
        self._write_json_artifact(requirements_artifact, requirements)
        self._write_json_artifact(candidates_artifact, candidates)
        self._write_json_artifact(allocate_subtasks_artifact, output)
        self._record_artifact("allocate", "requirements", requirements_artifact)
        self._record_artifact("allocate", "candidate_robots", candidates_artifact)
        self._record_artifact("allocate", "subtasks", allocate_subtasks_artifact)
        self._persist_manifest()
        return output

    def _build_subtask_requirements(
        self,
        subtasks: List[str],
        decomposed_plan: str,
        problem_pddl: List[str],
        planner_records: List[Dict[str, Any]],
        objects_ai: str,
        preferred_predecessors: Optional[Dict[int, List[int]]] = None,
    ) -> List[Dict[str, Any]]:
        """Derive robot skills and payload needs from each full-capability plan."""
        records_by_subtask = {
            subtask_id: record
            for record in planner_records
            for subtask_id in [
                self._subtask_id_from_filename(str(record.get("problem_file", "")))
            ]
            if subtask_id is not None
        }
        object_masses = self._parse_object_masses(objects_ai)
        predecessors = (
            self._normalize_predecessor_map(
                preferred_predecessors,
                len(subtasks),
            )
            if preferred_predecessors is not None
            else None
        )
        if predecessors is None:
            predecessors = self._infer_predecessors(decomposed_plan, problem_pddl)

        requirements: List[Dict[str, Any]] = []
        for subtask_id, subtask in enumerate(subtasks, start=1):
            record = records_by_subtask.get(subtask_id)
            if not record:
                raise PDDLError(f"No planner record for subtask {subtask_id}")
            plan_path = record.get("compatibility_output")
            if not plan_path or not os.path.isfile(str(plan_path)):
                raise PDDLError(
                    f"No full-capability plan file for subtask {subtask_id}: {plan_path}"
                )
            plan_text = self.file_processor.read_file(str(plan_path))
            actions_in_plan = self._parse_plan_actions(plan_text)
            requires_execution = bool(actions_in_plan)
            fallback_problem = (
                problem_pddl[subtask_id - 1]
                if subtask_id - 1 < len(problem_pddl)
                else ""
            )
            planner_problem = self._read_planner_problem_content(
                record,
                fallback_problem,
            )
            noop_proof: Optional[Dict[str, Any]] = None
            if not requires_execution:
                noop_proof = verify_zero_action_plan(
                    subtask_id=subtask_id,
                    problem=planner_problem,
                    plan_text=plan_text,
                    evidence=self._key_object_evidence_for_subtask(subtask_id),
                    planner_record=record,
                )
                self._write_noop_audit_records([noop_proof])
                if not noop_proof["verified"]:
                    reasons = ", ".join(noop_proof["failure_reasons"])
                    raise PDDLError(
                        "Unverified zero-action plan for subtask "
                        f"{subtask_id}: {reasons or plan_path}"
                    )
            if requires_execution:
                required_locations, unresolved_location_actions = (
                    self._extract_required_locations(
                        actions_in_plan,
                        planner_problem,
                    )
                )
                required_skills = self._extract_required_skills(actions_in_plan)
                min_mass_capacity = round(
                    self._calculate_pickup_mass_peak(
                        actions_in_plan,
                        object_masses,
                    ),
                    6,
                )
            else:
                required_locations = []
                unresolved_location_actions = []
                required_skills = []
                min_mass_capacity = 0.0
            requirements.append(
                {
                    "subtask_id": subtask_id,
                    "name": self._extract_subtask_name(subtask, subtask_id),
                    "predecessor_ids": predecessors.get(subtask_id, []),
                    "requires_execution": requires_execution,
                    "required_skills": required_skills,
                    "min_mass_capacity": min_mass_capacity,
                    "duration": len(actions_in_plan),
                    "required_locations": required_locations,
                    "unresolved_location_actions": unresolved_location_actions,
                    "plan_path": str(plan_path),
                    "noop_proof": noop_proof,
                }
            )
        return requirements

    def _write_noop_audit_records(self, records: Sequence[Dict[str, Any]]) -> None:
        artifact = "08_planner/noop_subtasks.json"
        existing = self._read_json_artifact(artifact, [])
        if not isinstance(existing, list):
            existing = []
        by_id = {
            int(item["subtask_id"]): item
            for item in existing
            if isinstance(item, dict) and str(item.get("subtask_id", "")).isdigit()
        }
        for record in records:
            subtask_id = record.get("subtask_id")
            if str(subtask_id).isdigit():
                by_id[int(subtask_id)] = record
        self._write_json_artifact(artifact, [by_id[key] for key in sorted(by_id)])
        self._record_artifact("planner", "noop_subtasks", artifact)

    def _filter_candidate_robots(
        self,
        requirements: List[Dict[str, Any]],
        available_robots: List[dict],
    ) -> Dict[int, List[str]]:
        """Return the robots satisfying every plan-derived constraint per subtask."""
        candidates: Dict[int, List[str]] = {}
        for requirement in requirements:
            required_skills = {
                self._canonical_skill_name(skill)
                for skill in requirement["required_skills"]
            }
            candidate_names = []
            for robot_index, robot in enumerate(available_robots, start=1):
                robot_skills = {
                    self._canonical_skill_name(skill)
                    for skill in robot.get("skills", [])
                }
                try:
                    mass_capacity = float(
                        robot.get("mass_capacity", robot.get("mass", 0)) or 0
                    )
                except (TypeError, ValueError):
                    mass_capacity = 0.0
                if required_skills.issubset(robot_skills) and mass_capacity >= float(
                    requirement["min_mass_capacity"]
                ):
                    candidate_names.append(
                        str(robot.get("name") or f"robot{robot_index}")
                    )
            if not candidate_names:
                raise PDDLError(
                    "No feasible robot for subtask "
                    f"{requirement['subtask_id']} skills={requirement['required_skills']} "
                    f"min_mass_capacity={requirement['min_mass_capacity']}"
                )
            candidates[requirement["subtask_id"]] = candidate_names
        return candidates

    def _solve_subtask_assignment(
        self,
        requirements: List[Dict[str, Any]],
        available_robots: List[dict],
    ) -> Dict[int, Dict[str, Any]]:
        """Solve binary robot assignment and non-overlapping scheduling with CP-SAT."""
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:
            raise PDDLError(
                "OR-Tools is required for integer-programming robot allocation."
            ) from exc
        if not requirements:
            return {}
        if not available_robots:
            raise PDDLError("No robots available for integer-programming allocation")

        candidates = self._filter_candidate_robots(requirements, available_robots)
        candidate_sets = {
            subtask_id: set(names) for subtask_id, names in candidates.items()
        }
        robot_names = [
            str(robot.get("name") or f"robot{index}")
            for index, robot in enumerate(available_robots, start=1)
        ]
        task_index_by_id = {
            requirement["subtask_id"]: index
            for index, requirement in enumerate(requirements)
        }
        horizon = max(1, sum(int(req["duration"]) for req in requirements))
        model = cp_model.CpModel()
        starts = [
            model.NewIntVar(0, horizon, f"start_{req['subtask_id']}")
            for req in requirements
        ]
        ends = [
            model.NewIntVar(0, horizon, f"end_{req['subtask_id']}")
            for req in requirements
        ]
        assigned: Dict[Tuple[int, int], Any] = {}
        task_intervals: List[Any] = []
        intervals_by_robot: Dict[int, List[Any]] = {
            robot_index: [] for robot_index in range(len(available_robots))
        }

        for task_index, requirement in enumerate(requirements):
            duration = int(requirement["duration"])
            model.Add(ends[task_index] == starts[task_index] + duration)
            task_intervals.append(
                model.NewIntervalVar(
                    starts[task_index],
                    duration,
                    ends[task_index],
                    f"task_interval_{requirement['subtask_id']}",
                )
            )
            choices = []
            for robot_index, robot_name in enumerate(robot_names):
                variable = model.NewBoolVar(
                    f"x_{requirement['subtask_id']}_{robot_index + 1}"
                )
                assigned[(task_index, robot_index)] = variable
                if robot_name not in candidate_sets[requirement["subtask_id"]]:
                    model.Add(variable == 0)
                choices.append(variable)
                intervals_by_robot[robot_index].append(
                    model.NewOptionalIntervalVar(
                        starts[task_index],
                        duration,
                        ends[task_index],
                        variable,
                        f"interval_{requirement['subtask_id']}_{robot_index + 1}",
                    )
                )
            model.Add(sum(choices) == 1)

        for intervals in intervals_by_robot.values():
            model.AddNoOverlap(intervals)

        intervals_by_location: Dict[str, List[Any]] = {}
        for task_index, requirement in enumerate(requirements):
            seen_location_keys: Set[str] = set()
            for location in requirement.get("required_locations", []):
                location_key = self._object_key(location)
                if not location_key or location_key in seen_location_keys:
                    continue
                seen_location_keys.add(location_key)
                intervals_by_location.setdefault(location_key, []).append(
                    task_intervals[task_index]
                )
        for intervals in intervals_by_location.values():
            if len(intervals) > 1:
                model.AddNoOverlap(intervals)

        for task_index, requirement in enumerate(requirements):
            for predecessor_id in requirement["predecessor_ids"]:
                predecessor_index = task_index_by_id.get(predecessor_id)
                if predecessor_index is not None:
                    model.Add(starts[task_index] >= ends[predecessor_index])

        makespan = model.NewIntVar(0, horizon, "makespan")
        model.AddMaxEquality(makespan, ends)
        used_robots = []
        for robot_index in range(len(available_robots)):
            used = model.NewBoolVar(f"used_robot_{robot_index + 1}")
            robot_assignments = [
                assigned[(task_index, robot_index)]
                for task_index in range(len(requirements))
            ]
            for variable in robot_assignments:
                model.Add(variable <= used)
            model.Add(sum(robot_assignments) >= used)
            used_robots.append(used)

        tie_break_robot_order = sum(
            (robot_index + 1) * assigned[(task_index, robot_index)]
            for task_index in range(len(requirements))
            for robot_index in range(len(available_robots))
        )
        used_weight = len(requirements) * len(available_robots) + 1
        makespan_weight = used_weight * (len(available_robots) + 1)
        model.Minimize(
            makespan * makespan_weight
            + sum(used_robots) * used_weight
            + tie_break_robot_order
        )

        solver = cp_model.CpSolver()
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise PDDLError(
                f"Integer-programming allocation failed: {solver.StatusName(status)}"
            )
        assignments: Dict[int, Dict[str, Any]] = {}
        for task_index, requirement in enumerate(requirements):
            chosen_robot_index = next(
                (
                    robot_index
                    for robot_index in range(len(available_robots))
                    if solver.Value(assigned[(task_index, robot_index)])
                ),
                None,
            )
            if chosen_robot_index is None:
                raise PDDLError(
                    f"Integer program did not assign subtask {requirement['subtask_id']}"
                )
            assignments[requirement["subtask_id"]] = {
                "robot_name": robot_names[chosen_robot_index],
                "robot_index": chosen_robot_index + 1,
                "start": solver.Value(starts[task_index]),
                "end": solver.Value(ends[task_index]),
            }
        return assignments

    @staticmethod
    def _extract_subtask_name(subtask: str, subtask_id: int) -> str:
        for line in str(subtask).splitlines():
            match = _match_subtask_header_line(line.strip(), allow_bare=True)
            if match:
                return match.group("title").strip()
        return f"Subtask {subtask_id}"

    def _parse_plan_actions(self, plan_text: str) -> List[Dict[str, Any]]:
        """Parse Fast Downward plan lines into canonical skills and arguments."""
        actions_in_plan: List[Dict[str, Any]] = []
        for line in str(plan_text).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(";"):
                continue
            action_match = re.search(r"\(([^()]+)\)", stripped)
            if action_match:
                inner = action_match.group(1).strip()
            else:
                inner = re.sub(r"\s*\(\d+(?:\.\d+)?\)\s*$", "", stripped)
                inner = re.sub(r"^\s*\d+(?:\.\d+)?\s*:\s*", "", inner).strip()
            parts = inner.split()
            if not parts:
                continue
            actions_in_plan.append(
                {
                    "raw": stripped,
                    "skill": self._canonical_skill_name(parts[0]),
                    "args": parts[1:],
                }
            )
        return actions_in_plan

    def _read_planner_problem_content(
        self,
        planner_record: Dict[str, Any],
        fallback_problem: str,
    ) -> str:
        """Read the exact problem used by the planner, with generated-PDDL fallback."""
        candidate_paths: List[str] = []
        problem_path = planner_record.get("problem_path")
        if problem_path:
            candidate_paths.append(str(problem_path))

        task_run_dir = getattr(self, "current_task_run_dir", None)
        problem_file = planner_record.get("problem_file")
        if task_run_dir and problem_file:
            candidate_paths.append(
                os.path.join(
                    str(task_run_dir),
                    "07_validate/outputs",
                    str(problem_file),
                )
            )

        for candidate_path in candidate_paths:
            if not os.path.isfile(candidate_path):
                continue
            try:
                return self.file_processor.read_file(candidate_path)
            except Exception:
                continue
        return str(fallback_problem or "")

    def _parse_initial_object_locations(
        self,
        problem_pddl: str,
    ) -> Dict[str, str]:
        """Return object-to-parent mappings from positive ``at-location`` init facts."""
        init_section = self._extract_pddl_section(problem_pddl, "init")
        if not init_section:
            return {}

        init_section = re.sub(r";.*", "", init_section)
        init_section = re.sub(
            r"\(\s*not\s*\(\s*at-location\b[^()]*\)\s*\)",
            "",
            init_section,
            flags=re.IGNORECASE,
        )
        object_locations: Dict[str, str] = {}
        for match in re.finditer(
            r"\(\s*at-location\s+([^\s()]+)\s+([^\s()]+)\s*\)",
            init_section,
            flags=re.IGNORECASE,
        ):
            object_token, parent_token = match.groups()
            object_key = self._object_key(object_token)
            if object_key:
                object_locations[object_key] = parent_token
        return object_locations

    def _resolve_physical_location(
        self,
        token: Any,
        object_locations: Dict[str, str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve an object or container token to its terminal physical location."""
        current = str(token or "").strip()
        if not current:
            return None, "location token is empty"

        seen: Set[str] = set()
        while True:
            current_key = self._object_key(current)
            if not current_key:
                return None, f"invalid location token: {current!r}"
            if current_key in seen:
                return None, f"at-location cycle detected at {current}"
            seen.add(current_key)
            parent = object_locations.get(current_key)
            if not parent:
                return current, None
            current = str(parent).strip()

    def _extract_required_locations(
        self,
        actions_in_plan: List[Dict[str, Any]],
        problem_pddl: str,
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """Derive whole-subtask physical locations by replaying its action sequence."""
        object_locations = self._parse_initial_object_locations(problem_pddl)
        current_robot_location: Optional[str] = None
        required_locations: List[str] = []
        seen_locations: Set[str] = set()
        unresolved: List[Dict[str, str]] = []

        explicit_location_arguments = {
            "PickupObject": 2,
            "PutObject": 2,
            "SliceObject": 2,
        }
        resource_arguments = {
            "CleanObject": 2,
            "RunMicrowave": 1,
            "RunCoffeeMachine": 1,
            "RunToaster": 1,
            "CookByStoveBurner": 1,
            "HeatByStoveBurner": 1,
            "FillWater": 1,
            "ColdObject": 1,
            "PrepareEgg": 2,
        }
        target_arguments = {
            "GoToObject": 1,
            "OpenObject": 1,
            "CloseObject": 1,
            "BreakObject": 1,
            "SwitchOn": 1,
            "SwitchOff": 1,
            "PushObject": 1,
            "PullObject": 1,
        }
        current_location_actions = {"DropHandObject", "ThrowObject"}

        for action in actions_in_plan:
            skill = str(action.get("skill") or "").strip()
            args = action.get("args", [])
            if not isinstance(args, list):
                args = list(args) if isinstance(args, tuple) else []
            raw_action = str(action.get("raw") or skill)
            location_token: Optional[str] = None
            missing_argument_index: Optional[int] = None

            if skill in explicit_location_arguments:
                argument_index = explicit_location_arguments[skill]
                if len(args) > argument_index:
                    location_token = str(args[argument_index])
                else:
                    missing_argument_index = argument_index
            elif skill in resource_arguments:
                argument_index = resource_arguments[skill]
                if len(args) > argument_index:
                    location_token = str(args[argument_index])
                else:
                    missing_argument_index = argument_index
            elif skill in target_arguments:
                argument_index = target_arguments[skill]
                if len(args) > argument_index:
                    location_token = str(args[argument_index])
                else:
                    missing_argument_index = argument_index
            elif skill in current_location_actions:
                location_token = current_robot_location
                if not location_token:
                    unresolved.append(
                        {
                            "action": raw_action,
                            "reason": "robot location is unknown before this action",
                        }
                    )
            else:
                unresolved.append(
                    {
                        "action": raw_action,
                        "reason": f"unsupported action for location inference: {skill}",
                    }
                )

            if missing_argument_index is not None:
                unresolved.append(
                    {
                        "action": raw_action,
                        "reason": (
                            "missing location argument at index "
                            f"{missing_argument_index}"
                        ),
                    }
                )

            resolved_location: Optional[str] = None
            if location_token:
                resolved_location, resolution_error = self._resolve_physical_location(
                    location_token,
                    object_locations,
                )
                if resolution_error:
                    unresolved.append(
                        {
                            "action": raw_action,
                            "reason": resolution_error,
                        }
                    )
                elif resolved_location:
                    location_key = self._object_key(resolved_location)
                    if location_key and location_key not in seen_locations:
                        seen_locations.add(location_key)
                        required_locations.append(resolved_location)
                    current_robot_location = resolved_location

            object_token = str(args[1]) if len(args) > 1 else None
            if skill == "PickupObject" and object_token:
                object_locations.pop(self._object_key(object_token), None)
            elif skill == "PutObject" and object_token and len(args) > 2:
                object_locations[self._object_key(object_token)] = str(args[2])
            elif skill in current_location_actions and object_token and resolved_location:
                object_locations[self._object_key(object_token)] = resolved_location

        return required_locations, unresolved

    def _extract_required_skills(
        self,
        actions_in_plan: List[Dict[str, Any]],
    ) -> List[str]:
        required_skills: List[str] = []
        seen: Set[str] = set()
        for action in actions_in_plan:
            skill = robot_skill_for_special_task_skill(action["skill"])
            if skill not in seen:
                seen.add(skill)
                required_skills.append(skill)
        return required_skills

    def _calculate_pickup_mass_peak(
        self,
        actions_in_plan: List[Dict[str, Any]],
        object_masses: Dict[str, float],
    ) -> float:
        held: Dict[str, float] = {}
        current_mass = 0.0
        peak_mass = 0.0
        for action in actions_in_plan:
            skill = action["skill"]
            args = action.get("args", [])
            if skill == "PickupObject" and len(args) >= 2:
                object_key = self._object_key(args[1])
                if object_key not in held:
                    mass = object_masses.get(object_key, 0.0)
                    held[object_key] = mass
                    current_mass += mass
                    peak_mass = max(peak_mass, current_mass)
            elif skill in {"PutObject", "DropHandObject", "ThrowObject"} and len(args) >= 2:
                object_key = self._object_key(args[1])
                if object_key in held:
                    current_mass -= held.pop(object_key)
        return max(0.0, peak_mass)

    def _parse_object_masses(self, objects_ai: str) -> Dict[str, float]:
        object_masses: Dict[str, float] = {}
        for item in self._parse_objects_ai(objects_ai):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            try:
                mass = float(item.get("mass", item.get("mass_capacity", 0.0)) or 0.0)
            except (TypeError, ValueError):
                mass = 0.0
            key = self._object_key(item["name"])
            object_masses[key] = max(object_masses.get(key, 0.0), mass)
        return object_masses

    @staticmethod
    def _object_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value).lower())

    def _canonical_skill_name(self, action_name: str) -> str:
        key = self._object_key(action_name)
        aliases = {
            self._object_key(skill): skill
            for skill in (
                "GoToObject",
                "OpenObject",
                "CloseObject",
                "BreakObject",
                "SliceObject",
                "SwitchOn",
                "SwitchOff",
                "PickupObject",
                "PutObject",
                "CleanObject",
                "RunMicrowave",
                "RunCoffeeMachine",
                "RunToaster",
                "CookByStoveBurner",
                "HeatByStoveBurner",
                "FillWater",
                "ColdObject",
                "PrepareEgg",
                "DropHandObject",
                "ThrowObject",
                "PushObject",
                "PullObject",
            )
        }
        aliases.update(SPECIAL_TASK_SKILL_ALIASES)
        return aliases.get(key, str(action_name).strip())

    def _infer_predecessors(
        self,
        decomposed_plan: str,
        problem_pddl: List[str],
    ) -> Dict[int, List[int]]:
        subtask_count = len(problem_pddl)
        edges = self._infer_text_precedence_edges(decomposed_plan)
        init_facts = [self._extract_pddl_facts(problem, "init") for problem in problem_pddl]
        goal_facts = [self._extract_pddl_facts(problem, "goal") for problem in problem_pddl]
        for predecessor_index, predecessor_goals in enumerate(goal_facts, start=1):
            for successor_index, successor_init in enumerate(init_facts, start=1):
                if predecessor_index != successor_index and predecessor_goals & successor_init:
                    edges.add((predecessor_index, successor_index))
        edges = {
            (before, after)
            for before, after in edges
            if 1 <= before <= subtask_count
            and 1 <= after <= subtask_count
            and before != after
        }
        predecessors = self._predecessors_from_edges(edges, subtask_count)
        if self._has_precedence_cycle(predecessors, subtask_count):
            return {subtask_id: [] for subtask_id in range(1, subtask_count + 1)}
        return predecessors

    @staticmethod
    def _infer_text_precedence_edges(decomposed_plan: str) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        for sentence in re.split(r"[\n.]", str(decomposed_plan)):
            lowered = f" {sentence.casefold()} "
            ids = [
                int(value)
                for value in re.findall(r"sub\s*task\s*(\d+)", sentence, re.IGNORECASE)
            ]
            if len(ids) < 2:
                continue
            before_match = re.search(
                r"sub\s*task\s*(\d+).*?\bbefore\b.*?sub\s*task\s*(\d+)",
                sentence,
                re.IGNORECASE,
            )
            after_match = re.search(
                r"sub\s*task\s*(\d+).*?\bafter\b.*?sub\s*task\s*(\d+)",
                sentence,
                re.IGNORECASE,
            )
            depends_match = re.search(
                r"sub\s*task\s*(\d+).*?\bdepends(?:\s+on)?\b.*?sub\s*task\s*(\d+)",
                sentence,
                re.IGNORECASE,
            )
            if before_match:
                edges.add((int(before_match.group(1)), int(before_match.group(2))))
            elif after_match:
                edges.add((int(after_match.group(2)), int(after_match.group(1))))
            elif depends_match:
                edges.add((int(depends_match.group(2)), int(depends_match.group(1))))
            elif any(marker in lowered for marker in (" then ", " sequential ")):
                edges.update(zip(ids, ids[1:]))
        return edges

    def _extract_pddl_facts(self, pddl_content: str, section_name: str) -> Set[str]:
        section = self._extract_pddl_section(pddl_content, section_name)
        if not section:
            return set()
        section = re.sub(r";.*", "", section)
        section = re.sub(
            r"\(\s*not\s+\([^()]*\)\s*\)",
            "",
            section,
            flags=re.IGNORECASE,
        )
        facts: Set[str] = set()
        for match in re.finditer(r"\(\s*([A-Za-z][A-Za-z0-9_-]*)\s+([^()]*)\)", section):
            predicate = match.group(1)
            if predicate.casefold() in {"and", "not"}:
                continue
            args = [arg for arg in match.group(2).split() if arg and not arg.startswith("-")]
            if args:
                facts.add(
                    self._object_key(predicate)
                    + ":"
                    + ",".join(self._object_key(arg) for arg in args)
                )
        return facts

    @staticmethod
    def _extract_pddl_section(pddl_content: str, section_name: str) -> str:
        match = re.search(
            rf"\(\s*:{re.escape(section_name)}\b",
            str(pddl_content),
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        start = match.start()
        depth = 0
        for index in range(start, len(pddl_content)):
            char = pddl_content[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return pddl_content[start:index + 1]
        return pddl_content[start:]
    
    def _generate_problem_summary(self, decomposed_plans: Union[str, List[str]], allocated_plans: Union[str, List[str]], available_robots: Union[List[dict], List[List[dict]]]) -> List[str]:
        """Generate problem summaries from decomposed and allocated plans.
        

            
        Returns:
            List[str]: List of generated problem summaries
        """
        try:
            #print("Generating Allocated summary...")
            
            # Convert single items to lists
            if isinstance(decomposed_plans, str):
                decomposed_plans = [decomposed_plans]
            if isinstance(allocated_plans, str):
                allocated_plans = [allocated_plans]
            if not isinstance(available_robots[0], list):
                available_robots = [available_robots]
            
            # Read summary prompt file
            prompt_file = self.config.prompt_file(f"{self.prompt_allocation_set}_summary.txt")
            with open(prompt_file, "r", encoding="utf-8") as code_prompt_file:
                code_prompt = _strip_legacy_inaction_prompt_text(code_prompt_file.read())
            
            # Build base prompt once
            base_prompt = " finish the problem content summary strictly following the example format"
            base_prompt += "\n\n" + code_prompt + "\n\n"
            
            code_plan = []
            # Process each plan
            for i, (plan, solution) in enumerate(zip(decomposed_plans, allocated_plans)):
                # Build prompt for this plan
                prompt = base_prompt + _strip_legacy_inaction_prompt_text(plan)
                prompt += f"\n# TASK ALLOCATION"
                prompt += f"\n\nrobots = {available_robots[i]}"
                prompt += _strip_legacy_inaction_prompt_text(solution)
                prompt += f"\n# problem content summary  \n"
                prompt_path = f"03_summary/summary_prompt_{i + 1:02d}.txt"
                output_path = f"03_summary/summary_output_{i + 1:02d}.txt"
                self._write_text_artifact(prompt_path, prompt)
                self._record_artifact("summary", f"prompt_{i + 1:02d}", prompt_path)

                messages = [
                    {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                    {"role": "user", "content": prompt}
                ]
                call_config = self.config.llm_call("summary")
                _, text = self.llm.query_model(
                    messages,
                    self.model,
                    frequency_penalty=call_config.get("frequency_penalty", 0.4),
                )
                
                code_plan.append(text)
                self._write_text_artifact(output_path, text)
                self._record_artifact("summary", f"output_{i + 1:02d}", output_path)
            self._persist_manifest()
            
            return code_plan
            
        except Exception as e:
            raise PDDLError(f"Error generating problem summary: {str(e)}")

    @staticmethod
    def _format_problem_prompt_subtask(subtask: str) -> str:
        """Remove a numbered subtask header before building the Problem prompt."""
        lines = str(subtask).strip().splitlines()
        if not lines:
            return ""
        first_line = lines[0].strip()
        match = _match_subtask_header_line(first_line, allow_bare=True)
        if match:
            first_line = match.group("title").strip()
        formatted_lines = [first_line] if first_line else []
        formatted_lines.extend(line.rstrip() for line in lines[1:])
        return "\n".join(formatted_lines).strip()

    def _generate_allaction_problem_files(
        self,
        subtasks: List[str],
        objects_ai: str,
        key_object_pddl_states: Optional[List[Dict[str, Any]]] = None,
        key_object_pddl_states_by_subtask: Optional[
            Dict[Union[int, str], List[Dict[str, Any]]]
        ] = None,
    ) -> List[str]:
        """Generate one Problem PDDL per subtask with the full-capability domain."""
        subtasks_index_artifact = self.config.artifact(
            "subtasks_index",
            "04_problem_files/03_subtasks.json",
        )
        generated_problem_files_artifact = self.config.artifact(
            "generated_problem_files",
            "04_problem_files/04_generated_problem_files.json",
        )
        subtask_entries = []
        for subtask_id, subtask in enumerate(subtasks, start=1):
            relative_path = f"04_problem_files/subtasks/subtask_{subtask_id:02d}.txt"
            self._write_text_artifact(relative_path, subtask)
            subtask_entries.append({"index": subtask_id, "path": relative_path})
        self._write_json_artifact(subtasks_index_artifact, subtask_entries)
        self._record_artifact("problem_files", "subtasks_index", subtasks_index_artifact)

        domain_content = self.file_processor.read_file(
            str(self.config.allaction_domain_path())
        )
        problem_prompt_path = self.config.prompt_file(
            f"{self.prompt_allocation_set}_problem.txt"
        )
        static_problem_prompt = self.file_processor.read_file(str(problem_prompt_path))
        results: List[ProblemGenerationResult] = []

        for subtask_id, subtask in enumerate(subtasks, start=1):
            subtask_states = key_object_pddl_states
            if key_object_pddl_states_by_subtask is not None:
                subtask_states = key_object_pddl_states_by_subtask.get(
                    subtask_id,
                    key_object_pddl_states_by_subtask.get(str(subtask_id), []),
                )
            filtered_states = self._filter_key_object_pddl_states_for_domain(
                subtask_states,
                domain_content,
            )
            prompt_examples = static_problem_prompt
            if self.problem_rag_retriever:
                rag_query = self._problem_rag_query(
                    subtask_id,
                    subtask,
                    "robot1",
                    domain_content,
                    objects_ai,
                    filtered_states,
                )
                manifest_key = f"subtask_{subtask_id:02d}"
                prompt_examples = self._rag_or_static_prompt_block(
                    self.problem_rag_retriever,
                    lambda query, key=manifest_key: self._problem_rag_prompt_block(
                        query,
                        key,
                    ),
                    lambda: static_problem_prompt,
                    rag_query,
                )
            prompt_examples = _strip_legacy_inaction_prompt_text(prompt_examples)
            subtask_text = _strip_legacy_inaction_prompt_text(
                self._format_problem_prompt_subtask(subtask)
            )
            prompt = (
                "\n"
                + prompt_examples
                + "\nGenerate one PDDL problem for the subtask below.\n"
                + "Subtask: "
                + subtask_text
                + "\n\nFull-capability domain file content:\n"
                + domain_content
                + "\n\nAvailable objects:\n"
                + str(objects_ai)
                + "\n\nkey_object_pddl_states = "
                + json.dumps(filtered_states, ensure_ascii=False, indent=2)
                + "\n\nUse (:domain allactionrobot) and use robot1 as the only robot "
                "object. robot1 is a synthetic full-capability robot used only to obtain "
                "the action plan before real robots are filtered and allocated. Declare "
                "every referenced object and use only predicates declared by the domain. "
                "Return only the complete Problem PDDL."
            )
            messages = [
                {"role": "system", "content": "You are a Robot PDDL problem expert."},
                {"role": "user", "content": prompt},
            ]
            call_config = self.config.llm_call("problem_generation")
            _, raw_output = self.llm.query_model(
                messages,
                self.model,
                frequency_penalty=call_config.get("frequency_penalty", 0.4),
            )
            problem = self._extract_pddl_problem_block(raw_output)
            problem = re.sub(
                r"(?<![A-Za-z0-9_])robot\d+(?![A-Za-z0-9_])",
                "robot1",
                problem,
                flags=re.IGNORECASE,
            )
            problem = self._force_problem_domain(problem, "allactionrobot")
            repair_result: Optional[ProblemRepairResult] = None
            if self.problem_repair_enabled:
                repair_result = repair_problem_pddl(
                    problem,
                    domain_content,
                    raw_output=raw_output,
                )
                problem = self._force_problem_domain(
                    repair_result.problem,
                    "allactionrobot",
                )
            result = ProblemGenerationResult(
                subtask_index=subtask_id,
                normalized_robot_name="robot1",
                real_robot_name="robot1",
                prompt=prompt,
                raw_output=raw_output,
                problem=problem,
                repair=repair_result,
            )
            results.append(result)
            self._write_text_artifact(
                f"05_problem_generation/prompts/subtask_{subtask_id:02d}_prompt.txt",
                prompt,
            )
            self._write_text_artifact(
                f"05_problem_generation/outputs/subtask_{subtask_id:02d}_problem.raw.txt",
                raw_output,
            )
            self._write_text_artifact(
                f"05_problem_generation/outputs/subtask_{subtask_id:02d}_problem.pddl",
                problem,
            )

        problems = [result.problem for result in results]
        self._write_json_artifact(
            generated_problem_files_artifact,
            [
                {"index": index, "content": content}
                for index, content in enumerate(problems, start=1)
            ],
        )
        self._record_artifact(
            "problem_files",
            "generated_problem_files",
            generated_problem_files_artifact,
        )
        self._write_problem_repair_manifest(results, replace_all=True)
        self._persist_manifest()
        return problems

    def _extract_pddl_problem_block(self, text: str) -> str:
        """Extract clean PDDL problem block from text.

        Args:
            text: Raw LLM output containing PDDL problem possibly with trailing text

        Returns:
            Extracted PDDL problem block, or original text if extraction fails
        """
        start_marker = "(define (problem"
        start_idx = text.find(start_marker)
        if start_idx == -1:
            return text

        depth = 0
        seen_open = False
        for idx in range(start_idx, len(text)):
            char = text[idx]
            if char == '(':
                depth += 1
                seen_open = True
            elif char == ')':
                depth -= 1
                if seen_open and depth == 0:
                    return text[start_idx:idx + 1].strip()

        return text

    def _domain_file_for_problem_domain(self, domain_name: str) -> Optional[str]:
        """Resolve a generated problem's domain without using robot allocation."""
        if not domain_name:
            return None
        if domain_name.casefold() == "allactionrobot":
            domain_path = str(self.config.allaction_domain_path())
            return domain_path if os.path.isfile(domain_path) else None
        domain_path = str(self.config.robot_domain_path(f"{domain_name}.pddl"))
        return domain_path if os.path.isfile(domain_path) else None

    def run_llmvalidator(self) -> List[Dict[str, Any]]:
        """Validate every full-capability Problem PDDL before planning it."""
        raw_problem_dir = self._get_raw_problem_file_path()
        if not raw_problem_dir or not os.path.isdir(raw_problem_dir):
            return []

        validation_records: List[Dict[str, Any]] = []
        validated_problem_files: List[str] = []
        problem_files = sorted(
            filename
            for filename in os.listdir(raw_problem_dir)
            if filename.endswith(".pddl")
        )
        for problem_file in problem_files:
            try:
                problem_full_path = os.path.join(raw_problem_dir, problem_file)
                domain_name = self.file_processor.extract_domain_name(problem_full_path)
                domain_file = self._domain_file_for_problem_domain(domain_name or "")
                if not domain_name or not domain_file:
                    validation_records.append(
                        {
                            "problem_file": problem_file,
                            "domain_name": domain_name,
                            "status": "skipped",
                            "error": "Problem domain could not be resolved",
                        }
                    )
                    continue
                domain_content = self.file_processor.read_file(domain_file)
                problem_content = self.file_processor.read_file(problem_full_path)
                prompt = (
                    "Domain Description:\n"
                    + domain_content
                    + "\n\nProblem Description:\n"
                    + problem_content
                    + "\n\nValidate the Problem PDDL against the domain. Check object "
                    "declarations, predicate/action compatibility, syntax, and balanced "
                    "parentheses. Return only the corrected complete Problem PDDL."
                )
                safe_name = self._sanitize_filename(problem_file[:-5])
                input_path = f"07_validate/inputs/{safe_name}_input.pddl"
                prompt_path = f"07_validate/prompts/{safe_name}_prompt.txt"
                raw_output_path = f"07_validate/outputs/{safe_name}_validated.raw.txt"
                validated_problem_path = (
                    f"07_validate/outputs/{safe_name}_validated.pddl"
                )
                self._write_text_artifact(input_path, problem_content)
                self._write_text_artifact(prompt_path, prompt)
                call_config = self.config.llm_call("llm_validator")
                _, raw_output = self.llm.query_model(
                    [
                        {
                            "role": "system",
                            "content": "You are a Robot PDDL problem expert.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    self.model,
                    frequency_penalty=call_config.get("frequency_penalty", 0.4),
                )
                validated_problem = self._extract_pddl_problem_block(raw_output)
                if "(define (problem" not in validated_problem.casefold():
                    raise ValidationError("Validator did not return a Problem PDDL block")
                validated_problem = self._force_problem_domain(
                    validated_problem,
                    "allactionrobot",
                )
                validated_problem = re.sub(
                    r"(?<![A-Za-z0-9_])robot\d+(?![A-Za-z0-9_])",
                    "robot1",
                    validated_problem,
                    flags=re.IGNORECASE,
                )
                self._write_text_artifact(raw_output_path, raw_output)
                validated_full_path = self._write_text_artifact(
                    validated_problem_path,
                    validated_problem,
                )
                if validated_full_path:
                    validated_problem_files.append(validated_full_path)
                validation_records.append(
                    {
                        "problem_file": problem_file,
                        "domain_name": "allactionrobot",
                        "domain_file": domain_file,
                        "input_path": input_path,
                        "prompt_path": prompt_path,
                        "raw_output_path": raw_output_path,
                        "validated_problem_path": validated_problem_path,
                        "status": "validated",
                    }
                )
            except Exception as exc:
                validation_records.append(
                    {
                        "problem_file": problem_file,
                        "status": "error",
                        "error": str(exc),
                    }
                )

        validation_manifest = "07_validate/validation_manifest.json"
        self._write_json_artifact(validation_manifest, validation_records)
        self._record_artifact("validate", "manifest", validation_manifest)
        self.current_task_manifest.setdefault("validate", {})[
            "validated_problem_files"
        ] = validated_problem_files
        self._persist_manifest()
        return validation_records

    def _key_object_evidence_for_subtask(self, subtask_id: int) -> List[Dict[str, Any]]:
        evidence_by_subtask = self._read_json_artifact(
            "05_problem_generation/key_object_pddl_state_evidence_by_subtask.json",
            {},
        )
        if not isinstance(evidence_by_subtask, dict):
            return []
        evidence = evidence_by_subtask.get(
            str(subtask_id),
            evidence_by_subtask.get(subtask_id, []),
        )
        return evidence if isinstance(evidence, list) else []

    def _audit_problem_before_planning(
        self,
        problem_path: str,
        subtask_id: int,
    ) -> Dict[str, Any]:
        problem = self.file_processor.read_file(problem_path)
        audit = audit_problem_initial_state(
            problem,
            self._key_object_evidence_for_subtask(subtask_id),
        )
        if audit.problem != problem:
            self.file_processor.write_file(problem_path, audit.problem)
        audit_record = {
            "subtask_id": subtask_id,
            "goal_literals": audit.goal_literals,
            "repairs": audit.repairs,
            "failure_reasons": audit.failure_reasons,
        }
        artifact = "05_problem_generation/problem_state_audit.json"
        existing = self._read_json_artifact(artifact, [])
        if not isinstance(existing, list):
            existing = []
        by_id = {
            int(item["subtask_id"]): item
            for item in existing
            if isinstance(item, dict) and str(item.get("subtask_id", "")).isdigit()
        }
        by_id[subtask_id] = audit_record
        self._write_json_artifact(artifact, [by_id[key] for key in sorted(by_id)])
        self._record_artifact("problem_generation", "problem_state_audit", artifact)
        return audit_record

    def run_allaction_planners(self) -> List[Dict[str, Any]]:
        """Plan validated problems with the full-capability PDDL domain."""
        validated_problem_dir = self._get_validated_problem_file_path()
        if not validated_problem_dir or not os.path.isdir(validated_problem_dir):
            return []
        plan_dir = self._get_plan_file_path()
        if not plan_dir:
            return []
        os.makedirs(plan_dir, exist_ok=True)
        planner_path = str(self.config.planner_executable)
        domain_file = str(self.config.allaction_domain_path())
        planner_records: List[Dict[str, Any]] = []
        plan_output_files: List[str] = []

        for problem_file in sorted(
            filename
            for filename in os.listdir(validated_problem_dir)
            if filename.endswith(".pddl")
        ):
            problem_full_path = os.path.join(validated_problem_dir, problem_file)
            subtask_id = self._subtask_id_from_filename(problem_file)
            if subtask_id is not None:
                self._audit_problem_before_planning(problem_full_path, subtask_id)
            safe_name = self._sanitize_filename(problem_file[:-5])
            output_file = os.path.join(plan_dir, f"{safe_name}_plan.txt")
            if os.path.isfile(output_file):
                os.unlink(output_file)
            command = [
                planner_path,
                "--plan-file",
                output_file,
                "--alias",
                str(self.config.get("planner", "alias", "seq-opt-lmcut")),
                domain_file,
                problem_full_path,
            ]
            started_at = time.time()
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=int(
                        self.config.get("planner", "timeout_seconds", 300)
                    ),
                )
                command_path = f"08_planner/commands/{safe_name}_command.txt"
                stdout_path = f"08_planner/stdout/{safe_name}_stdout.txt"
                stderr_path = f"08_planner/stderr/{safe_name}_stderr.txt"
                self._write_text_artifact(command_path, " ".join(command))
                self._write_text_artifact(stdout_path, result.stdout)
                self._write_text_artifact(stderr_path, result.stderr)
                if os.path.isfile(output_file):
                    plan_output_files.append(output_file)
                planner_records.append(
                    {
                        "problem_file": problem_file,
                        "problem_path": problem_full_path,
                        "domain_name": "allactionrobot",
                        "domain_file": domain_file,
                        "command_path": command_path,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                        "return_code": result.returncode,
                        "duration_seconds": round(time.time() - started_at, 3),
                        "compatibility_output": output_file,
                        **self._build_planner_status_fields(
                            output_file,
                            stdout_text=result.stdout,
                            stderr_text=result.stderr,
                            return_code=result.returncode,
                            status="completed",
                        ),
                    }
                )
            except subprocess.TimeoutExpired as exc:
                planner_records.append(
                    {
                        "problem_file": problem_file,
                        "problem_path": problem_full_path,
                        "domain_name": "allactionrobot",
                        "domain_file": domain_file,
                        "compatibility_output": output_file,
                        "error": str(exc),
                        **self._build_planner_status_fields(
                            output_file,
                            stderr_text=str(exc),
                            status="timeout",
                        ),
                    }
                )
            except Exception as exc:
                planner_records.append(
                    {
                        "problem_file": problem_file,
                        "problem_path": problem_full_path,
                        "domain_name": "allactionrobot",
                        "domain_file": domain_file,
                        "compatibility_output": output_file,
                        "error": str(exc),
                        **self._build_planner_status_fields(
                            output_file,
                            stderr_text=str(exc),
                            status="error",
                        ),
                    }
                )

        planner_manifest = self.config.artifact(
            "planner_manifest",
            "08_planner/planner_manifest.json",
        )
        self._write_json_artifact(planner_manifest, planner_records)
        self._record_artifact("planner", "manifest", planner_manifest)
        self.current_task_manifest.setdefault("planner", {})[
            "plan_output_files"
        ] = plan_output_files
        self._persist_manifest()
        return planner_records

    def _validate_and_plan(self) -> List[Dict[str, Any]]:
        """Validate full-capability problems and generate their plans."""
        try:
            self.run_llmvalidator()
            return self.run_allaction_planners()
        except Exception as exc:
            raise PDDLError(f"Error in validation and planning: {exc}") from exc

    def _plan_generated_problems(self, subtask_ids: Optional[Set[int]] = None) -> List[Dict[str, Any]]:
        """Plan generated problem files, optionally restricted to selected subtasks."""
        try:
            if subtask_ids is None:
                return self.run_planners()
            return self.run_planners(subtask_ids=subtask_ids)
        except Exception as e:
            raise PDDLError(f"Error planning generated problems: {str(e)}")

    def run_planners(self, subtask_ids: Optional[Set[int]] = None) -> List[Dict[str, Any]]:
        """Run PDDL planners on problem files."""
        try:
            planner_path = str(self.config.planner_executable)
            problem_file_path = self._get_raw_problem_file_path()
            if not problem_file_path or not os.path.exists(problem_file_path):
                print("no problem_file")
                return []
            plan_file_path = self._get_plan_file_path()
            os.makedirs(plan_file_path, exist_ok=True)
            problem_files = [
                f
                for f in os.listdir(problem_file_path)
                if f.endswith('.pddl')
                and (
                    subtask_ids is None
                    or self._subtask_id_from_filename(f) in subtask_ids
                )
            ]
            planner_records = []
            for problem_file in problem_files:
                domain_file = None
                output_file = None
                try:
                    problem_file_full = os.path.join(problem_file_path, problem_file)
                    subtask_id = self._subtask_id_from_filename(problem_file)
                    if subtask_id is not None:
                        self._audit_problem_before_planning(
                            problem_file_full,
                            subtask_id,
                        )
                    safe_name = self._sanitize_filename(problem_file.replace(".pddl", ""))
                    output_file = os.path.join(plan_file_path, f"{safe_name}_plan.txt")
                    if os.path.isfile(output_file):
                        os.unlink(output_file)
                    domain_name = self.file_processor.extract_domain_name(problem_file_full)
                    if not domain_name:
                        print(f"No domain specified in {problem_file}")
                        planner_records.append({
                            "problem_file": problem_file,
                            "problem_path": problem_file_full,
                            "domain_file": None,
                            "compatibility_output": output_file,
                            **self._build_planner_status_fields(
                                output_file,
                                stderr_text=f"No domain specified in {problem_file}",
                                status="error",
                            ),
                        })
                        continue

                    domain_file = self.file_processor.find_domain_file(domain_name)
                    if not domain_file:
                        print(f"No domain file found for domain {domain_name}")
                        planner_records.append({
                            "problem_file": problem_file,
                            "problem_path": problem_file_full,
                            "domain_file": None,
                            "compatibility_output": output_file,
                            **self._build_planner_status_fields(
                                output_file,
                                stderr_text=f"No domain file found for domain {domain_name}",
                                status="error",
                            ),
                        })
                        continue
                    
                    command = [
                        planner_path,
                        "--plan-file",
                        output_file,
                        "--alias",
                        str(self.config.get("planner", "alias", "seq-opt-lmcut")),
                        domain_file,
                        problem_file_full
                    ]
                    started_at = time.time()

                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=int(self.config.get("planner", "timeout_seconds", 300))
                    )
                    
                    command_path = f"08_planner/commands/{safe_name}_command.txt"
                    stdout_path = f"08_planner/stdout/{safe_name}_stdout.txt"
                    stderr_path = f"08_planner/stderr/{safe_name}_stderr.txt"
                    self._write_text_artifact(command_path, " ".join(command))
                    self._write_text_artifact(stdout_path, result.stdout)
                    self._write_text_artifact(stderr_path, result.stderr)
                    planner_records.append({
                        "problem_file": problem_file,
                        "problem_path": problem_file_full,
                        "domain_file": domain_file,
                        "command_path": command_path,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                        "return_code": result.returncode,
                        "duration_seconds": round(time.time() - started_at, 3),
                        "compatibility_output": output_file,
                        **self._build_planner_status_fields(
                            output_file,
                            stdout_text=result.stdout,
                            stderr_text=result.stderr,
                            return_code=result.returncode,
                            status="completed",
                        ),
                    })

                    if result.stderr:
                        print(f"Warnings/Errors for {problem_file}:", result.stderr)
                        
                except subprocess.TimeoutExpired as e:
                    print(f"Planner timed out for {problem_file}")
                    planner_records.append({
                        "problem_file": problem_file,
                        "problem_path": problem_file_full,
                        "domain_file": domain_file,
                        "compatibility_output": output_file,
                        "error": str(e),
                        **self._build_planner_status_fields(
                            output_file,
                            stderr_text=str(e),
                            status="timeout",
                        ),
                    })
                except Exception as e:
                    print(f"Error processing file {problem_file}: {str(e)}")
                    planner_records.append({
                        "problem_file": problem_file,
                        "problem_path": problem_file_full,
                        "domain_file": domain_file,
                        "compatibility_output": output_file,
                        "error": str(e),
                        **self._build_planner_status_fields(
                            output_file,
                            stderr_text=str(e),
                            status="error",
                        ),
                    })
                    continue

            planner_manifest_path = self.config.artifact("planner_manifest", "08_planner/planner_manifest.json")
            existing_records = self._read_json_artifact(planner_manifest_path, [])
            if not isinstance(existing_records, list) or subtask_ids is None:
                existing_records = []
            merged_records = self._merge_subtask_records(existing_records, planner_records)
            self._write_json_artifact(planner_manifest_path, merged_records)
            self._record_artifact("planner", "manifest", planner_manifest_path)
            self._persist_manifest()
            return planner_records
                    
        except Exception as e:
            print(f"Error in run_planners: {str(e)}")
            raise

    def _combine_all_plans(self, decomposed_plan:str, sequence_operations:List[str]) -> str:
        """Combine all generated plan files into a single plan.
 
        """
        plan_file_path = self._get_plan_file_path()
        plan_files = [os.path.join(plan_file_path, f) for f in os.listdir(plan_file_path) if f.endswith('_plan.txt')]
        prompt = ""
        # Add plans from files if they exist
        if plan_files:
            for idx, filepath in enumerate(plan_files):
                content = self.file_processor.read_file(filepath)
                plan = self.file_processor.extract_plan_from_planfile(content)
                if plan:  # Only add non-empty plans
                    prompt += f"\nPlan {idx + 1}:\n{plan}\n"
        
        # Add allocation examination and initial plan
        prompt += "\nallocation examination\n"
        prompt += "\n".join(sequence_operations)
        prompt += "\ninitial plan examination\n"
        prompt += decomposed_plan
        
        prompt += ("\nyou are robot allocation expert, Your task is, based on inital plan examination "
                  "and allocation examination correct the subplans. Then based on your understanding "
                  "merge the subtasks together by using timed durative actions format, where parallel "
                  "tasks are performed at the same time. IMPORTANT: all 'variablelocation' should be "
                  "corrected to variable itself, since variable itself includes location. and result "
                  "must be in PDDL plan format.")
        combine_prompt_artifact = self.config.artifact("combine_prompt", "09_combine/01_combine_prompt.txt")
        combine_output_artifact = self.config.artifact("combine_output", "09_combine/02_combined_plan.txt")
        self._write_text_artifact(combine_prompt_artifact, prompt)
        self._record_artifact("combine", "prompt", combine_prompt_artifact)
        
        messages = [{"role": "user", "content": prompt}]
        call_config = self.config.llm_call("combine")
        _, text = self.llm.query_model(
            messages,
            self.model,
            frequency_penalty=call_config.get("frequency_penalty", 0.0),
        )
        self._write_text_artifact(combine_output_artifact, text)
        self._record_artifact("combine", "output", combine_output_artifact)
        self._persist_manifest()
        
        return text

    def _match_references_for_plan(self, plan: str, objects_ai: str) -> str:
        """Match and correct variable locations in plan."""
        prompt = (
            f"{objects_ai}\n"
            "IMPORTANT: Your TASK is based on the provided pddl plan provided in the passage below "
            "and the object list above, modify and only modify the plan so that all 'variablelocation' "
            "should be corrected to variable itself, since variable itself includes location. and similarly variable names should be corrected to variable itself. "
            "IMPORTANT: the only parenthesis usage should be for the correct PDDL plan, no exception.\n\n"
            f"{plan}"
        )
        final_match_prompt_artifact = self.config.artifact("final_match_prompt", "10_final_match/01_match_prompt.txt")
        final_match_output_artifact = self.config.artifact("final_match_output", "10_final_match/02_final_plan.txt")
        self._write_text_artifact(final_match_prompt_artifact, prompt)
        self._record_artifact("final_match", "prompt", final_match_prompt_artifact)
        
        messages = [{"role": "user", "content": prompt}]
        call_config = self.config.llm_call("final_match")
        _, text = self.llm.query_model(
            messages,
            self.model,
            frequency_penalty=call_config.get("frequency_penalty", 0.0),
        )
        self._write_text_artifact(final_match_output_artifact, text)
        self._record_artifact("final_match", "output", final_match_output_artifact)
        self._persist_manifest()
        
        return text

def build_robot_team(robot_ids: List[int]) -> List[dict]:
    """Build a task-local robot team definition from dataset robot ids."""
    task_robots: List[dict] = []
    for index, robot_id in enumerate(robot_ids):
        robot_def = copy.deepcopy(robots.robots[robot_id - 1])
        robot_def["name"] = f"robot{index + 1}"
        task_robots.append(robot_def)
    return task_robots


def build_robot_domain_name_map(robot_ids: List[int]) -> Dict[str, str]:
    """Map task-local robot names to the real robot domain names."""
    return {
        f"robot{index + 1}": f"robot{robot_id}"
        for index, robot_id in enumerate(robot_ids)
    }


def run_single_floor_plan_task(
    base_path: str,
    model: str,
    floor_plan: str,
    task_record: Dict[str, Any],
    prompt_decompse_set: str = "pddl_train_task_decomposesep",
    prompt_allocation_set: str = "pddl_train_task_allocationsep",
    objects_ai: Optional[str] = None,
    config: Optional[RunConfig] = None,
    test_set: str = "final_test",
    task_index: Optional[int] = None,
    allocate_model: Optional[str] = None,
) -> TaskProcessingResult:
    """Run a single dataset record as an isolated task-safe execution unit."""
    run_config = config or load_run_config(base_path)
    task_manager = TaskManager(
        base_path=base_path,
        model=model,
        prompt_decompse_set=prompt_decompse_set,
        prompt_allocation_set=prompt_allocation_set,
        config=run_config,
        test_set=test_set,
        floor_plan=floor_plan,
        allocate_model=allocate_model,
    )
    floor_plan_id = PDDLUtils.extract_floor_plan_number(floor_plan)
    objects_description = objects_ai or f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(int(floor_plan_id), run_config)}"
    task = task_record["task"]
    robot_ids = task_record["robot list"]
    robot_team = build_robot_team(robot_ids)
    robot_domain_name_map = build_robot_domain_name_map(robot_ids)

    task_manager.process_tasks(
        test_tasks=[task],
        available_robots=[robot_team],
        objects_ai=objects_description,
        robot_domain_name_maps=[robot_domain_name_map],
        task_indices=[task_index] if task_index is not None else None,
    )

    llm_token_usage = empty_llm_token_usage()
    if task_manager.current_task_run_dir:
        llm_calls_path = (
            Path(task_manager.current_task_run_dir)
            / task_manager.config.artifact("llm_calls", "00_llm/llm_calls.jsonl")
        )
        llm_token_usage = summarize_llm_token_usage(llm_calls_path)
        task_manager.current_task_manifest["llm_token_usage"] = llm_token_usage
        task_manager._persist_manifest()

    return {"task_run_dir": task_manager.current_task_run_dir, 
            "tc": task_manager.tc,
            "total": task_manager.total,
            "llm_token_usage": llm_token_usage}
    

def load_dataset_records(test_file: str) -> List[Dict[str, Any]]:
    """Load raw dataset records from a JSONL file."""
    records: List[Dict[str, Any]] = []
    try:
        with open(test_file, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
    except FileNotFoundError:
        raise PDDLError(f"Test file not found: {test_file}")
    except json.JSONDecodeError as exc:
        raise PDDLError(f"Invalid JSON in test file {test_file}: {str(exc)}")

    return records


def validate_dataset_file(run_config: RunConfig, test_set: str, floor_plan: Union[int, str]) -> Path:
    test_file = run_config.dataset_file(test_set, floor_plan)
    if test_file.exists():
        return test_file

    available = run_config.available_test_sets()
    available_text = ", ".join(available) if available else "none"
    raise PDDLError(
        f"Test file not found: {test_file}. "
        f"Available test sets: {available_text}"
    )


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    available_models = get_available_models()
    parser.add_argument(
        "--floor-plan", 
        type=str, 
        required=True,
        help="Floor plan dataset identifier to run"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek-chat",
        choices=available_models,
    )
    parser.add_argument(
        "--allocate-model",
        type=str,
        default=None,
        choices=available_models,
        help="Model to use for task allocation; defaults to --model.",
    )
    parser.add_argument(
        "--prompt-decompse-set",
        type=str,
        default="pddl_train_task_decomposesep",
        choices=['pddl_train_task_decomposesep']
    )
    parser.add_argument(
        "--prompt-allocation-set",
        type=str,
        default="pddl_train_task_allocationsep",
        choices=['pddl_train_task_allocationsep']
    )
    parser.add_argument(
        "--test-set",
        type=str,
        default="final_test"
    )
    parser.add_argument(
        "--task-index",
        type=int,
        default=0,
        help="Zero-based task index to run from the selected floor plan dataset."
    )
    decompose_rag_group = parser.add_mutually_exclusive_group()
    decompose_rag_group.add_argument(
        "--decompose-rag",
        dest="decompose_rag",
        action="store_true",
        help="Enable task decomposition RAG few-shot examples.",
    )
    decompose_rag_group.add_argument(
        "--no-decompose-rag",
        dest="decompose_rag",
        action="store_false",
        help="Disable task decomposition RAG and use the static decomposition prompt examples (default).",
    )
    allocate_rag_group = parser.add_mutually_exclusive_group()
    allocate_rag_group.add_argument(
        "--allocate-rag",
        dest="allocate_rag",
        action="store_true",
        help="Enable task allocation RAG few-shot examples.",
    )
    allocate_rag_group.add_argument(
        "--no-allocate-rag",
        dest="allocate_rag",
        action="store_false",
        help="Disable task allocation RAG and use the static allocation prompt examples (default).",
    )
    problem_rag_group = parser.add_mutually_exclusive_group()
    problem_rag_group.add_argument(
        "--problem-rag",
        dest="problem_rag",
        action="store_true",
        help="Enable PDDL problem-generation RAG few-shot examples.",
    )
    problem_rag_group.add_argument(
        "--no-problem-rag",
        dest="problem_rag",
        action="store_false",
        help="Disable PDDL problem-generation RAG and use the static problem prompt examples (default).",
    )
    problem_repair_group = parser.add_mutually_exclusive_group()
    problem_repair_group.add_argument(
        "--problem-repair",
        dest="problem_repair",
        action="store_true",
        help="Enable deterministic local repair of generated PDDL problems.",
    )
    problem_repair_group.add_argument(
        "--no-problem-repair",
        dest="problem_repair",
        action="store_false",
        help="Disable deterministic local repair of generated PDDL problems (default).",
    )
    parser.set_defaults(
        decompose_rag=False,
        allocate_rag=False,
        problem_rag=False,
        problem_repair=None,
    )

    return parser.parse_args(argv)

def main():
    """Main execution function."""
    try:
        # Parse arguments
        args = parse_arguments()
        base_path = str(_repo_root())
        run_config = load_run_config(base_path)
        apply_decompose_rag_cli_override(run_config, args.decompose_rag)
        apply_allocate_rag_cli_override(run_config, args.allocate_rag)
        apply_problem_rag_cli_override(run_config, args.problem_rag)
        apply_problem_repair_cli_override(
            run_config,
            getattr(args, "problem_repair", None),
        )
        # Initialize task manager
        task_manager = TaskManager(
            base_path=base_path,
            model=args.model,
            allocate_model=args.allocate_model,
            prompt_decompse_set=args.prompt_decompse_set,
            prompt_allocation_set=args.prompt_allocation_set,
            config=run_config,
        )
        
        # Dataset workflow: run a single task selected by task index
        test_file = validate_dataset_file(run_config, args.test_set, args.floor_plan)
        task_records = load_dataset_records(str(test_file))
        if not task_records:
            raise PDDLError(f"No tasks found in dataset: {test_file}")
        if args.task_index < 0 or args.task_index >= len(task_records):
            raise PDDLError(
                f"Task index {args.task_index} is out of range for {test_file}. "
                f"Valid range: 0 to {len(task_records) - 1}"
            )

        selected_record = task_records[args.task_index]
        selected_task = selected_record.get("task", f"task_{args.task_index}")

        print(f"\n----Test set tasks----\nTotal: {len(task_records)} tasks\n")
        print(f"Selected task index: {args.task_index}")
        print(f"Selected task: {selected_task}\n")
        
        # Get AI2thor objects 
        scene_floor_plan = int(PDDLUtils.extract_floor_plan_number(args.floor_plan))
        objects_ai = f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(scene_floor_plan, run_config)}"
        run_single_floor_plan_task(
            base_path=base_path,
            model=args.model,
            allocate_model=args.allocate_model,
            floor_plan=args.floor_plan,
            task_record=selected_record,
            prompt_decompse_set=args.prompt_decompse_set,
            prompt_allocation_set=args.prompt_allocation_set,
            objects_ai=objects_ai,
            config=run_config,
            test_set=args.test_set,
            task_index=args.task_index,
        )
        
    except Exception as e:
        print(f"Error in main execution: {str(e)}")
        print(f"Full error: {str(e.__class__.__name__)}: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()





























#####



    
