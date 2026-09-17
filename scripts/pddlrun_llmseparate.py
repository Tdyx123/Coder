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
from llm_client import extract_finish_reason, extract_usage
from decomposition_validation import build_retry_prompt, validate_decomposition
from file_processor import FileProcessor, PDDLError
from llm_handler import LLMError, LLMHandler
from llm_logger import get_llm_logger
from pddl_rag import PDDLRagError, PDDLRagRetriever, PDDLRagTimeoutError
from pddl_object_selection import select_context_objects, merge_object_contexts
from pddl_problem_repair import (
    ProblemRepairResult,
    repair_problem_pddl,
    repair_unknown_object_types,
)
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
    apply_feedback_cli_override,
    apply_problem_repair_cli_override,
    apply_problem_rag_cli_override,
    apply_val_feedback_cli_override,
    load_run_config as _load_run_config,
    normalize_floor_plan,
)
from special_task_skills import SPECIAL_TASK_SKILL_PROMPT_RULE

import sys
sys.path.append(".")

import resources.actions as actions
import resources.robots as robots


ALLOCATION_FALLBACK_RULES_PATH = Path(__file__).with_name("allocation_fallback_rules.json")


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
        self.feedback_enabled = _config_bool(self.config.get("feedback", "enabled", False), False)
        self.feedback_max_retries = max(0, int(self.config.get("feedback", "max_retries", 2)))
        self.feedback_max_prompt_chars = max(500, int(self.config.get("feedback", "max_prompt_chars", 4000)))
        self.val_feedback_enabled = _config_bool(self.config.get("val_feedback", "enabled", False), False)
        self.val_feedback_max_retries = max(0, int(self.config.get("val_feedback", "max_retries", 2)))
        self.val_feedback_max_prompt_chars = max(
            500,
            int(self.config.get("val_feedback", "max_prompt_chars", 4000)),
        )
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

    def _clean_feedback_attempt_outputs(self) -> None:
        """Remove downstream artifacts that must not leak across feedback attempts."""
        if not self.current_task_run_dir:
            return

        cleanup_paths = [
            os.path.join(self.current_task_run_dir, "05_problem_generation", "prompts"),
            os.path.join(self.current_task_run_dir, "05_problem_generation", "outputs"),
            os.path.join(self.current_task_run_dir, "08_planner"),
        ]
        for path in cleanup_paths:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.isfile(path):
                os.unlink(path)

    def _clean_subtask_attempt_outputs(self, subtask_ids: Set[int]) -> None:
        """Remove stale canonical artifacts for selected VAL-feedback subtasks."""
        if not self.current_task_run_dir:
            return

        roots = (
            "05_problem_generation/prompts",
            "05_problem_generation/outputs",
            "08_planner/commands",
            "08_planner/stdout",
            "08_planner/stderr",
            "08_planner/outputs",
        )
        prefixes = tuple(f"subtask_{subtask_id:02d}_" for subtask_id in sorted(subtask_ids))
        for relative_root in roots:
            root = Path(self.current_task_run_dir) / relative_root
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if path.is_file() and path.name.startswith(prefixes):
                    path.unlink()

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
        basic_success = (
            record.get("status") == "completed"
            and bool(record.get("plan_generated"))
            and not bool(record.get("has_planner_error"))
            and record.get("return_code") in (None, 0)
        )
        if not basic_success:
            return False
        plan_path = record.get("compatibility_output")
        if not plan_path or not os.path.isfile(str(plan_path)):
            return True
        plan_text = self.file_processor.read_file(str(plan_path))
        if plan_has_actions(plan_text):
            return True
        if self.val_feedback_enabled:
            return True
        subtask_id = self._subtask_id_from_filename(str(record.get("problem_file", "")))
        if subtask_id is None:
            return False
        noop_records = self._read_json_artifact("08_planner/noop_subtasks.json", [])
        return any(
            isinstance(noop, dict)
            and noop.get("subtask_id") == subtask_id
            and bool(noop.get("verified"))
            for noop in (noop_records if isinstance(noop_records, list) else [])
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
            feedback_reason = "No planner plan was generated."
        elif has_planner_error:
            feedback_reason = "Planner produced a plan but reported an error."
        else:
            feedback_reason = ""

        return {
            "status": status,
            "plan_exists": plan_exists,
            "plan_size_bytes": plan_size_bytes,
            "plan_generated": plan_generated,
            "has_planner_error": has_planner_error,
            "feedback_reason": feedback_reason,
            "planner_output_excerpt": self._summarize_planner_output(stdout_text, stderr_text),
        }

    def _build_planner_feedback(
        self,
        planner_records: Sequence[Dict[str, Any]],
        expected_subtask_count: int,
        robot_assignments: Optional[Dict[int, int]] = None,
        expected_subtask_ids: Optional[Iterable[int]] = None,
    ) -> Dict[str, Any]:
        records_by_subtask: Dict[int, Dict[str, Any]] = {}
        for record in planner_records:
            subtask_id = self._subtask_id_from_filename(str(record.get("problem_file", "")))
            if subtask_id is not None:
                records_by_subtask[subtask_id] = record

        failed_sections: Dict[int, str] = {}
        subtask_feedback: Dict[int, str] = {}
        subtask_ids = (
            sorted({int(subtask_id) for subtask_id in expected_subtask_ids})
            if expected_subtask_ids is not None
            else range(1, expected_subtask_count + 1)
        )
        for subtask_id in subtask_ids:
            record = records_by_subtask.get(subtask_id)
            if record and self._planner_record_success(record):
                continue

            robot_num = robot_assignments.get(subtask_id) if robot_assignments else None
            if robot_num is None:
                feedback_line = "No valid previous robot assignment was found. Please assign a capable robot for this subtask."
            else:
                feedback_line = (
                    f"The previously assigned robot was Robot {robot_num}. "
                    "This robot may be unable to complete this subtask."
                )
            subtask_feedback[subtask_id] = feedback_line
            failed_sections[subtask_id] = f"- Subtask {subtask_id}: {feedback_line}"

        succeeded = not failed_sections
        if succeeded:
            return {
                "succeeded": True,
                "feedback_text": "",
                "failed_subtask_ids": [],
                "planner_feedback_by_subtask": {},
            }

        parts = [
            "# PLANNER FEEDBACK FROM PREVIOUS ATTEMPT",
            "",
            "Failed subtasks:",
        ]
        parts.extend(failed_sections[subtask_id] for subtask_id in sorted(failed_sections))
        parts.extend([
            "",
            "Please reconsider the robot assignment for the failed subtasks.",
        ])
        feedback_text = "\n".join(parts).strip()
        if len(feedback_text) > self.feedback_max_prompt_chars:
            feedback_text = feedback_text[:self.feedback_max_prompt_chars].rstrip() + "\n...[truncated]"

        return {
            "succeeded": False,
            "feedback_text": feedback_text,
            "failed_subtask_ids": sorted(failed_sections),
            "planner_feedback_by_subtask": subtask_feedback,
        }

    @staticmethod
    def _val_output_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _val_output_has_domain_error(output_text: str) -> bool:
        lowered = output_text.lower()
        return any(
            marker in lowered
            for marker in (
                "problem in domain definition",
                "error in domain definition",
                "errors in domain definition",
                "failed to parse domain",
                "cannot parse domain",
            )
        )

    def _resolve_val_executable(self) -> Optional[str]:
        configured = str(self.config.get("val", "executable", "Validate") or "Validate").strip()
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return str(candidate) if candidate.is_file() else None
        if candidate.parent != Path("."):
            resolved = self.config.resolve_path(candidate)
            return str(resolved) if resolved.is_file() else None
        return shutil.which(configured)

    def _val_arguments(self) -> List[str]:
        configured = self.config.get("val", "arguments", ["-v", "-e"])
        if not isinstance(configured, list):
            return ["-v", "-e"]
        return [str(argument) for argument in configured]

    def _classify_val_result(
        self,
        return_code: Optional[int],
        stdout_text: str,
        stderr_text: str,
        infrastructure_error: Optional[str] = None,
    ) -> Tuple[str, bool]:
        combined = "\n".join(part for part in (stdout_text, stderr_text) if part)
        if infrastructure_error:
            return "infrastructure_error", False
        if self._val_output_has_domain_error(combined):
            return "domain_error", False
        if return_code == 0 and re.search(r"\bPlan\s+valid\b", combined, re.IGNORECASE):
            return "valid", True
        if return_code is None or return_code == 0 or return_code < 0 or not combined.strip():
            return "infrastructure_error", False
        return "validation_error", False

    def _format_val_feedback(self, record: Dict[str, Any]) -> str:
        output_parts = []
        if record.get("stdout"):
            output_parts.append(f"stdout:\n{record['stdout']}")
        if record.get("stderr"):
            output_parts.append(f"stderr:\n{record['stderr']}")
        if record.get("infrastructure_error") and not output_parts:
            output_parts.append(str(record["infrastructure_error"]))
        output_text = "\n\n".join(output_parts).strip() or "VAL returned no diagnostic output."
        feedback = (
            f"VAL status: {record.get('status', 'unknown')}\n"
            f"VAL exit code: {record.get('return_code')}\n"
            f"VAL output:\n{output_text}"
        )
        if len(feedback) > self.val_feedback_max_prompt_chars:
            feedback = feedback[:self.val_feedback_max_prompt_chars].rstrip() + "\n...[truncated]"
        return feedback

    def _record_val_attempt(
        self,
        allocation_attempt: int,
        val_attempt: int,
        records: Sequence[Dict[str, Any]],
    ) -> None:
        val_manifest_path = self.config.artifact("val_manifest", "08_val/val_manifest.json")
        manifest = self._read_json_artifact(
            val_manifest_path,
            {"enabled": True, "attempts": [], "latest_by_subtask": {}},
        )
        if not isinstance(manifest, dict):
            manifest = {"enabled": True, "attempts": [], "latest_by_subtask": {}}
        attempts = manifest.setdefault("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
            manifest["attempts"] = attempts
        attempt_target_ids = sorted(
            int(record["subtask_id"])
            for record in records
            if record.get("subtask_id") is not None
        )
        attempts.append({
            "allocation_attempt": allocation_attempt,
            "val_attempt": val_attempt,
            "targeted_subtask_ids": attempt_target_ids,
            "records": list(records),
        })
        latest = manifest.setdefault("latest_by_subtask", {})
        if not isinstance(latest, dict):
            latest = {}
            manifest["latest_by_subtask"] = latest
        for record in records:
            if record.get("subtask_id") is not None:
                latest[str(record["subtask_id"])] = dict(record)
        existing_target_ids = manifest.get("targeted_subtask_ids", [])
        if not isinstance(existing_target_ids, list):
            existing_target_ids = []
        targeted_subtask_ids = sorted({
            *(int(subtask_id) for subtask_id in existing_target_ids),
            *attempt_target_ids,
        })
        manifest["targeted_subtask_ids"] = targeted_subtask_ids
        manifest["passed"] = bool(targeted_subtask_ids) and all(
            isinstance(latest.get(str(subtask_id)), dict)
            and bool(latest[str(subtask_id)].get("valid"))
            for subtask_id in targeted_subtask_ids
        )
        manifest["enabled"] = True
        self._write_json_artifact(val_manifest_path, manifest)
        self._record_artifact("val", "manifest", val_manifest_path)

        feedback_manifest = self.current_task_manifest.setdefault("val_feedback", {})
        feedback_manifest["enabled"] = True
        feedback_manifest["max_retries"] = self.val_feedback_max_retries
        feedback_attempts = feedback_manifest.setdefault("attempts", [])
        feedback_attempts.append({
            "allocation_attempt": allocation_attempt,
            "val_attempt": val_attempt,
            "targeted_subtask_ids": sorted(
                int(record["subtask_id"])
                for record in records
                if record.get("subtask_id") is not None
            ),
            "valid_subtask_ids": sorted(
                int(record["subtask_id"])
                for record in records
                if record.get("valid") and record.get("subtask_id") is not None
            ),
            "failed_subtask_ids": sorted(
                int(record["subtask_id"])
                for record in records
                if not record.get("valid") and record.get("subtask_id") is not None
            ),
            "statuses": {
                str(record["subtask_id"]): record.get("status")
                for record in records
                if record.get("subtask_id") is not None
            },
        })
        self._persist_manifest()

    def _set_val_feedback_status(self, status: str) -> None:
        if not isinstance(self.current_task_manifest, dict):
            return
        feedback_manifest = self.current_task_manifest.setdefault("val_feedback", {})
        feedback_manifest["enabled"] = True
        feedback_manifest["max_retries"] = self.val_feedback_max_retries
        feedback_manifest["status"] = status
        self._persist_manifest()

    def run_val_validations(
        self,
        planner_records: Sequence[Dict[str, Any]],
        allocation_attempt: int = 1,
        val_attempt: int = 1,
    ) -> List[Dict[str, Any]]:
        """Run VAL for planner records and persist full per-attempt diagnostics."""
        executable = self._resolve_val_executable()
        arguments = self._val_arguments()
        timeout_seconds = max(1, int(self.config.get("val", "timeout_seconds", 60)))
        problem_dir = self._get_raw_problem_file_path()
        records: List[Dict[str, Any]] = []

        for planner_record in planner_records:
            problem_file = str(planner_record.get("problem_file") or "")
            subtask_id = self._subtask_id_from_filename(problem_file)
            safe_name = self._sanitize_filename(problem_file.replace(".pddl", "") or "problem")
            domain_file = str(planner_record.get("domain_file") or "")
            problem_path = (
                os.path.join(problem_dir, problem_file)
                if problem_dir and problem_file
                else ""
            )
            plan_file = str(planner_record.get("compatibility_output") or "")
            base_artifact = (
                f"08_val/allocation_attempt_{allocation_attempt:02d}/"
                f"val_attempt_{val_attempt:02d}"
            )
            command_path = f"{base_artifact}/commands/{safe_name}_command.txt"
            stdout_path = f"{base_artifact}/stdout/{safe_name}_stdout.txt"
            stderr_path = f"{base_artifact}/stderr/{safe_name}_stderr.txt"
            command: List[str] = []
            stdout_text = ""
            stderr_text = ""
            return_code: Optional[int] = None
            infrastructure_error: Optional[str] = None
            started_at = time.time()

            missing_inputs = [
                label
                for label, path in (
                    ("domain", domain_file),
                    ("problem", problem_path),
                    ("plan", plan_file),
                )
                if not path or not os.path.isfile(path)
            ]
            if executable is None:
                infrastructure_error = "VAL executable was not found. Configure val.executable or PATH."
            elif missing_inputs:
                infrastructure_error = "Missing VAL input file(s): " + ", ".join(missing_inputs)
            else:
                command = [executable, *arguments, domain_file, problem_path, plan_file]
                try:
                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout_seconds,
                    )
                    return_code = result.returncode
                    stdout_text = result.stdout or ""
                    stderr_text = result.stderr or ""
                except subprocess.TimeoutExpired as exc:
                    stdout_text = self._val_output_text(exc.stdout)
                    stderr_text = self._val_output_text(exc.stderr)
                    infrastructure_error = f"VAL timed out after {timeout_seconds} seconds."
                except OSError as exc:
                    infrastructure_error = f"Unable to execute VAL: {exc}"

            if infrastructure_error:
                stderr_text = "\n".join(
                    part for part in (stderr_text, infrastructure_error) if part
                )
            status, valid = self._classify_val_result(
                return_code,
                stdout_text,
                stderr_text,
                infrastructure_error=infrastructure_error,
            )
            self._write_text_artifact(command_path, " ".join(command))
            self._write_text_artifact(stdout_path, stdout_text)
            self._write_text_artifact(stderr_path, stderr_text)
            record = {
                "subtask_id": subtask_id,
                "problem_file": problem_file,
                "domain_file": domain_file or None,
                "problem_path": problem_path or None,
                "plan_file": plan_file or None,
                "command_path": command_path,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "return_code": return_code,
                "duration_seconds": round(time.time() - started_at, 3),
                "status": status,
                "valid": valid,
                "infrastructure_error": infrastructure_error,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }
            record["feedback"] = self._format_val_feedback(record)
            records.append(record)

        self._record_val_attempt(allocation_attempt, val_attempt, records)
        return records

    def _run_val_feedback_loop(
        self,
        subtasks: List[str],
        robot_assignments: Dict[int, int],
        objects_ai: str,
        key_object_pddl_states: Optional[List[Dict[str, Any]]],
        key_object_pddl_states_by_subtask: Optional[Dict[int, List[Dict[str, Any]]]],
        planner_records: Sequence[Dict[str, Any]],
        allocation_attempt: int,
    ) -> Dict[str, Any]:
        """Validate plans with VAL and regenerate only repairable failed subtasks."""
        planner_by_subtask = {
            subtask_id: dict(record)
            for record in planner_records
            for subtask_id in [self._subtask_id_from_filename(str(record.get("problem_file", "")))]
            if subtask_id is not None
        }
        target_ids = set(planner_by_subtask)
        val_attempt = 1

        while True:
            target_planner_records = [
                planner_by_subtask[subtask_id]
                for subtask_id in sorted(target_ids)
                if subtask_id in planner_by_subtask
            ]
            val_records = self.run_val_validations(
                target_planner_records,
                allocation_attempt=allocation_attempt,
                val_attempt=val_attempt,
            )
            failed_records = [record for record in val_records if not record.get("valid")]
            if not failed_records:
                self._set_val_feedback_status("passed")
                return {
                    "succeeded": True,
                    "failure_source": None,
                    "feedback_text": "",
                    "failed_subtask_ids": [],
                    "planner_feedback_by_subtask": {},
                    "planner_records": [planner_by_subtask[key] for key in sorted(planner_by_subtask)],
                    "val_records": val_records,
                }

            if any(record.get("status") == "domain_error" for record in failed_records):
                self._set_val_feedback_status("domain_error")
                return {
                    "succeeded": False,
                    "failure_source": "val_domain",
                    "feedback_text": "",
                    "failed_subtask_ids": sorted(
                        record["subtask_id"]
                        for record in failed_records
                        if record.get("subtask_id") is not None
                    ),
                    "planner_feedback_by_subtask": {},
                    "planner_records": [planner_by_subtask[key] for key in sorted(planner_by_subtask)],
                    "val_records": val_records,
                }

            if any(record.get("status") == "infrastructure_error" for record in failed_records):
                self._set_val_feedback_status("infrastructure_error")
                return {
                    "succeeded": False,
                    "failure_source": "val_infrastructure",
                    "feedback_text": "",
                    "failed_subtask_ids": sorted(
                        record["subtask_id"]
                        for record in failed_records
                        if record.get("subtask_id") is not None
                    ),
                    "planner_feedback_by_subtask": {},
                    "planner_records": [planner_by_subtask[key] for key in sorted(planner_by_subtask)],
                    "val_records": val_records,
                }

            retry_ids = {
                int(record["subtask_id"])
                for record in failed_records
                if record.get("subtask_id") is not None
            }
            if val_attempt > self.val_feedback_max_retries:
                self._set_val_feedback_status("retry_exhausted")
                return {
                    "succeeded": False,
                    "failure_source": "val_validation",
                    "feedback_text": "",
                    "failed_subtask_ids": sorted(retry_ids),
                    "planner_feedback_by_subtask": {},
                    "planner_records": [planner_by_subtask[key] for key in sorted(planner_by_subtask)],
                    "val_records": val_records,
                }

            val_feedback_by_subtask = {
                int(record["subtask_id"]): str(record.get("feedback") or "")
                for record in failed_records
                if record.get("subtask_id") is not None
            }
            print(
                "Retrying PDDL problem generation with VAL feedback for subtasks "
                f"{sorted(retry_ids)} (VAL attempt {val_attempt + 1}/"
                f"{self.val_feedback_max_retries + 1})"
            )
            self._clean_subtask_attempt_outputs(retry_ids)
            self._generate_problem_files(
                subtasks,
                robot_assignments,
                objects_ai,
                key_object_pddl_states=key_object_pddl_states,
                key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
                val_feedback_by_subtask=val_feedback_by_subtask,
                subtask_ids=retry_ids,
            )
            updated_planner_records = self._plan_generated_problems(subtask_ids=retry_ids)
            for record in updated_planner_records:
                subtask_id = self._subtask_id_from_filename(str(record.get("problem_file", "")))
                if subtask_id is not None:
                    planner_by_subtask[subtask_id] = dict(record)

            merged_planner_records = [
                planner_by_subtask[key]
                for key in sorted(planner_by_subtask)
            ]
            planner_feedback = self._build_planner_feedback(
                updated_planner_records,
                len(subtasks),
                robot_assignments,
                expected_subtask_ids=retry_ids,
            )
            if not planner_feedback["succeeded"]:
                self._set_val_feedback_status("planner_failed")
                return {
                    **planner_feedback,
                    "failure_source": "planner",
                    "planner_records": merged_planner_records,
                    "val_records": val_records,
                }

            target_ids = retry_ids
            val_attempt += 1

    def _record_feedback_attempt(
        self,
        attempt_result: Dict[str, Any],
        will_retry: bool,
    ) -> None:
        if (
            not self.feedback_enabled
            or not isinstance(self.current_task_manifest, dict)
            or str(attempt_result.get("failure_source") or "").startswith("val_")
        ):
            return

        feedback_manifest = self.current_task_manifest.setdefault("feedback", {})
        attempts = feedback_manifest.setdefault("attempts", [])
        attempts.append({
            "attempt": attempt_result.get("attempt_index"),
            "succeeded": bool(attempt_result.get("succeeded")),
            "will_retry": bool(will_retry),
            "failed_subtask_ids": attempt_result.get("failed_subtask_ids", []),
            "feedback_path": attempt_result.get("feedback_path"),
            "planner_summary": [
                {
                    "problem_file": record.get("problem_file"),
                    "status": record.get("status"),
                    "return_code": record.get("return_code"),
                    "plan_generated": record.get("plan_generated"),
                    "has_planner_error": record.get("has_planner_error"),
                    "feedback_reason": record.get("feedback_reason"),
                }
                for record in attempt_result.get("planner_records", [])
            ],
        })
        feedback_manifest["enabled"] = True
        feedback_manifest["max_retries"] = self.feedback_max_retries
        self._persist_manifest()

    def _run_feedback_attempt(
        self,
        decomposed_plan: str,
        subtasks: List[str],
        robots: List[dict],
        objects_ai: str,
        key_objects: List[Dict[str, Any]],
        key_objects_by_subtask: Dict[int, List[Dict[str, Any]]],
        key_object_pddl_states: Optional[List[Dict[str, Any]]] = None,
        key_object_pddl_states_by_subtask: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        attempt_index: int = 1,
        feedback_text: Optional[str] = None,
        planner_feedback_by_subtask: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        if not robots:
            raise PDDLError("No robots available for task allocation")
        allocation_result = self._generate_allocation_plan(
            decomposed_plan,
            robots,
            objects_ai,
            key_objects=key_objects,
            key_objects_by_subtask=key_objects_by_subtask,
            feedback_text=feedback_text,
        )
        allocation_result = self._recover_allocation_plan(allocation_result, subtasks, robots)
        self._write_allocation_generation_artifacts(
            allocation_result,
            attempt_index=attempt_index,
        )
        allocated_plan = allocation_result["text"]
        print(f"✓ Allocation plan generated (attempt {attempt_index})")

        sequence_operations = self._extract_sequence_operations(allocated_plan)
        robot_assignments = self._extract_robot_assignments(sequence_operations)
        print(f"✓ Extracted {len(subtasks)} subtasks with robot assignments")

        _ = self._generate_problem_files(
            subtasks,
            robot_assignments,
            objects_ai,
            key_object_pddl_states=key_object_pddl_states,
            key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
            planner_feedback_by_subtask=planner_feedback_by_subtask,
        )
        print("✓ Problem files generated")

        planner_records = self._plan_generated_problems()
        print("✓ Planning complete")

        feedback_result = self._build_planner_feedback(
            planner_records,
            len(subtasks),
            robot_assignments,
        )
        feedback_result["failure_source"] = (
            None if feedback_result["succeeded"] else "planner"
        )

        feedback_path = None
        if (
            self.feedback_enabled
            and feedback_result.get("failure_source") == "planner"
            and feedback_result["feedback_text"]
        ):
            feedback_path = f"02_allocate/feedback/attempt_{attempt_index:02d}_feedback.txt"
            self._write_text_artifact(feedback_path, feedback_result["feedback_text"])
            self._record_artifact("allocate", f"attempt_{attempt_index:02d}_feedback", feedback_path)
            self._persist_manifest()

        return {
            "attempt_index": attempt_index,
            "allocation_result": allocation_result,
            "allocated_plan": allocated_plan,
            "sequence_operations": sequence_operations,
            "robot_assignments": robot_assignments,
            "planner_records": planner_records,
            "feedback_path": feedback_path,
            **feedback_result,
        }

    def _merge_planner_records(
        self,
        planner_records: Sequence[Dict[str, Any]],
        updated_records: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge updated VAL records without dropping allocation diagnostics."""
        updated_by_subtask = {
            subtask_id: dict(record)
            for record in updated_records
            for subtask_id in [
                self._subtask_id_from_filename(str(record.get("problem_file", "")))
            ]
            if subtask_id is not None
        }
        merged_records: List[Dict[str, Any]] = []
        merged_subtask_ids: Set[int] = set()
        for record in planner_records:
            subtask_id = self._subtask_id_from_filename(str(record.get("problem_file", "")))
            if subtask_id is not None and subtask_id in updated_by_subtask:
                merged_records.append(updated_by_subtask[subtask_id])
                merged_subtask_ids.add(subtask_id)
            else:
                merged_records.append(dict(record))

        for subtask_id in sorted(set(updated_by_subtask) - merged_subtask_ids):
            merged_records.append(updated_by_subtask[subtask_id])
        return merged_records
    
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

        planner_manifest_path = self.config.artifact(
            "planner_manifest",
            "08_planner/planner_manifest.json",
        )
        planner_records = self._read_json_artifact(planner_manifest_path, [])
        if not isinstance(planner_records, list):
            planner_records = []
        noop_records = self._read_json_artifact("08_planner/noop_subtasks.json", [])
        verified_noops = {
            int(record["subtask_id"])
            for record in noop_records
            if isinstance(record, dict)
            and str(record.get("subtask_id", "")).isdigit()
            and bool(record.get("verified"))
        } if isinstance(noop_records, list) else set()

        valid_subtasks: Optional[Set[int]] = None
        if self.val_feedback_enabled:
            val_manifest_path = self.config.artifact("val_manifest", "08_val/val_manifest.json")
            val_manifest = self._read_json_artifact(val_manifest_path, {})
            latest = val_manifest.get("latest_by_subtask", {}) if isinstance(val_manifest, dict) else {}
            valid_subtasks = {
                int(subtask_id)
                for subtask_id, record in latest.items()
                if str(subtask_id).isdigit()
                and isinstance(record, dict)
                and bool(record.get("valid"))
            } if isinstance(latest, dict) else set()

        completed: Set[int] = set()
        for record in planner_records:
            if not isinstance(record, dict) or not self._planner_record_success(record):
                continue
            subtask_id = self._subtask_id_from_filename(str(record.get("problem_file", "")))
            if subtask_id is None:
                continue
            if valid_subtasks is not None and subtask_id not in valid_subtasks:
                continue
            plan_path = record.get("compatibility_output")
            if not plan_path or not os.path.isfile(str(plan_path)):
                continue
            plan_text = self.file_processor.read_file(str(plan_path))
            if plan_has_actions(plan_text) or subtask_id in verified_noops:
                completed.add(subtask_id)

        return len(completed), total_subtasks

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
        """Process a list of tasks."""
        try:
            # Initial task count
            print(f"\n[DIAGNOSTIC] Initial Task Count: {len(test_tasks)}")
            
            # Store objects_ai for use in other methods
            self.objects_ai = objects_ai
            
            # Initialize or reset result lists
            self.decomposed_plan = []
            self.allocated_plan = []
            self.code_plan = []
            self.combined_plan = []
            self.code_planpddl = []
            self.tc = []
            self.total_subtasks = []
            self.task_results = []
            
            # Get domain content
            allaction_domain_path = str(self.config.allaction_domain_path())
            domain_content = self.file_processor.read_file(allaction_domain_path)
            effective_robot_domain_name_maps = (
                robot_domain_name_maps
                if robot_domain_name_maps is not None
                else self.dataset_robot_domain_name_maps
            )
            
            # Process each task
            for task_idx, (task, robots) in enumerate(zip(test_tasks, available_robots)):
                manifest_task_index = (
                    task_idx
                    if task_indices is None
                    else int(task_indices[task_idx])
                )
                print(f"\n{'='*50}")
                print(f"Processing Task: {task}: {task_idx + 1}/{len(test_tasks)}")
                print(f"{'='*50}")
                self.current_robot_domain_names = (
                    copy.deepcopy(effective_robot_domain_name_maps[task_idx])
                    if task_idx < len(effective_robot_domain_name_maps)
                    else {}
                )
                self._prepare_task_run_dir(
                    task_idx,
                    task,
                    robots,
                    objects_ai,
                    domain_content,
                    manifest_task_index=manifest_task_index,
                )
                
                # Clean generated subtask directory before starting new task
                self.clean_generated_subtask_directory()
                
                # Generate and store decomposed plan
                decomposed_plan = self._generate_decomposed_plan(task, domain_content, robots, objects_ai)
                self.decomposed_plan.append(decomposed_plan)
                
                print("✓ Decomposed plan generated")
                #print("decomposed plan:\n", decomposed_plan)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                subtasks = ParsingUtils.extract_subtasks(decomposed_plan)
                key_objects_by_subtask = self._extract_key_objects_by_subtask(subtasks, objects_ai)
                key_objects = self._combine_key_objects_by_subtask(key_objects_by_subtask)
                key_objects_artifact = "02_allocate/00_key_objects.json"
                key_objects_by_subtask_artifact = "02_allocate/00_key_objects_by_subtask.json"
                self._write_json_artifact(key_objects_artifact, key_objects)
                self._record_artifact("allocate", "key_objects", key_objects_artifact)
                self._write_json_artifact(key_objects_by_subtask_artifact, key_objects_by_subtask)
                self._record_artifact("allocate", "key_objects_by_subtask", key_objects_by_subtask_artifact)
                key_object_pddl_context_by_subtask = {
                    subtask_idx: self._build_key_object_pddl_context(
                        subtask_key_objects, domain_content,
                        task_text=subtasks[subtask_idx - 1],
                    )
                    for subtask_idx, subtask_key_objects in key_objects_by_subtask.items()
                }
                key_object_pddl_context = merge_object_contexts(
                    list(key_object_pddl_context_by_subtask.values())
                )
                key_object_pddl_states = key_object_pddl_context["states"]
                key_object_id_bindings = key_object_pddl_context["object_id_bindings"]
                key_object_pddl_states_by_subtask = {
                    subtask_idx: subtask_context["states"]
                    for subtask_idx, subtask_context in key_object_pddl_context_by_subtask.items()
                }
                key_object_pddl_evidence_by_subtask = {
                    subtask_idx: subtask_context.get("evidence", [])
                    for subtask_idx, subtask_context in key_object_pddl_context_by_subtask.items()
                }
                key_object_id_bindings_by_subtask = {
                    subtask_idx: subtask_context["object_id_bindings"]
                    for subtask_idx, subtask_context in key_object_pddl_context_by_subtask.items()
                }
                key_object_states_artifact = "05_problem_generation/key_object_pddl_states.json"
                key_object_states_by_subtask_artifact = "05_problem_generation/key_object_pddl_states_by_subtask.json"
                key_object_id_bindings_artifact = "05_problem_generation/key_object_id_bindings.json"
                key_object_id_bindings_by_subtask_artifact = "05_problem_generation/key_object_id_bindings_by_subtask.json"
                key_object_evidence_by_subtask_artifact = "05_problem_generation/key_object_pddl_state_evidence_by_subtask.json"
                self._write_json_artifact(key_object_states_artifact, key_object_pddl_states)
                self._record_artifact("problem_files", "key_object_pddl_states", key_object_states_artifact)
                self._write_json_artifact(key_object_states_by_subtask_artifact, key_object_pddl_states_by_subtask)
                self._record_artifact("problem_files", "key_object_pddl_states_by_subtask", key_object_states_by_subtask_artifact)
                self._write_json_artifact(
                    key_object_evidence_by_subtask_artifact,
                    key_object_pddl_evidence_by_subtask,
                )
                self._record_artifact(
                    "problem_files",
                    "key_object_pddl_state_evidence_by_subtask",
                    key_object_evidence_by_subtask_artifact,
                )
                self._write_json_artifact(key_object_id_bindings_artifact, key_object_id_bindings)
                self._record_artifact("problem_files", "key_object_id_bindings", key_object_id_bindings_artifact)
                self._write_json_artifact(key_object_id_bindings_by_subtask_artifact, key_object_id_bindings_by_subtask)
                self._record_artifact("problem_files", "key_object_id_bindings_by_subtask", key_object_id_bindings_by_subtask_artifact)
                self._persist_manifest()
                print(f"✓ Matched {len(key_objects)} key objects")

                max_attempts = 1 + (self.feedback_max_retries if self.feedback_enabled else 0)
                attempt_feedback_text = None
                planner_feedback_by_subtask = None
                attempt_result: Dict[str, Any] = {}
                for attempt_index in range(1, max_attempts + 1):
                    if attempt_index > 1:
                        print(f"Retrying from allocation with planner feedback (attempt {attempt_index}/{max_attempts})")
                        self._clean_feedback_attempt_outputs()

                    attempt_result = self._run_feedback_attempt(
                        decomposed_plan=decomposed_plan,
                        subtasks=subtasks,
                        robots=robots,
                        objects_ai=objects_ai,
                        key_objects=key_objects,
                        key_objects_by_subtask=key_objects_by_subtask,
                        key_object_pddl_states=key_object_pddl_states,
                        key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
                        attempt_index=attempt_index,
                        feedback_text=attempt_feedback_text,
                        planner_feedback_by_subtask=planner_feedback_by_subtask,
                    )

                    will_retry = (
                        self.feedback_enabled
                        and not attempt_result.get("succeeded", False)
                        and (attempt_result.get("failure_source") or "planner") == "planner"
                        and attempt_index < max_attempts
                    )
                    self._record_feedback_attempt(attempt_result, will_retry)
                    if not will_retry:
                        break

                    attempt_feedback_text = attempt_result.get("feedback_text") or ""
                    planner_feedback_by_subtask = attempt_result.get("planner_feedback_by_subtask") or {}

                if self.val_feedback_enabled:
                    final_planner_records = attempt_result.get("planner_records", [])
                    available_planner_records = [
                        record
                        for record in final_planner_records
                        if self._planner_record_success(record)
                    ]
                    if available_planner_records:
                        val_result = self._run_val_feedback_loop(
                            subtasks=subtasks,
                            robot_assignments=attempt_result.get("robot_assignments", {}),
                            objects_ai=objects_ai,
                            key_object_pddl_states=key_object_pddl_states,
                            key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
                            planner_records=available_planner_records,
                            allocation_attempt=int(attempt_result.get("attempt_index") or attempt_index),
                        )
                        attempt_result["planner_records"] = self._merge_planner_records(
                            final_planner_records,
                            val_result.get("planner_records", available_planner_records),
                        )
                        attempt_result["val_result"] = val_result
                        self._refresh_noop_audits(attempt_result["planner_records"])
                        print("✓ VAL validation complete")

                allocated_plan = attempt_result.get("allocated_plan", "")
                self.allocated_plan.append(allocated_plan)
                #print("Allocation Plan:\n", allocated_plan)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                
                # currently don't combine the plans
                # # Combine and process plans
                # combined_plan = self._combine_all_plans(decomposed_plan, sequence_operations)
                # self.combined_plan.append(combined_plan)
                # print("✓ Plans combined")
                # #print("Combined Plan:\n", combined_plan)
                # #input("Press Enter to continue")

                # # Match references and store final PDDL plan
                # matched_plan = self._match_references_for_plan(combined_plan, objects_ai)
                # self.code_planpddl.append(matched_plan)
                # print("✓ References matched")
                # print("Final PDDL Plan:\n", matched_plan)

                # Calculate completion rate
                tc, total = self.calculate_completion_rate()

                self.current_task_manifest["completion"] = {
                    "successful_subtasks": tc,
                    "total_subtasks": total
                }
                self._persist_manifest()
                print(f"Task {task_idx + 1} completion rate: {tc}/{total}")
                self.tc = tc
                self.total = total
                
            print(f"\n{'='*50}")
            print(f"All {len(test_tasks)} tasks processed")
            print(f"{'='*50}")
            
        except Exception as e:
            print(f"\n[ERROR] Task Processing Failed:")
            print(f"Error type: {type(e).__name__}")
            print(f"Error message: {str(e)}")
            print(f"Current task index: {task_idx if 'task_idx' in locals() else 'Not started'}")
            raise
        finally:
            get_llm_logger().clear_context()

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
        if bool(item.get("openable")):
            add_fact("is-openable", f"(is-openable {object_token})")
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
        task_text: str = "",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Convert key objects into PDDL states plus object-token bindings."""
        if not key_objects and not task_text:
            return {"states": [], "object_id_bindings": [], "evidence": []}

        key_object_types = {
            self._object_match_key(obj.get("name", ""))
            for obj in key_objects
            if isinstance(obj, dict) and isinstance(obj.get("name"), str)
        }
        key_object_types.discard("")
        if not key_object_types and not task_text:
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

        selected_objects, direct_ids = select_context_objects(
            self, floor_objects, floor_object_numbering, key_object_types, task_text,
        )
        for item in selected_objects:
            object_type = item.get("objectType")
            if not isinstance(object_type, str):
                continue

            object_entry = self._object_numbering_entry_for_metadata(item, floor_object_numbering)
            object_token = str(object_entry["object"])
            role = "key_object" if (item.get("objectId") or id(item)) in direct_ids else "parentReceptacle"
            record_binding(object_entry, role)
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
    ) -> Dict[str, Any]:
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
            response, text = self.llm.query_model(
                messages,
                self.model,
                max_completion_tokens=call_config.get("max_completion_tokens", 1300),
                frequency_penalty=call_config.get("frequency_penalty", 0.0),
            )

            return {"prompt": prompt, "text": text,
                    "finish_reason": extract_finish_reason(response),
                    "usage": extract_usage(response),
                    "max_completion_tokens": call_config.get("max_completion_tokens", 1300)}
            
        except Exception as e:
            raise PDDLError(f"Error generating decomposed plan: {str(e)}") from e

    def _generate_decomposed_plan(
        self,
        task: str,
        domain_content: str,
        robots: List[dict],
        objects_ai: str,
        write_artifacts: bool = True,
    ) -> str:
        """Validate decomposition before allocation, with one content retry."""
        records: List[Dict[str, Any]] = []
        messages: List[Dict[str, str]] = []
        budget = self.config.llm_call("decompose").get("max_completion_tokens", 1300)
        try:
            result = self._run_decompose_generation(task, domain_content, robots, objects_ai)
            prompt = result["prompt"]
            messages = [{"role": "user", "content": prompt}]
            required = None
            for attempt in range(2):
                if attempt:
                    response, text = self.llm.query_model(
                        messages, self.model, max_completion_tokens=budget,
                        frequency_penalty=self.config.llm_call("decompose").get("frequency_penalty", 0.0),
                    )
                    result = {"text": text, "finish_reason": extract_finish_reason(response),
                              "usage": extract_usage(response), "max_completion_tokens": budget}
                text = result["text"]
                validation = validate_decomposition(
                    text, domain_content=domain_content,
                    finish_reason=result.get("finish_reason"), usage=result.get("usage"),
                    max_completion_tokens=budget, required_subtasks=required,
                )
                status = validation["status"]
                if status == "invalid":
                    status = "retry_pending" if attempt == 0 else "retry_exhausted"
                record = {"attempt": attempt + 1, "max_completion_tokens": budget,
                          "finish_reason": result.get("finish_reason"), "usage": result.get("usage"),
                          "errors": validation["errors"], "status": status,
                          "required_subtasks": validation["required_subtasks"],
                          "normalized": validation["normalized_text"] != text}
                records.append(record)
                if write_artifacts:
                    self._write_decompose_attempt(records, messages, text, status)
                if status == "passed":
                    if write_artifacts:
                        output_path = self.config.artifact("decompose_output", "01_decompose/02_decompose_output.txt")
                        self._write_text_artifact(output_path, validation["normalized_text"])
                        self._record_artifact("decompose", "output", output_path)
                        self._persist_manifest()
                    return validation["normalized_text"]
                if status in {"parse_failed", "retry_exhausted"}:
                    details = "; ".join(error["message"] for error in validation["errors"])
                    raise PDDLError(f"Decomposition {status}: {details}")
                required = validation["required_subtasks"]
                if text.strip():
                    messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": build_retry_prompt(validation)})
                if validation["truncated"]:
                    budget *= 2
        except Exception as e:
            if write_artifacts and (isinstance(e, LLMError) or isinstance(e.__cause__, LLMError)):
                records.append({"attempt": len(records) + 1, "max_completion_tokens": budget,
                                "status": "api_failed", "error": str(e)})
                self._write_decompose_attempt(records, messages, None, "api_failed")
            if isinstance(e, PDDLError):
                raise
            raise PDDLError(f"Error generating decomposed plan: {str(e)}") from e

    def _write_decompose_attempt(
        self, records: List[Dict[str, Any]], messages: List[Dict[str, str]],
        text: Optional[str], status: str,
    ) -> None:
        """Persist each attempt before another request can fail or be interrupted."""
        record = records[-1]
        directory = f"01_decompose/attempts/attempt_{record['attempt']}"
        messages_path = f"{directory}/messages.json"
        self._write_json_artifact(messages_path, messages)
        record["messages"] = messages_path
        if text is not None:
            output_path = f"{directory}/output.txt"
            self._write_text_artifact(output_path, text)
            record["output"] = output_path
        if messages:
            prompt_path = self.config.artifact("decompose_prompt", "01_decompose/01_decompose_prompt.txt")
            self._write_text_artifact(prompt_path, messages[0]["content"])
            self._record_artifact("decompose", "prompt", prompt_path)
        validation = {"status": status, "max_retries": 1, "attempts": copy.deepcopy(records)}
        validation_path = "01_decompose/validation_manifest.json"
        self._write_json_artifact(validation_path, validation)
        self._record_artifact("decompose", "validation", validation_path)
        self.current_task_manifest["decompose_validation"] = validation
        self._persist_manifest()

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
    
    def _generate_allocation_plan(
        self,
        decomposed_plan: str,
        robots: List[dict],
        objects_ai: str,
        key_objects: Optional[List[Dict[str, Any]]] = None,
        key_objects_by_subtask: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        feedback_text: Optional[str] = None,
    ) -> Dict[str, str]:
        """Generate allocation plan for decomposed tasks.
        
        """
        try:
            subtasks = ParsingUtils.extract_subtasks(decomposed_plan)
            if key_objects_by_subtask is None:
                if subtasks:
                    key_objects_by_subtask = self._extract_key_objects_by_subtask(subtasks, objects_ai)
                else:
                    key_objects_by_subtask = {}
            if key_objects is None:
                key_objects = (
                    self._combine_key_objects_by_subtask(key_objects_by_subtask)
                    if key_objects_by_subtask
                    else self._extract_key_objects_from_decomposition(decomposed_plan, objects_ai)
                )
            if not key_objects_by_subtask and key_objects:
                key_objects_by_subtask = {1: key_objects}

            # Build prompt incrementally like the original
            allocation_decomposed_plan = _strip_legacy_inaction_prompt_text(
                self._strip_decomposition_completion_sentence(decomposed_plan)
            )
            task_description = ""
            if isinstance(self.current_task_manifest, dict):
                task_description = str(self.current_task_manifest.get("task") or "").strip()
            prompt = "\n"
            rag_query = ""
            if self.allocate_rag_retriever:
                rag_query = self._allocation_rag_query(decomposed_plan, robots, key_objects, subtasks)
            prompt += _strip_legacy_inaction_prompt_text(
                self._rag_or_static_prompt_block(
                    self.allocate_rag_retriever,
                    self._allocate_rag_prompt_block,
                    self._read_allocation_static_prompt,
                    rag_query,
                )
            )
            if task_description:
                prompt += f"\n# Task Description: {task_description}\n"
            prompt += allocation_decomposed_plan
            prompt += f"\n# TASK ALLOCATION"
            prompt += f"\n# Scenario: There are {len(robots)} robots available. Use available robots to execute independent subtasks in parallel whenever dependencies and robot capabilities allow. Robots should be assigned to subtasks that match their skills, and mass capacity should only be considered when a subtask requires picking up the relevant object. Using your reasoning come up with a solution to satisfy all constraints."
            prompt += f"\n\nrobots = {robots}"
            prompt += f"\nobjects = {key_objects}"
            prompt += f"\n\n# IMPORTANT: The AI should ensure that the robots assigned to the tasks have all the necessary skills to perform the tasks. IMPORTANT: Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both and allocate robots based on availability. "
            prompt += f"\n# SOLUTION\n"
            prompt += f"\n# Additional Output Rules:"
            prompt += f"\n# - Use robots and objects as allocation context."
            prompt += f"\n# - Assign robots using the task-local robot ids from robots = ..."
            prompt += f"\n# - Judge robot capability using robot skills, and only consider mass capacity for objects that the subtask requires a robot to pick up."
            prompt += f"\n# - Only assign a robot if it has every required skill."
            prompt += f"\n{SPECIAL_TASK_SKILL_PROMPT_RULE}"
            prompt += f"\n# - If multiple robots satisfy all constraints equally, choose the robot with the smallest robot number/name order."
            prompt += f"\n# - Mass capacity only matters for objects that must be picked up."
            prompt += f"\n# - The SOLUTION must strictly follow the concise reasoning style shown in the examples."
            prompt += f"\n# - For the **Sequence of Operations** part: if two or more subtasks can be executed in parallel (i.e., they are independent), they MUST be placed on the same line, separated by a semicolon and no newline. "
            prompt += f"\n#   Example correct format: Subtask 1: Robot 1;Subtask 2: Robot 2;"
            prompt += f"\n#   Sequential subtasks that depend on others should appear on their own new line."
            prompt += f"\n# - End with one final machine-readable block headed exactly '# Sequence of Operations:'."
            prompt += f"\n# - Every assignment in that final block must use numeric subtask and robot ids, e.g. 'Subtask 1: Robot 2;'."
            prompt += f"\n# - Do not use placeholders or non-numeric assignments such as 'Subtask;Robot;', 'Subtask A', 'Robot ?', or 'Robot A'."
            prompt += f"\n# - Do not output self-corrections or extra explanation after the final '# Sequence of Operations:' block.\n"
            if feedback_text:
                feedback_block = str(feedback_text).strip()
                if not feedback_block.startswith("# PLANNER FEEDBACK FROM PREVIOUS ATTEMPT"):
                    feedback_block = "# PLANNER FEEDBACK FROM PREVIOUS ATTEMPT\n\n" + feedback_block
                prompt += "\n" + feedback_block + "\n"
            messages = [{"role": "user", "content": prompt}]
            call_config = self.config.llm_call("allocate")
            _, text = self.llm.query_model(
                messages,
                self.allocate_model,
                frequency_penalty=call_config.get("frequency_penalty", 0.69),
            )
            return {"prompt": prompt, "text": text}
            
        except Exception as e:
            raise PDDLError(f"Error generating allocation plan: {str(e)}")

    def _allocation_validation_errors(
        self, text: str, subtask_count: int, robot_count: int,
    ) -> List[str]:
        assignments = self._extract_robot_assignments(self._extract_sequence_operations(text))
        expected = set(range(1, subtask_count + 1))
        errors = []
        if not assignments:
            errors.append("No assignments could be extracted.")
        missing = sorted(expected - assignments.keys())
        unexpected = sorted(assignments.keys() - expected)
        invalid_robots = {task: robot for task, robot in assignments.items()
                          if robot not in range(1, robot_count + 1)}
        if missing:
            errors.append(f"Missing subtask IDs: {missing}")
        if unexpected:
            errors.append(f"Unexpected subtask IDs: {unexpected}")
        if invalid_robots:
            errors.append(f"Invalid robot IDs (subtask: robot): {invalid_robots}")
        # The shared tolerant parser can truncate "Robot 1.5" or strip a minus
        # sign. Check raw IDs in the same selected section before accepting it.
        sections = self._extract_sequence_sections(text) or [text.splitlines()]
        _, selected = max(enumerate(sections), key=lambda item: (
            len(self._parse_sequence_section(item[1])[1]), item[0],
        ))
        invalid_numeric_ids = re.findall(
            r"\b(?:Sub\s*Task|Robot)[#_\s]*([+-]\s*\d+(?:\.\d+)?|\d+\.\d+)\b",
            "\n".join(selected), flags=re.IGNORECASE,
        )
        if invalid_numeric_ids:
            errors.append(f"Invalid numeric IDs: {invalid_numeric_ids}; use positive integers")
        return errors

    def _recover_allocation_plan(
        self, result: Dict[str, str], subtasks: List[str], robots: List[dict],
    ) -> Dict[str, Any]:
        """Repair an unusable allocation once, then resolve it entirely offline."""
        errors = self._allocation_validation_errors(result["text"], len(subtasks), len(robots))
        recovery: Dict[str, Any] = {"source": "initial_llm", "initial_errors": errors}
        resolved: Dict[str, Any] = {**result, "initial_text": result["text"], "recovery": recovery}
        if not errors:
            return resolved

        repair_prompt = (
            "Your previous allocation could not be used.\n"
            f"Validation errors: {'; '.join(errors)}\n\n"
            "Return a corrected, complete allocation for the existing subtasks.\n"
            f"Required subtask IDs: {list(range(1, len(subtasks) + 1))}\n"
            f"Allowed task-local robot IDs: {list(range(1, len(robots) + 1))}\n\n"
            "Assign every required subtask exactly once. Do not add, remove,\n"
            "merge, or renumber subtasks. Use only the listed robots and their\n"
            "actual skills. Follow the original capability and pickup-capacity\n"
            "constraints.\n\n"
            "Preserve task dependencies. Put independent subtasks on the same\n"
            "line only when their assigned robots can execute them in parallel.\n\n"
            "Output only one block headed exactly:\n"
            "# Sequence of Operations:\n\n"
            "Write every assignment as:\n"
            "Subtask <numeric_id>: Robot <numeric_id>;\n\n"
            "Separate parallel assignments with semicolons on the same line.\n"
            "Put sequential steps on separate lines.\n"
            "Do not include placeholders, Markdown fences, reasoning, or text\n"
            "after this block.\n"
        )
        messages = [
            {"role": "user", "content": result["prompt"]},
            {"role": "assistant", "content": result["text"]},
            {"role": "user", "content": repair_prompt},
        ]
        resolved.update(repair_prompt=repair_prompt, repair_messages=messages)
        print("Allocation validation failed; retrying once with format feedback")
        call_config = self.config.llm_call("allocate")
        try:
            _, repair_text = self.llm.query_model(
                messages, self.allocate_model,
                frequency_penalty=call_config.get("frequency_penalty", 0.69),
            )
        except Exception as exc:
            recovery["retry_error"] = str(exc)
        else:
            resolved["repair_text"] = repair_text
            retry_errors = self._allocation_validation_errors(repair_text, len(subtasks), len(robots))
            recovery["retry_errors"] = retry_errors
            if not retry_errors:
                recovery["source"] = "repair_llm"
                resolved["text"] = repair_text
                return resolved

        fallback_text, fallback_details = self._fallback_allocation_plan(subtasks, robots)
        recovery.update(fallback_details, source="skill_fallback")
        resolved["text"] = fallback_text
        print("Allocation repair failed; using offline core-skill allocation")
        return resolved

    @staticmethod
    def _allocation_skill_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    @classmethod
    def _load_allocation_fallback_rules(cls) -> List[Dict[str, Any]]:
        """Load the checked-in snapshot; never import data generation code here."""
        with ALLOCATION_FALLBACK_RULES_PATH.open(encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ValueError("Unsupported allocation fallback configuration")
        priorities = config.get("priorities")
        rules = config.get("rules")
        known_skills = config.get("robot_skills")
        if (not isinstance(priorities, dict) or not priorities
                or not all(type(value) is int for value in priorities.values())
                or not isinstance(known_skills, list) or not known_skills
                or not all(isinstance(skill, str) and skill for skill in known_skills)
                or not isinstance(rules, list) or not rules):
            raise ValueError("Missing or invalid allocation fallback tables")
        compiled = []
        seen_aliases: Set[str] = set()
        for rule in rules:
            if (not isinstance(rule, dict) or rule.get("skill") not in known_skills
                    or not isinstance(rule.get("priority_group"), str)
                    or rule["priority_group"] not in priorities):
                raise ValueError("Invalid allocation skill or priority group")
            for key in ("aliases", "patterns"):
                if (not isinstance(rule.get(key), list) or not rule[key]
                        or not all(isinstance(value, str) and value for value in rule[key])):
                    raise ValueError(f"Invalid allocation rule {key}")
            aliases = {cls._allocation_skill_key(alias) for alias in rule["aliases"]}
            if "" in aliases or seen_aliases.intersection(aliases):
                raise ValueError("Empty or ambiguous allocation skill alias")
            seen_aliases.update(aliases)
            compiled.append({
                "skill": rule["skill"], "priority": priorities[rule["priority_group"]],
                "aliases": aliases,
                "patterns": [re.compile(pattern, re.IGNORECASE | re.MULTILINE)
                             for pattern in rule["patterns"]],
            })
        return compiled

    @classmethod
    def _allocation_key_skill(cls, subtask: str, rules: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Prefer the task's goal over the support actions used to achieve it."""
        def phrase_match(text: str, source: str) -> Optional[Dict[str, Any]]:
            candidates = [
                (-rule["priority"], match.start(), index, match.group())
                for index, rule in enumerate(rules)
                for pattern in rule["patterns"]
                for match in pattern.finditer(text)
            ]
            if not candidates:
                return None
            _, _, index, matched = min(candidates)
            return {"skill": rules[index]["skill"], "match_source": source, "matched_text": matched}

        lines = subtask.strip().splitlines()
        first_line = lines[0].strip() if lines else ""
        header = _match_subtask_header_line(first_line, allow_bare=True)
        # A leading action/metadata label is not a task title.
        title = header.group("title") if header else (first_line if ":" not in first_line else "")
        title = re.split(r"\(?Skills?\s+Required\s*:", title, flags=re.IGNORECASE)[0].strip()
        match = phrase_match(title, "title")
        if match:
            return match

        aliases = {alias: rule for rule in rules for alias in rule["aliases"]}
        action_lines = []
        in_state_section = False
        for line in lines:
            clean = re.sub(r"^(?:[#*\-]+\s*|\d+[.)]\s*)+", "", line.strip()).strip()
            if re.match(r"(?:initial\b|preconditions?\b|effects?\b|parameters?\b|states?\b)",
                        clean, re.IGNORECASE):
                in_state_section = True
                continue
            if re.match(r"(?:actions?\b|steps?\b|instructions?\b)", clean, re.IGNORECASE):
                in_state_section = False
                continue
            action_label = re.match(r"([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)", clean)
            # A recognized action declaration starts a new action after the
            # preceding action's Preconditions/Effects. State labels do not.
            if action_label and cls._allocation_skill_key(action_label[1]) in aliases:
                if not re.match(r"(?:true|false|none|null)\b|[({]", action_label[2], re.IGNORECASE):
                    in_state_section = False
            if re.search(r"Skills?\s+Required\s*:", clean, re.IGNORECASE):
                in_state_section = False
            if not in_state_section:
                action_lines.append(clean)
        action_text = "\n".join(action_lines)
        candidates = []
        # Preserve document positions so equal priorities follow text order.
        for match in re.finditer(
            r"Skills?\s+Required\s*:\s*(?P<skills>[^\n#.)]+)|"
            r"^[ \t]*(?:[#*\-]+\s*)?(?P<action>[A-Za-z][A-Za-z0-9_]*)\s*:",
            action_text, flags=re.IGNORECASE | re.MULTILINE,
        ):
            group = "skills" if match.group("skills") is not None else "action"
            for token in re.finditer(r"[A-Za-z][A-Za-z0-9_]*", match.group(group)):
                rule = aliases.get(cls._allocation_skill_key(token.group()))
                if rule:
                    candidates.append((-rule["priority"], match.start(group) + token.start(),
                                       rule["skill"], token.group()))
        if candidates:
            _, _, skill, matched = min(candidates)
            return {"skill": skill, "match_source": "structured_skills", "matched_text": matched}

        return phrase_match(action_text, "body") or {
            "skill": None, "match_source": None, "matched_text": None,
        }

    def _fallback_allocation_plan(
        self, subtasks: List[str], robots: List[dict],
    ) -> Tuple[str, Dict[str, Any]]:
        if not robots:
            raise PDDLError("No robots available for task allocation")
        details: Dict[str, Any] = {"config_path": str(ALLOCATION_FALLBACK_RULES_PATH)}
        try:
            rules = self._load_allocation_fallback_rules()
        except (OSError, ValueError, TypeError, re.error) as exc:
            rules = []
            details["config_error"] = str(exc)
        robot_skills = [
            {self._allocation_skill_key(str(skill)) for skill in robot.get("skills", [])}
            for robot in robots
        ]
        assignments = []
        for subtask_id, subtask in enumerate(subtasks, start=1):
            match = self._allocation_key_skill(subtask, rules)
            skill = match["skill"]
            robot_id = next((index for index, skills in enumerate(robot_skills, start=1)
                             if skill and self._allocation_skill_key(skill) in skills), None)
            reason = ("skill_match" if robot_id else "skill_unavailable") if skill else "unrecognized_action"
            assignments.append({
                "subtask_id": subtask_id, **match, "robot_id": robot_id or 1,
                "reason": "config_error" if "config_error" in details else reason,
            })
        details["fallback_assignments"] = assignments
        text = "# Sequence of Operations:\n" + "".join(
            f"Subtask {row['subtask_id']}: Robot {row['robot_id']};\n" for row in assignments
        )
        return text, details

    def _write_allocation_generation_artifacts(
        self,
        result: Dict[str, Any],
        attempt_index: Optional[int] = None,
    ) -> None:
        """Persist allocation prompt/output artifacts produced by allocation generation."""
        allocate_prompt_artifact = self.config.artifact("allocate_prompt", "02_allocate/01_allocate_prompt.txt")
        allocate_output_artifact = self.config.artifact("allocate_output", "02_allocate/02_allocate_output.txt")
        self._write_text_artifact(allocate_prompt_artifact, result["prompt"])
        self._record_artifact("allocate", "prompt", allocate_prompt_artifact)
        self._write_text_artifact(allocate_output_artifact, result["text"])
        self._record_artifact("allocate", "output", allocate_output_artifact)
        if attempt_index is not None:
            attempt_prompt_artifact = f"02_allocate/attempt_{attempt_index:02d}/01_allocate_prompt.txt"
            attempt_output_artifact = f"02_allocate/attempt_{attempt_index:02d}/02_allocate_output.txt"
            self._write_text_artifact(attempt_prompt_artifact, result["prompt"])
            self._write_text_artifact(attempt_output_artifact, result["text"])
            self._record_artifact("allocate", f"attempt_{attempt_index:02d}_prompt", attempt_prompt_artifact)
            self._record_artifact("allocate", f"attempt_{attempt_index:02d}_output", attempt_output_artifact)
        if "recovery" in result:
            index = attempt_index if attempt_index is not None else 1
            directory = f"02_allocate/attempt_{index:02d}"
            artifacts = [
                ("initial_text", "initial_output", "03_initial_output.txt", False),
                ("repair_prompt", "repair_prompt", "04_repair_prompt.txt", False),
                ("repair_messages", "repair_messages", "04_repair_messages.json", True),
                ("repair_text", "repair_output", "05_repair_output.txt", False),
                ("recovery", "recovery", "06_recovery.json", True),
            ]
            for field, key, filename, is_json in artifacts:
                if field in result:
                    path = f"{directory}/{filename}"
                    writer = self._write_json_artifact if is_json else self._write_text_artifact
                    writer(path, result[field])
                    self._record_artifact("allocate", f"attempt_{index:02d}_{key}", path)
        self._persist_manifest()

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

    def _generate_problem_files(
        self,
        subtasks: List[str],
        robot_assignments: Dict[int, int],
        objects_ai: str,
        key_object_pddl_states: Optional[List[Dict[str, Any]]] = None,
        key_object_pddl_states_by_subtask: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        planner_feedback_by_subtask: Optional[Dict[int, str]] = None,
        val_feedback_by_subtask: Optional[Dict[int, str]] = None,
        subtask_ids: Optional[Set[int]] = None,
    ) -> List[str]:
        """Generate PDDL problem files from subtasks and robot assignments.

        Args:
            subtasks: List of subtask text
            robot_assignments: Dict mapping subtask index to robot number
            objects_ai: AI objects description
            key_object_pddl_states: PDDL-ready state facts for key objects
            key_object_pddl_states_by_subtask: PDDL-ready state facts keyed by subtask index

        Returns:
            List[str]: Generated PDDL problem files
        """
        problem_pddl = []
        subtasks_index_artifact = self.config.artifact("subtasks_index", "04_problem_files/03_subtasks.json")
        generated_problem_files_artifact = self.config.artifact("generated_problem_files", "04_problem_files/04_generated_problem_files.json")

        subtask_entries = []
        for idx, subtask in enumerate(subtasks, start=1):
            relative_path = f"04_problem_files/subtasks/subtask_{idx:02d}.txt"
            self._write_text_artifact(relative_path, subtask)
            subtask_entries.append({
                "index": idx,
                "path": relative_path
            })
        self._write_json_artifact(subtasks_index_artifact, subtask_entries)
        self._record_artifact("problem_files", "subtasks_index", subtasks_index_artifact)

        self._ensure_raw_problem_output_dir()
        problem_pddl = self.problemextracting(
            subtasks=subtasks,
            robot_assignments=robot_assignments,
            llm=self.llm,
            model=self.model,
            file_processor=self.file_processor,
            objects_ai=objects_ai,
            prompt_allocation_set=self.prompt_allocation_set,
            key_object_pddl_states=key_object_pddl_states,
            key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
            planner_feedback_by_subtask=planner_feedback_by_subtask,
            val_feedback_by_subtask=val_feedback_by_subtask,
            subtask_ids=subtask_ids,
        )
        selected_ids = (
            sorted(subtask_ids)
            if subtask_ids is not None
            else list(range(1, len(subtasks) + 1))
        )
        generated_updates = []
        raw_problem_dir = self._get_raw_problem_file_path()
        for subtask_id in selected_ids:
            generated_path = (
                os.path.join(raw_problem_dir, f"subtask_{subtask_id:02d}_problem.pddl")
                if raw_problem_dir
                else ""
            )
            if generated_path and os.path.isfile(generated_path):
                generated_updates.append({
                    "index": subtask_id,
                    "content": self.file_processor.read_file(generated_path),
                })
        existing_generated = self._read_json_artifact(generated_problem_files_artifact, [])
        if not isinstance(existing_generated, list) or subtask_ids is None:
            existing_generated = []
        generated_by_index = {
            int(entry["index"]): entry
            for entry in existing_generated
            if isinstance(entry, dict) and str(entry.get("index", "")).isdigit()
        }
        for entry in generated_updates:
            generated_by_index[int(entry["index"])] = entry
        self._write_json_artifact(
            generated_problem_files_artifact,
            [generated_by_index[index] for index in sorted(generated_by_index)],
        )
        self._record_artifact("problem_files", "generated_problem_files", generated_problem_files_artifact)
        self._persist_manifest()

        return problem_pddl

    @staticmethod
    def _format_problem_prompt_subtask(subtask: str) -> str:
        """Remove numbered subtask headers before adding the problem prompt label."""
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
    

    def _run_problem_generation(
            self,
            subtasks: List[str],
            robot_assignments: Dict[int, int],
            llm: 'LLMHandler',
            model: str,
            objects_ai: str,
            domain_contents_by_robot: Dict[str, str],
            static_problem_prompt: str,
            key_object_pddl_states: Optional[List[Dict[str, Any]]] = None,
            key_object_pddl_states_by_subtask: Optional[Dict[Union[int, str], List[Dict[str, Any]]]] = None,
            planner_feedback_by_subtask: Optional[Dict[int, str]] = None,
            val_feedback_by_subtask: Optional[Dict[int, str]] = None,
            subtask_ids: Optional[Set[int]] = None,
        ) -> List[ProblemGenerationResult]:
        """Generate problem prompts and PDDL results without reading or writing artifacts.

        Args:
            subtasks: List of subtask text
            robot_assignments: Dict mapping subtask index to robot number
            llm: LLM handler
            model: Model name
            objects_ai: AI objects description
            domain_contents_by_robot: Domain content keyed by real robot/domain name
            static_problem_prompt: Static fallback few-shot prompt content
            key_object_pddl_states: PDDL-ready state facts for key objects
            key_object_pddl_states_by_subtask: PDDL-ready state facts keyed by subtask index

        Returns:
            List[ProblemGenerationResult]: Generated in-memory problem artifacts
        """
        results: List[ProblemGenerationResult] = []

        for subtask_idx, subtask in enumerate(subtasks, start=1):
            if subtask_ids is not None and subtask_idx not in subtask_ids:
                continue
            robot_num = robot_assignments.get(subtask_idx, 1)
            normalized_robot_name = f"robot{robot_num}"
            real_robot_name = self.current_robot_domain_names.get(normalized_robot_name, normalized_robot_name)

            domain_content = (
                domain_contents_by_robot.get(real_robot_name)
                or domain_contents_by_robot.get(normalized_robot_name, "")
            )
            if not domain_content:
                print(f"Domain content not provided or empty for robot/domain: {real_robot_name}")
                continue

            subtask_key_object_states = key_object_pddl_states
            if key_object_pddl_states_by_subtask is not None:
                if subtask_idx in key_object_pddl_states_by_subtask:
                    subtask_key_object_states = key_object_pddl_states_by_subtask[subtask_idx]
                else:
                    subtask_key_object_states = key_object_pddl_states_by_subtask.get(str(subtask_idx), [])
            domain_key_object_states = self._filter_key_object_pddl_states_for_domain(
                subtask_key_object_states,
                domain_content,
            )
            key_object_pddl_states_text = json.dumps(
                domain_key_object_states,
                ensure_ascii=False,
                indent=2,
            )
            subtask_feedback = ""
            if planner_feedback_by_subtask:
                subtask_feedback = str(planner_feedback_by_subtask.get(subtask_idx) or "").strip()
            val_feedback = ""
            if val_feedback_by_subtask:
                val_feedback = str(val_feedback_by_subtask.get(subtask_idx) or "").strip()
            problem_prompt_examples = static_problem_prompt
            if self.problem_rag_retriever:
                rag_query = self._problem_rag_query(
                    subtask_idx,
                    subtask,
                    real_robot_name,
                    domain_content,
                    objects_ai,
                    domain_key_object_states,
                )
                manifest_key = f"subtask_{subtask_idx:02d}"
                problem_prompt_examples = self._rag_or_static_prompt_block(
                    self.problem_rag_retriever,
                    lambda query_text, key=manifest_key: self._problem_rag_prompt_block(query_text, key),
                    lambda: static_problem_prompt,
                    rag_query,
                )
            problem_prompt_examples = _strip_legacy_inaction_prompt_text(problem_prompt_examples)

            subtask_prompt_text = _strip_legacy_inaction_prompt_text(
                self._format_problem_prompt_subtask(subtask)
            )
            prompt = (
                "\n" + problem_prompt_examples +
                " Finish the tasks like example\n"
                "Subtask : " + subtask_prompt_text +
                "\nDomain file content:\n" + domain_content +
                "\nkey_object_pddl_states = " + key_object_pddl_states_text +
                "\nTask description: generate the problem file. Based on "
                "the domain file preconditions, actions, and subtask. "
                "Use key_object_pddl_states as PDDL-ready context: declare every object token "
                "referenced by those entries and copy applicable facts into (:init). "
                "Do not emit raw AI2-THOR state fields such as isOpen or isToggled; emit only "
                "predicates declared in the domain file. "
                f"IMPORTANT {normalized_robot_name} is only the task-local allocation name. "
                f"The real PDDL domain and robot object for this subtask is {real_robot_name}. "
                f"IMPORTANT the generated problem must use (:domain {real_robot_name}) and "
                f"must use {real_robot_name} as the robot object token. "
                "#IMPORTANT, strictly follow the structure, stop generating after the Problem file generation is done."
            )
            if subtask_feedback:
                prompt += (
                    "\n# PLANNER FEEDBACK FOR THIS SUBTASK\n"
                    "The previous attempt may not have completed this subtask with the assigned robot. "
                    "Use the feedback below while generating the next problem for this subtask.\n"
                    + subtask_feedback
                )
            if val_feedback:
                prompt += (
                    "\n# VAL FEEDBACK FROM PREVIOUS ATTEMPT\n"
                    "The previous problem and its newly generated plan failed VAL validation. "
                    "Regenerate the PDDL problem so that a new plan satisfies the domain semantics "
                    "and passes VAL. Do not change the assigned robot.\n"
                    + val_feedback
                )

            messages = [
                {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                {"role": "user", "content": prompt}
            ]
            call_config = self.config.llm_call("problem_generation")
            _, text = llm.query_model(
                messages,
                model,
                frequency_penalty=call_config.get("frequency_penalty", 0.4),
            )

            extracted_problem = self._extract_pddl_problem_block(text)
            extracted_problem = self._force_problem_robot_name(
                extracted_problem,
                normalized_robot_name,
                real_robot_name,
            )
            extracted_problem = self._force_problem_domain(extracted_problem, real_robot_name)
            extracted_problem, _, _ = repair_unknown_object_types(
                extracted_problem,
                domain_content,
            )
            repair_result: Optional[ProblemRepairResult] = None
            if self.problem_repair_enabled:
                repair_result = repair_problem_pddl(
                    extracted_problem,
                    domain_content,
                    raw_output=text,
                )
                extracted_problem = repair_result.problem
            results.append(
                ProblemGenerationResult(
                    subtask_index=subtask_idx,
                    normalized_robot_name=normalized_robot_name,
                    real_robot_name=real_robot_name,
                    prompt=prompt,
                    raw_output=text,
                    problem=extracted_problem,
                    repair=repair_result,
                )
            )

        return results

    def problemextracting(
            self,
            subtasks: List[str],
            robot_assignments: Dict[int, int],
            llm: 'LLMHandler',
            model: str,
            file_processor: 'FileProcessor',
            objects_ai: str,
            prompt_allocation_set: str,
            key_object_pddl_states: Optional[List[Dict[str, Any]]] = None,
            key_object_pddl_states_by_subtask: Optional[Dict[Union[int, str], List[Dict[str, Any]]]] = None,
            planner_feedback_by_subtask: Optional[Dict[int, str]] = None,
            val_feedback_by_subtask: Optional[Dict[int, str]] = None,
            subtask_ids: Optional[Set[int]] = None,
        ) -> List[str]:
        """Extract problem files from subtasks using precomputed robot assignments.

        Args:
            subtasks: List of subtask text
            robot_assignments: Dict mapping subtask index to robot number
            llm: LLM handler
            model: Model name
            file_processor: File processor instance
            objects_ai: AI objects description
            prompt_allocation_set: Prompt template name
            key_object_pddl_states: PDDL-ready state facts for key objects
            key_object_pddl_states_by_subtask: PDDL-ready state facts keyed by subtask index

        Returns:
            List[str]: Generated PDDL problem files
        """
        domain_contents_by_robot: Dict[str, str] = {}
        for subtask_idx, _ in enumerate(subtasks, start=1):
            robot_num = robot_assignments.get(subtask_idx, 1)
            normalized_robot_name = f"robot{robot_num}"
            real_robot_name = self.current_robot_domain_names.get(normalized_robot_name, normalized_robot_name)
            if real_robot_name in domain_contents_by_robot:
                continue

            robotassignnumber = f"{real_robot_name}.pddl"
            domain_path = str(self.config.robot_domain_path(robotassignnumber))
            domain_contents_by_robot[real_robot_name] = file_processor.read_file(domain_path) or ""

        problem_fileexamplepath = self.config.prompt_file(f"{prompt_allocation_set}_problem.txt")
        problem_examplecontent = file_processor.read_file(str(problem_fileexamplepath)) or ""
        results = self._run_problem_generation(
            subtasks=subtasks,
            robot_assignments=robot_assignments,
            llm=llm,
            model=model,
            objects_ai=objects_ai,
            domain_contents_by_robot=domain_contents_by_robot,
            static_problem_prompt=problem_examplecontent,
            key_object_pddl_states=key_object_pddl_states,
            key_object_pddl_states_by_subtask=key_object_pddl_states_by_subtask,
            planner_feedback_by_subtask=planner_feedback_by_subtask,
            val_feedback_by_subtask=val_feedback_by_subtask,
            subtask_ids=subtask_ids,
        )

        for result in results:
            prompt_path = f"05_problem_generation/prompts/subtask_{result.subtask_index:02d}_prompt.txt"
            output_path0 = f"05_problem_generation/outputs/subtask_{result.subtask_index:02d}_problem.raw.txt"
            output_path1 = f"05_problem_generation/outputs/subtask_{result.subtask_index:02d}_problem.pddl"
            self._write_text_artifact(prompt_path, result.prompt)
            self._write_text_artifact(output_path0, result.raw_output)
            self._write_text_artifact(output_path1, result.problem)

        self._write_problem_repair_manifest(results, replace_all=subtask_ids is None)

        return [result.problem for result in results]

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

    def _plan_generated_problems(self, subtask_ids: Optional[Set[int]] = None) -> List[Dict[str, Any]]:
        """Plan generated problem files, optionally restricted to selected subtasks."""
        try:
            if subtask_ids is None:
                return self.run_planners()
            return self.run_planners(subtask_ids=subtask_ids)
        except Exception as e:
            raise PDDLError(f"Error planning generated problems: {str(e)}")

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
        if audit.failure_reasons:
            raise PlanningError(
                f"Problem state audit failed for subtask {subtask_id}: "
                + "; ".join(audit.failure_reasons)
            )
        return audit_record

    def _refresh_noop_audits(
        self,
        planner_records: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        artifact = "08_planner/noop_subtasks.json"
        existing = self._read_json_artifact(artifact, [])
        if not isinstance(existing, list):
            existing = []
        by_id = {
            int(item["subtask_id"]): item
            for item in existing
            if isinstance(item, dict) and str(item.get("subtask_id", "")).isdigit()
        }
        problem_audits = self._read_json_artifact(
            "05_problem_generation/problem_state_audit.json",
            [],
        )
        audit_by_id = {
            int(item["subtask_id"]): item
            for item in problem_audits
            if isinstance(item, dict) and str(item.get("subtask_id", "")).isdigit()
        } if isinstance(problem_audits, list) else {}
        val_manifest = self._read_json_artifact(
            self.config.artifact("val_manifest", "08_val/val_manifest.json"),
            {},
        )
        latest_val = (
            val_manifest.get("latest_by_subtask", {})
            if isinstance(val_manifest, dict)
            else {}
        )

        refreshed: List[Dict[str, Any]] = []
        for planner_record in planner_records:
            subtask_id = self._subtask_id_from_filename(
                str(planner_record.get("problem_file", ""))
            )
            if subtask_id is None:
                continue
            by_id.pop(subtask_id, None)
            plan_path = planner_record.get("compatibility_output")
            audit_record = audit_by_id.get(subtask_id, {})
            plan_exists = bool(plan_path and os.path.isfile(str(plan_path)))
            if not plan_exists and not audit_record.get("failure_reasons"):
                continue
            plan_text = self.file_processor.read_file(str(plan_path)) if plan_exists else ""
            if plan_has_actions(plan_text):
                continue
            problem_path = planner_record.get("problem_path")
            if not problem_path:
                problem_dir = self._get_raw_problem_file_path()
                problem_path = (
                    os.path.join(problem_dir, str(planner_record.get("problem_file", "")))
                    if problem_dir
                    else ""
                )
            problem = (
                self.file_processor.read_file(str(problem_path))
                if problem_path and os.path.isfile(str(problem_path))
                else ""
            )
            val_record = (
                latest_val.get(str(subtask_id), latest_val.get(subtask_id))
                if isinstance(latest_val, dict)
                else None
            )
            proof = verify_zero_action_plan(
                subtask_id=subtask_id,
                problem=problem,
                plan_text=plan_text,
                evidence=self._key_object_evidence_for_subtask(subtask_id),
                planner_record=planner_record,
                val_enabled=self.val_feedback_enabled,
                val_record=val_record if isinstance(val_record, dict) else None,
                repairs=audit_record.get("repairs", []),
            )
            by_id[subtask_id] = proof
            refreshed.append(proof)

        self._write_json_artifact(artifact, [by_id[key] for key in sorted(by_id)])
        self._record_artifact("planner", "noop_subtasks", artifact)
        return refreshed

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
                    safe_name = self._sanitize_filename(problem_file.replace(".pddl", ""))
                    output_file = os.path.join(plan_file_path, f"{safe_name}_plan.txt")
                    if os.path.isfile(output_file):
                        os.unlink(output_file)
                    if subtask_id is not None:
                        self._audit_problem_before_planning(problem_file_full, subtask_id)
                    domain_name = self.file_processor.extract_domain_name(problem_file_full)
                    if not domain_name:
                        print(f"No domain specified in {problem_file}")
                        planner_records.append({
                            "problem_file": problem_file,
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
            self._refresh_noop_audits(planner_records)
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
    plan_feedback_group = parser.add_mutually_exclusive_group()
    plan_feedback_group.add_argument(
        "--plan-feedback",
        dest="plan_feedback",
        action="store_true",
        help="Enable planner-feedback retries from allocation onward.",
    )
    plan_feedback_group.add_argument(
        "--no-plan-feedback",
        dest="plan_feedback",
        action="store_false",
        help="Disable planner-feedback retries.",
    )
    parser.add_argument(
        "--plan-feedback-max-retries",
        dest="plan_feedback_max_retries",
        type=int,
        default=None,
        help="Maximum feedback retry rounds after the first allocation attempt.",
    )
    val_feedback_group = parser.add_mutually_exclusive_group()
    val_feedback_group.add_argument(
        "--val-feedback",
        dest="val_feedback",
        action="store_true",
        help="Enable VAL validation and failed-subtask problem-generation retries.",
    )
    val_feedback_group.add_argument(
        "--no-val-feedback",
        dest="val_feedback",
        action="store_false",
        help="Disable VAL validation and feedback retries.",
    )
    parser.add_argument(
        "--val-feedback-max-retries",
        type=int,
        default=None,
        help="Maximum VAL feedback retry rounds after the first validation attempt.",
    )
    parser.set_defaults(
        decompose_rag=False,
        allocate_rag=False,
        problem_rag=False,
        problem_repair=None,
        plan_feedback=None,
        val_feedback=None,
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
        apply_feedback_cli_override(
            run_config,
            args.plan_feedback,
            args.plan_feedback_max_retries,
        )
        apply_val_feedback_cli_override(
            run_config,
            getattr(args, "val_feedback", None),
            getattr(args, "val_feedback_max_retries", None),
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



    
