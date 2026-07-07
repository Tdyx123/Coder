import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

import yaml


CONFIG_FILE_NAME = "pddlrun_llmseparate_config.yaml"
FAST_DOWNWARD_ENV_VAR = "FAST_DOWNWARD_PATH"


DEFAULT_RUN_CONFIG: Dict[str, Any] = {
    "storage": {
        "base_dir": "logs/intermediate_runs",
        "task_manager_runs_dir": "logs/task_manager_runs",
        "default_generated_subtask_dir": "resources/generated_subtask",
        "default_validated_subtask_dir": "resources/validated_subtask",
        "default_each_run_dir": "resources/each_run",
        "parallel_output_root": "parallel_runs",
    },
    "data": {
        "dataset_dir": "data",
        "prompt_template_dir": "prompts/v1",
        "ai2thor_objects_cache_dir": "data/ai2thor_objects_cache",
        "ai2thor_objects_file": "data/all_ai2thor_objects.json",
    },
    "resources": {
        "resources_dir": "resources",
        "robot_domain_dir": "resources",
        "allaction_domain_file": "allactionrobot.pddl",
    },
    "planner": {
        "executable": "downward/fast-downward.py",
        "alias": "seq-opt-lmcut",
        "timeout_seconds": 300,
    },
    "llm": {
        "providers_file": "scripts/providers.yaml",
        "default_max_tokens": 128,
        "default_temperature": 0,
        "default_retry_delay": 20,
        "max_retries": 3,
        "calls": {
            "structure_fix": {"max_tokens": 1400, "frequency_penalty": 0.4},
            "validate_problem": {"max_tokens": 1400, "frequency_penalty": 0.4},
            "decompose": {"max_tokens": 1300, "frequency_penalty": 0.0},
            "precedence": {"max_tokens": 700, "frequency_penalty": 0.0},
            "allocate": {"max_tokens": 1500, "frequency_penalty": 0.69},
            "summary": {"max_tokens": 1400, "frequency_penalty": 0.4},
            "problem_generation": {"max_tokens": 1400, "frequency_penalty": 0.4},
            "llm_validator": {"max_tokens": 1400, "frequency_penalty": 0.4},
            "combine": {"max_tokens": 1300, "frequency_penalty": 0.0},
            "final_match": {"max_tokens": 1300, "frequency_penalty": 0.0},
        },
    },
    "decompose_rag": {
        "enabled": False,
        "corpus_path": "data/rag/task_decompose_corpus.jsonl",
        "index_path": "data/rag/task_decompose_index.json",
        "runtime_db_path": "data/rag/task_decompose_runtime.sqlite",
        "quality": ["success"],
        "retrieval_eligible_only": True,
        "top_k": 2,
        "max_example_chars": 3000,
        "max_block_chars": 8000,
        "max_query_tokens": 12,
        "query_timeout_seconds": 5,
        "prewarm_runtime_db": True,
    },
    "artifacts": {
        "manifest": "run_manifest.json",
        "llm_calls": "00_llm/llm_calls.jsonl",
        "task_context": "inputs/task_context.json",
        "domain_content": "inputs/domain_content.pddl",
        "generated_subtask_dir": "06_split/generated_subtask",
        "validated_subtask_dir": "07_validate/validated_subtask",
        "each_run_dir": "artifacts/each_run",
        "generated_subtask_manifest": "06_split/generated_subtask_manifest.json",
        "validation_manifest": "07_validate/validation_manifest.json",
        "planner_manifest": "08_planner/planner_manifest.json",
        "decompose_prompt": "01_decompose/01_decompose_prompt.txt",
        "decompose_output": "01_decompose/02_decompose_output.txt",
        "precedence_output": "02_precedence/predecessors.json",
        "precedence_manifest": "02_precedence/pairwise_manifest.json",
        "allocate_prompt": "02_allocate/01_allocate_prompt.txt",
        "allocate_output": "02_allocate/02_allocate_output.txt",
        "allocate_subtasks": "02_allocate/subtasks.json",
        "problem_summary_raw": "04_problem_files/01_problem_summary_raw.txt",
        "sequence_operations": "04_problem_files/02_sequence_operations.txt",
        "subtasks_index": "04_problem_files/03_subtasks.json",
        "generated_problem_files": "04_problem_files/04_generated_problem_files.json",
        "combine_prompt": "09_combine/01_combine_prompt.txt",
        "combine_output": "09_combine/02_combined_plan.txt",
        "final_match_prompt": "10_final_match/01_match_prompt.txt",
        "final_match_output": "10_final_match/02_final_plan.txt",
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_floor_plan(value: str) -> str:
    """Normalize FloorPlan-style identifiers to their suffix form."""
    text = str(value).strip()
    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]
    return text


def apply_decompose_rag_cli_override(config: "RunConfig", enabled: bool) -> "RunConfig":
    """Apply the CLI-level task-decomposition RAG default."""
    rag_config = config.values.setdefault("decompose_rag", {})
    if not isinstance(rag_config, dict):
        rag_config = {}
        config.values["decompose_rag"] = rag_config
    rag_config["enabled"] = bool(enabled)
    return config


class RunConfig:
    """Resolved runtime configuration for pddlrun_llmseparate workflows."""

    def __init__(
        self,
        base_path: Union[str, Path],
        values: Optional[Dict[str, Any]] = None,
        config_path: Optional[Union[str, Path]] = None,
    ):
        self.base_path = Path(base_path).resolve()
        self.config_path = Path(config_path).resolve() if config_path else Path(__file__).resolve().parent / CONFIG_FILE_NAME
        self.values = _deep_merge(DEFAULT_RUN_CONFIG, values or {})
        self._apply_legacy_aliases()

    def _apply_legacy_aliases(self) -> None:
        storage = self.values.setdefault("storage", {})
        if storage.get("intermediate_base_dir"):
            storage["base_dir"] = storage["intermediate_base_dir"]
        if storage.get("base_dir"):
            storage["intermediate_base_dir"] = storage["base_dir"]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        section_value = self.values.get(section, {})
        if not isinstance(section_value, dict):
            return default
        return section_value.get(key, default)

    def path(self, section: str, key: str) -> Path:
        return self.resolve_path(self.get(section, key))

    def resolve_path(self, value: Union[str, Path]) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return self.base_path / path

    @property
    def storage_base_dir(self) -> Path:
        return self.path("storage", "base_dir")

    @property
    def task_manager_runs_dir(self) -> Path:
        return self.path("storage", "task_manager_runs_dir")

    @property
    def resources_dir(self) -> Path:
        return self.path("resources", "resources_dir")

    @property
    def robot_domain_dir(self) -> Path:
        return self.path("resources", "robot_domain_dir")

    @property
    def prompt_template_dir(self) -> Path:
        return self.path("data", "prompt_template_dir")

    @property
    def providers_file(self) -> Path:
        return self.path("llm", "providers_file")

    @property
    def ai2thor_objects_cache_dir(self) -> Path:
        return self.path("data", "ai2thor_objects_cache_dir")

    @property
    def ai2thor_objects_file(self) -> Path:
        return self.path("data", "ai2thor_objects_file")

    @property
    def planner_executable(self) -> Path:
        shared_planner = os.getenv(FAST_DOWNWARD_ENV_VAR)
        if shared_planner:
            return Path(shared_planner).expanduser().resolve()
        return self.path("planner", "executable")

    def allaction_domain_path(self) -> Path:
        configured = self.get("resources", "allaction_domain_file")
        path = Path(configured)
        return path if path.is_absolute() else self.resources_dir / path

    def dataset_file(self, test_set: str, floor_plan: Union[int, str]) -> Path:
        normalized = normalize_floor_plan(str(floor_plan))
        return self.path("data", "dataset_dir") / test_set / f"FloorPlan{normalized}.jsonl"

    def available_test_sets(self) -> List[str]:
        dataset_dir = self.path("data", "dataset_dir")
        if not dataset_dir.exists():
            return []
        return sorted(
            entry.name
            for entry in dataset_dir.iterdir()
            if entry.is_dir() and any(entry.glob("FloorPlan*.jsonl"))
        )

    def prompt_file(self, name: str) -> Path:
        return self.prompt_template_dir / name

    def robot_domain_path(self, filename: str) -> Path:
        return self.robot_domain_dir / filename

    def artifact(self, key: str, default: str) -> str:
        return str(self.get("artifacts", key, default))

    def llm_call(self, name: str) -> Dict[str, Any]:
        calls = self.get("llm", "calls", {})
        if not isinstance(calls, dict):
            return {}
        value = calls.get(name, {})
        return value if isinstance(value, dict) else {}


def load_run_config(
    base_path: Union[str, Path],
    config_path: Optional[Union[str, Path]] = None,
    error_cls: Type[Exception] = ValueError,
) -> RunConfig:
    """Load and resolve the shared runtime config for single and parallel runs."""
    config_file = Path(config_path).resolve() if config_path else Path(__file__).resolve().parent / CONFIG_FILE_NAME
    loaded: Dict[str, Any] = {}

    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as file:
            parsed = yaml.safe_load(file) or {}
        if not isinstance(parsed, dict):
            raise error_cls(f"Invalid config format in {config_file}")
        loaded = parsed

    return RunConfig(base_path=base_path, values=loaded, config_path=config_file)
