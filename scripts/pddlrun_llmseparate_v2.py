import copy
import json
import os
import argparse
import ast
from pathlib import Path
from datetime import datetime
import random
import subprocess
import time
import re
import shutil
import sys
from typing import List, Dict, Tuple, Optional, Union, Any, Set
import uuid

from ai2thor_object_cache import get_ai2_thor_objects_cached
from llm_client import get_available_models as get_litellm_models
from file_processor import FileProcessor, PDDLError
from llm_handler import LLMError, LLMHandler
from llm_logger import get_llm_logger
from parsing_utils import ParsingUtils
from run_config import RunConfig, load_run_config as _load_run_config, normalize_floor_plan
from special_task_skills import SPECIAL_TASK_SKILL_ALIASES, SPECIAL_TASK_SKILL_PROMPT_RULE

import sys
sys.path.append(".")

import resources.actions as actions
import resources.robots as robots


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_run_config(base_path: Union[str, Path], config_path: Optional[Union[str, Path]] = None) -> RunConfig:
    """Load and resolve the shared runtime config for single and parallel runs."""
    config = _load_run_config(base_path, config_path, error_cls=PDDLError)
    config.values.setdefault("data", {})["prompt_template_dir"] = "prompts/v2"
    return config


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

class PDDLValidator:
    """Handles PDDL validation operations"""
    
    def __init__(self, llm_handler: LLMHandler, file_processor: FileProcessor, config: Optional[RunConfig] = None):
        """Initialize the PDDL validator.
        
        Args:
            llm_handler (LLMHandler)
            file_processor (FileProcessor)
        """
        self.llm = llm_handler
        self.file_processor = file_processor
        self.config = config or load_run_config(_repo_root())
    
    def validate_problem(self, domain_file: str, problem_file: str, model: str) -> None:
        """Validate a PDDL problem file against its domain.


        """
        try:
            domain_content = self.file_processor.read_file(domain_file)
            problem_content = self.file_processor.read_file(problem_file)
            
            prompt = (
                f"Domain Description:\n{domain_content}\n\n"
                f"Problem Description:\n{problem_content}\n\n"
                "Validate the preconditions in problem file to ensure all precondition listed object "

            )
            

            messages = [
                {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                {"role": "user", "content": prompt}
            ]
            call_config = self.config.llm_call("validate_problem")
            _, validated_text = self.llm.query_model(
                messages,
                model,
                frequency_penalty=call_config.get("frequency_penalty", 0.4),
            )
            
            # Save the validated content back to the problem file
            self.file_processor.write_file(problem_file, validated_text)
            
        except Exception as e:
            raise ValidationError(f"Error validating PDDL problem: {str(e)}")

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
    ):
        """Initialize the task manager.
        
        Args:
            base_path (str): Base path for all operations
            model (str): Model to use
            prompt_decompse_set (str): Name of the decomposition prompt set
            prompt_allocation_set (str): Name of the allocation prompt set
        """
        self.base_path = base_path
        self.model = model
        self.prompt_decompse_set = prompt_decompse_set
        self.prompt_allocation_set = prompt_allocation_set
        self.config = config or load_run_config(base_path)
        self.test_set = test_set
        self.floor_plan = normalize_floor_plan(str(floor_plan)) if floor_plan is not None else None
        self.runtime_config = {"storage": {"base_dir": str(self.config.storage_base_dir)}}
        self.instance_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
        
        # Initialize components
        self.llm = LLMHandler(self.config)
        self.file_processor = FileProcessor(base_path, config=self.config)
        self.validator = PDDLValidator(self.llm, self.file_processor, self.config)
        self.planner = PDDLPlanner(base_path, self.file_processor, self.config)
        
        # Initialize paths
        self.resources_path = str(self.config.resources_dir)
        self.logs_path = str(self.config.task_manager_runs_dir / self.instance_id)
        self.intermediate_base_path = str(self.config.storage_base_dir)
        os.makedirs(self.logs_path, exist_ok=True)
        os.makedirs(self.intermediate_base_path, exist_ok=True)
        
        # Initialize result storage
        self.decomposed_plan: List[str] = []
        self.allocated_plan: List[str] = []
        self.code_plan: List[str] = []
        self.validated_plan: List[str] = []  #PG: Added for validation
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
        self.current_validated_subtask_dir: Optional[str] = None
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

    def _get_validated_problem_file_path(self) -> Optional[str]:
        """Write a text artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        return os.path.join(self.current_task_run_dir, "07_validate/outputs")
    
    def _get_raw_problem_file_path(self) -> Optional[str]:
        """Write a text artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        return os.path.join(self.current_task_run_dir, "05_problem_generation/outputs")
    
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

    def _record_artifact(self, section: str, key: str, relative_path: str) -> None:
        """Record an artifact in the current task manifest."""
        if not self.current_task_manifest:
            return

        if "artifacts" not in self.current_task_manifest:
            self.current_task_manifest["artifacts"] = {}
        if section not in self.current_task_manifest["artifacts"]:
            self.current_task_manifest["artifacts"][section] = {}
        self.current_task_manifest["artifacts"][section][key] = relative_path

    def _replace_domain_robot_name(self, domain_content: str, real_robot_name: str, normalized_robot_name: str) -> str:
        """Replace a real robot domain token with the task-local robot token."""
        real_robot_name = real_robot_name.replace(" ", "")
        normalized_robot_name = normalized_robot_name.replace(" ", "")
        if not real_robot_name or real_robot_name == normalized_robot_name:
            return domain_content

        token_pattern = rf"(?<![A-Za-z0-9_]){re.escape(real_robot_name)}(?![A-Za-z0-9_])"
        return re.sub(token_pattern, normalized_robot_name, domain_content)

    def _persist_manifest(self) -> None:
        """Persist the current task manifest to disk."""
        if self.current_task_run_dir and self.current_task_manifest:
            self._write_json_artifact(self.config.artifact("manifest", "run_manifest.json"), self.current_task_manifest)

    def _prepare_task_run_dir(self, task_idx: int, task: str, robots: List[dict], objects_ai: str, domain_content: str) -> None:
        """Create and initialize the storage directory for the current task."""
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        date_prefix = now.strftime("%Y%m%d")
        run_sequence = None
        if self.test_set and self.floor_plan:
            self.current_task_run_dir, run_sequence = self._create_dataset_task_run_dir(task, date_prefix)
        else:
            folder_name = f"{task_idx + 1:03d}_{self._sanitize_filename(task)[:80]}_{self.instance_id}_{timestamp}"
            self.current_task_run_dir = os.path.join(self.intermediate_base_path, folder_name)
            os.makedirs(self.current_task_run_dir, exist_ok=True)
        generated_artifact_dir = self.config.artifact("generated_subtask_dir", "06_split/generated_subtask")
        validated_artifact_dir = self.config.artifact("validated_subtask_dir", "07_validate/validated_subtask")
        each_run_artifact_dir = self.config.artifact("each_run_dir", "artifacts/each_run")
        self.current_generated_subtask_dir = os.path.join(self.current_task_run_dir, generated_artifact_dir)
        self.current_validated_subtask_dir = os.path.join(self.current_task_run_dir, validated_artifact_dir)
        self.current_each_run_dir = os.path.join(self.current_task_run_dir, each_run_artifact_dir)
        self.file_processor.configure_workspace(
            subtask_path=self.current_generated_subtask_dir,
            validated_subtask_path=self.current_validated_subtask_dir,
            each_run_path=self.current_each_run_dir,
        )
        self.current_task_manifest = {
            "task_index": task_idx,
            "task": task,
            "model": self.model,
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
            task_index=task_idx,
            task=task,
            task_run_dir=self.current_task_run_dir,
            task_log_file=os.path.join(self.current_task_run_dir, self.config.artifact("llm_calls", "00_llm/llm_calls.jsonl")),
        )

        inputs = {
            "task": task,
            "robots": robots,
            "objects_ai": objects_ai,
            "model": self.model,
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

    def clean_generated_subtask_directory(self, isValidated: bool = False) -> None:
        """Clean the generated subtask directory."""
        if isValidated:
            directory = self.file_processor.validated_subtask_path
        else:
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
    
    def calculate_completion_rate(self) -> Tuple[int, int]:
        """
        
        Returns:
            Tuple[int, int]: (number of completed tasks, total number of tasks)
        """
        TC = 0
        plan_file_path = self._get_plan_file_path()
        if os.path.exists(plan_file_path):
            TC = len([f for f in os.listdir(plan_file_path) if f.endswith('_validated_plan.txt')])
        
        total_subtasks = 0
        validated_problem_file_path = self._get_validated_problem_file_path()
        if os.path.exists(validated_problem_file_path):
            total_subtasks = len([f for f in os.listdir(validated_problem_file_path) if f.endswith('_validated.pddl')])
        
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
                   min_trans_cnt_tasks: List[int], objects_ai: str,
                   bddl_file_path: Optional[str] = None):
        """Log results including BDDL file if provided."""
        # print(f"\n[DEBUG] Logging task {idx + 1}")
        # print(f"Current list lengths:")
        # print(f"- code_planpddl: {len(self.code_planpddl)}")
        # print(f"- combined_plan: {len(self.combined_plan)}")
        # print(f"- decomposed_plan: {len(self.decomposed_plan)}")
        # print(f"- allocated_plan: {len(self.allocated_plan)}")
        # print(f"- code_plan: {len(self.code_plan)}")
        # print(f"- validated_plan: {len(self.validated_plan)}")  #PG: Added for validation
        
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
            self._write_plan(log_folder, "validated_plan.py", task_result["validated_plan"] if task_result else self.validated_plan[idx])  #PG: Added for validation
            #print(f"Successfully wrote validated_plan for task {idx + 1}")
            
            # Log main information
            if task_result:
                TC = task_result["successful_subtasks"]
                total_subtasks = task_result["total_subtasks"]
            else:
                TC, total_subtasks = self.tc[idx], self.total_subtasks[idx]
            print(f"Task {idx + 1} - TC: {TC}, Total Subtasks: {total_subtasks}")


            generated_subtask_dir = task_result["generated_subtask_dir"] if task_result else self.file_processor.subtask_path
            validated_subtask_dir = task_result["validated_subtask_dir"] if task_result else self.file_processor.validated_subtask_path
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
                "validated_subtask_dir": validated_subtask_dir,
                "generated_subtasks": sorted(os.listdir(generated_subtask_dir)) if os.path.exists(generated_subtask_dir) else [],
                "validated_subtasks": sorted(os.listdir(validated_subtask_dir)) if os.path.exists(validated_subtask_dir) else [],
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
                f.write(f"\nValidatedSubtaskDir = {validated_subtask_dir}")
                f.write(f"\nValidationManifest = {artifact_map.get('validate', {}).get('manifest')}")
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
            
            #PG: Added for validation
            # Copy validated subtasks
            validated_subtask_folder = os.path.join(log_folder, "validated_subtask")
            os.makedirs(validated_subtask_folder)
            source_validated_folder = validated_subtask_dir
            for file_name in os.listdir(source_validated_folder):
                full_file_name = os.path.join(source_validated_folder, file_name)
                if os.path.isfile(full_file_name):
                    shutil.copy(full_file_name, validated_subtask_folder)


            # Add BDDL file to logs if provided
            if bddl_file_path and os.path.exists(bddl_file_path):
                bddl_content = self.file_processor.read_file(bddl_file_path)
                bddl_output_path = os.path.join(log_folder, "task.bddl")
                self.file_processor.write_file(bddl_output_path, bddl_content)
            
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
            self.validated_plan = []  #PG: Added for validation
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
                print(f"\n{'='*50}")
                print(f"Processing Task: {task}: {task_idx + 1}/{len(test_tasks)}")
                print(f"{'='*50}")
                self.current_robot_domain_names = (
                    copy.deepcopy(effective_robot_domain_name_maps[task_idx])
                    if task_idx < len(effective_robot_domain_name_maps)
                    else {}
                )
                self._prepare_task_run_dir(task_idx, task, robots, objects_ai, domain_content)
                
                # Clean generated subtask directory before starting new task
                self.clean_generated_subtask_directory()
                self.clean_generated_subtask_directory(True)  #PG: Added for validation
                
                # Generate and store decomposed plan
                decomposed_plan = self._generate_decomposed_plan(task, domain_content, robots, objects_ai)
                self.decomposed_plan.append(decomposed_plan)
                
                print("✓ Decomposed plan generated")
                #print("decomposed plan:\n", decomposed_plan)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                # Extract subtasks before deterministic allocation.
                subtasks = ParsingUtils.extract_subtasks(decomposed_plan)
                print(f"✓ Extracted {len(subtasks)} subtasks")

                pairwise_predecessors = self._generate_pairwise_predecessors(
                    task=task,
                    decomposed_plan=decomposed_plan,
                    subtasks=subtasks,
                )
                if pairwise_predecessors is None:
                    print("! Pairwise precedence extraction unavailable; using heuristic fallback")
                else:
                    print("✓ Pairwise precedence extracted")

                # Generate allaction problem files for each subtask.
                problem_pddl = self._generate_allaction_problem_files(subtasks, objects_ai)
                print("✓ Allaction problem files generated")
                
                # Validate/fix generated allaction problems before planning and allocation.
                planner_records = self._validate_and_plan()
                print("✓ Allaction validation and planning complete")

                allocated_subtasks = self._allocate_subtasks_with_cpsat(
                    subtasks=subtasks,
                    decomposed_plan=decomposed_plan,
                    problem_pddl=problem_pddl,
                    planner_records=planner_records,
                    available_robots=robots,
                    objects_ai=objects_ai,
                    preferred_predecessors=pairwise_predecessors,
                )
                self.allocated_plan.append(json.dumps(allocated_subtasks, indent=2, ensure_ascii=False))
                print("✓ CP-SAT allocation generated")
                
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

                # In v2, successful allocation of all subtasks is the task-level completion signal.
                tc, total = len(allocated_subtasks), len(subtasks)

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

    def _generate_decomposed_plan(self, task: str, domain_content: str, robots: List[dict], objects_ai: str) -> str:
        """Generate decomposed plan for a task."""
        try:
            # Read decomposition prompt file
            decompose_prompt_path = self.config.prompt_file(f"{self.prompt_decompse_set}.txt")
            decompose_prompt = self.file_processor.read_file(str(decompose_prompt_path))
            
            # Construct the prompt incrementally like the original
            prompt = f"from pddl domain file with all possible actions: \n{domain_content}\n\n"
            prompt += objects_ai
            prompt += f"\nrobots = {robots}\n\n"
            prompt += "robot initiate 'as not inaction robot '(which defaults location too)\n\n"
            prompt += decompose_prompt
            prompt += "# GENERAL TASK DECOMPOSITION \n"
            prompt += "Decompose and parallel subtasks where ever possible.\n"
            prompt += "Strictly follow the format in the examples above..\n"
            prompt += f"# Task Description: {task}"
            decompose_prompt_artifact = self.config.artifact("decompose_prompt", "01_decompose/01_decompose_prompt.txt")
            decompose_output_artifact = self.config.artifact("decompose_output", "01_decompose/02_decompose_output.txt")
            self._write_text_artifact(decompose_prompt_artifact, prompt)
            self._record_artifact("decompose", "prompt", decompose_prompt_artifact)
            
            messages = [{"role": "user", "content": prompt}]
            call_config = self.config.llm_call("decompose")
            _, text = self.llm.query_model(
                messages,
                self.model,
                frequency_penalty=call_config.get("frequency_penalty", 0.0),
            )
            self._write_text_artifact(decompose_output_artifact, text)
            self._record_artifact("decompose", "output", decompose_output_artifact)
            self._persist_manifest()
            
            return text
            
        except Exception as e:
            raise PDDLError(f"Error generating decomposed plan: {str(e)}")

    def _generate_pairwise_predecessors(
        self,
        task: str,
        decomposed_plan: str,
        subtasks: List[str],
    ) -> Optional[Dict[int, List[int]]]:
        """Use pairwise LLM judgments to infer direct subtask predecessors."""
        subtask_count = len(subtasks)
        empty_predecessors = {idx: [] for idx in range(1, subtask_count + 1)}
        precedence_output_artifact = self.config.artifact("precedence_output", "02_precedence/predecessors.json")
        precedence_manifest_artifact = self.config.artifact("precedence_manifest", "02_precedence/pairwise_manifest.json")

        if subtask_count <= 1:
            self._write_json_artifact(precedence_output_artifact, empty_predecessors)
            self._write_json_artifact(precedence_manifest_artifact, {
                "status": "skipped",
                "reason": "single_or_empty_subtask_set",
                "pairwise_records": [],
            })
            self._record_artifact("precedence", "output", precedence_output_artifact)
            self._record_artifact("precedence", "manifest", precedence_manifest_artifact)
            self._persist_manifest()
            return empty_predecessors

        records: List[Dict[str, Any]] = []
        edges: Set[Tuple[int, int]] = set()

        try:
            prompt_template_path = self.config.prompt_file("pddl_train_task_precedence_pairwise.txt")
            prompt_template = self.file_processor.read_file(str(prompt_template_path))

            for subtask_a_id in range(1, subtask_count + 1):
                for subtask_b_id in range(subtask_a_id + 1, subtask_count + 1):
                    prompt = self._render_pairwise_precedence_prompt(
                        prompt_template=prompt_template,
                        task=task,
                        decomposed_plan=decomposed_plan,
                        subtask_a_id=subtask_a_id,
                        subtask_a=subtasks[subtask_a_id - 1],
                        subtask_b_id=subtask_b_id,
                        subtask_b=subtasks[subtask_b_id - 1],
                    )
                    pair_key = f"subtask_{subtask_a_id:02d}_{subtask_b_id:02d}"
                    prompt_path = f"02_precedence/prompts/{pair_key}_prompt.txt"
                    output_path = f"02_precedence/outputs/{pair_key}_raw.txt"
                    self._write_text_artifact(prompt_path, prompt)

                    messages = [
                        {"role": "system", "content": "You are a pairwise task-order classifier for robot planning."},
                        {"role": "user", "content": prompt},
                    ]
                    call_config = self.config.llm_call("precedence")
                    _, text = self.llm.query_model(
                        messages,
                        self.model,
                        max_tokens=call_config.get("max_tokens", 700),
                        frequency_penalty=call_config.get("frequency_penalty", 0.0),
                    )
                    self._write_text_artifact(output_path, text)

                    parsed = self._parse_pairwise_precedence_response(
                        text,
                        expected_subtask_a_id=subtask_a_id,
                        expected_subtask_b_id=subtask_b_id,
                    )
                    relation = parsed["relation"]
                    if relation == "A_BEFORE_B":
                        edges.add((subtask_a_id, subtask_b_id))
                    elif relation == "B_BEFORE_A":
                        edges.add((subtask_b_id, subtask_a_id))

                    records.append({
                        "subtask_a_id": subtask_a_id,
                        "subtask_b_id": subtask_b_id,
                        "relation": relation,
                        "reason": parsed.get("reason", ""),
                        "prompt_path": prompt_path,
                        "output_path": output_path,
                        "status": "ok",
                    })

            predecessors = self._predecessors_from_edges(edges, subtask_count)
            normalized = self._normalize_predecessor_map(predecessors, subtask_count)
            if normalized is None:
                raise PDDLError("Pairwise precedence result is invalid or cyclic")

            self._write_json_artifact(precedence_output_artifact, normalized)
            self._write_json_artifact(precedence_manifest_artifact, {
                "status": "ok",
                "pairwise_records": records,
            })
            self._record_artifact("precedence", "output", precedence_output_artifact)
            self._record_artifact("precedence", "manifest", precedence_manifest_artifact)
            self._persist_manifest()
            return normalized

        except Exception as e:
            records.append({
                "status": "fallback",
                "error": str(e),
            })
            self._write_json_artifact(precedence_manifest_artifact, {
                "status": "fallback",
                "error": str(e),
                "pairwise_records": records,
            })
            self._record_artifact("precedence", "manifest", precedence_manifest_artifact)
            self._persist_manifest()
            return None

    def _render_pairwise_precedence_prompt(
        self,
        prompt_template: str,
        task: str,
        decomposed_plan: str,
        subtask_a_id: int,
        subtask_a: str,
        subtask_b_id: int,
        subtask_b: str,
    ) -> str:
        replacements = {
            "{task}": task,
            "{decomposed_plan}": decomposed_plan,
            "{subtask_a_id}": str(subtask_a_id),
            "{subtask_a}": subtask_a,
            "{subtask_b_id}": str(subtask_b_id),
            "{subtask_b}": subtask_b,
        }
        prompt = prompt_template
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def _parse_pairwise_precedence_response(
        self,
        text: str,
        expected_subtask_a_id: int,
        expected_subtask_b_id: int,
    ) -> Dict[str, Any]:
        payload = self._extract_json_object(text)
        try:
            subtask_a_id = int(payload.get("subtask_a_id"))
            subtask_b_id = int(payload.get("subtask_b_id"))
        except (TypeError, ValueError) as exc:
            raise PDDLError("Pairwise precedence response has non-numeric subtask ids") from exc

        if subtask_a_id != expected_subtask_a_id or subtask_b_id != expected_subtask_b_id:
            raise PDDLError(
                "Pairwise precedence response ids do not match "
                f"expected ({expected_subtask_a_id}, {expected_subtask_b_id})"
            )

        relation = str(payload.get("relation", "")).strip().upper()
        allowed_relations = {"A_BEFORE_B", "B_BEFORE_A", "NO_ORDER"}
        if relation not in allowed_relations:
            raise PDDLError(f"Invalid pairwise precedence relation: {relation}")

        return {
            "subtask_a_id": subtask_a_id,
            "subtask_b_id": subtask_b_id,
            "relation": relation,
            "reason": str(payload.get("reason", "")).strip(),
        }

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        stripped = text.strip()
        fenced_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', stripped, flags=re.DOTALL | re.IGNORECASE)
        if fenced_match:
            stripped = fenced_match.group(1).strip()
        else:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end < start:
                raise PDDLError("No JSON object found in LLM response")
            stripped = stripped[start:end + 1]

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PDDLError(f"Invalid JSON in LLM response: {exc}") from exc
        if not isinstance(payload, dict):
            raise PDDLError("LLM response JSON must be an object")
        return payload

    def _predecessors_from_edges(
        self,
        edges: Set[Tuple[int, int]],
        subtask_count: int,
    ) -> Dict[int, List[int]]:
        predecessors: Dict[int, List[int]] = {idx: [] for idx in range(1, subtask_count + 1)}
        for before, after in sorted(edges):
            if before == after or before < 1 or after < 1 or before > subtask_count or after > subtask_count:
                continue
            if before not in predecessors[after]:
                predecessors[after].append(before)
        return {idx: sorted(values) for idx, values in predecessors.items()}

    def _normalize_predecessor_map(
        self,
        predecessors: Dict[int, List[int]],
        subtask_count: int,
    ) -> Optional[Dict[int, List[int]]]:
        expected_ids = set(range(1, subtask_count + 1))
        try:
            received_ids = {int(task_id) for task_id in predecessors.keys()}
        except (TypeError, ValueError):
            return None
        if received_ids != expected_ids:
            return None

        normalized: Dict[int, List[int]] = {idx: [] for idx in range(1, subtask_count + 1)}
        for raw_task_id, raw_predecessors in predecessors.items():
            try:
                task_id = int(raw_task_id)
            except (TypeError, ValueError):
                return None
            if not isinstance(raw_predecessors, list):
                return None

            seen: Set[int] = set()
            for raw_predecessor_id in raw_predecessors:
                try:
                    predecessor_id = int(raw_predecessor_id)
                except (TypeError, ValueError):
                    return None
                if predecessor_id not in expected_ids or predecessor_id == task_id:
                    return None
                if predecessor_id not in seen:
                    seen.add(predecessor_id)
                    normalized[task_id].append(predecessor_id)
            normalized[task_id].sort()

        if self._has_precedence_cycle(normalized, subtask_count):
            return None
        return normalized

    def _has_precedence_cycle(
        self,
        predecessors: Dict[int, List[int]],
        subtask_count: int,
    ) -> bool:
        successors: Dict[int, List[int]] = {idx: [] for idx in range(1, subtask_count + 1)}
        for after, before_ids in predecessors.items():
            for before in before_ids:
                successors.setdefault(before, []).append(after)

        state: Dict[int, int] = {idx: 0 for idx in range(1, subtask_count + 1)}

        def visit(task_id: int) -> bool:
            if state[task_id] == 1:
                return True
            if state[task_id] == 2:
                return False
            state[task_id] = 1
            for successor_id in successors.get(task_id, []):
                if visit(successor_id):
                    return True
            state[task_id] = 2
            return False

        return any(visit(task_id) for task_id in range(1, subtask_count + 1) if state[task_id] == 0)

    def _generate_allaction_problem_files(
        self,
        subtasks: List[str],
        objects_ai: str,
    ) -> List[str]:
        """Generate allactionrobot problem files for each subtask."""
        subtasks_index_artifact = self.config.artifact("subtasks_index", "04_problem_files/03_subtasks.json")
        generated_problem_files_artifact = self.config.artifact("generated_problem_files", "04_problem_files/04_generated_problem_files.json")

        subtask_entries = []
        for idx, subtask in enumerate(subtasks, start=1):
            relative_path = f"04_problem_files/subtasks/subtask_{idx:02d}.txt"
            self._write_text_artifact(relative_path, subtask)
            subtask_entries.append({"index": idx, "path": relative_path})
        self._write_json_artifact(subtasks_index_artifact, subtask_entries)
        self._record_artifact("problem_files", "subtasks_index", subtasks_index_artifact)

        problem_pddl = self.allaction_problemextracting(
            subtasks=subtasks,
            llm=self.llm,
            model=self.model,
            file_processor=self.file_processor,
            objects_ai=objects_ai,
            prompt_allocation_set=self.prompt_allocation_set,
        )
        self._write_json_artifact(
            generated_problem_files_artifact,
            [{"index": idx + 1, "content": content} for idx, content in enumerate(problem_pddl)],
        )
        self._record_artifact("problem_files", "generated_problem_files", generated_problem_files_artifact)
        self._persist_manifest()
        return problem_pddl

    def allaction_problemextracting(
        self,
        subtasks: List[str],
        llm: 'LLMHandler',
        model: str,
        file_processor: 'FileProcessor',
        objects_ai: str,
        prompt_allocation_set: str,
    ) -> List[str]:
        """Generate PDDL problem files against the allactionrobot domain."""
        problem_pddl: List[str] = []
        domain_content = file_processor.read_file(str(self.config.allaction_domain_path())) or ""
        problem_fileexamplepath = self.config.prompt_file(f"{prompt_allocation_set}_problem.txt")
        problem_examplecontent = file_processor.read_file(str(problem_fileexamplepath)) or ""

        for subtask_idx, subtask in enumerate(subtasks, start=1):
            prompt = (
                "\n" + problem_examplecontent +
                " Finish the tasks like example\n"
                "Subtask examination from action perspective:" + subtask +
                "\nDomain file content:" + domain_content +
                "\n based on the objects available for potential usage below." + objects_ai +
                "\nTask description: generate the problem file. Based on the objects above, "
                "the domain file preconditions, actions, and subtask examination. "
                "IMPORTANT the problem must use (:domain allactionrobot). "
                "IMPORTANT use robot1 as the robot object in the generated problem. "
                "IMPORTANT the robot initiates strictly as not inaction and robot "
                "(which includes location)\n"
                "#IMPORTANT, strictly follow the structure, stop generating after the Problem file generation is done."
            )
            prompt_path = f"05_problem_generation/prompts/subtask_{subtask_idx:02d}_prompt.txt"
            output_path0 = f"05_problem_generation/outputs/subtask_{subtask_idx:02d}_problem.raw.txt"
            output_path1 = f"05_problem_generation/outputs/subtask_{subtask_idx:02d}_problem.pddl"
            self._write_text_artifact(prompt_path, prompt)

            messages = [
                {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                {"role": "user", "content": prompt},
            ]
            call_config = self.config.llm_call("problem_generation")
            _, text = llm.query_model(
                messages,
                model,
                frequency_penalty=call_config.get("frequency_penalty", 0.4),
            )

            extracted_problem = self._force_problem_domain(
                self._extract_pddl_problem_block(text),
                "allactionrobot",
            )
            self._write_text_artifact(output_path0, text)
            self._write_text_artifact(output_path1, extracted_problem)
            problem_pddl.append(extracted_problem)

        return problem_pddl

    def _force_problem_domain(self, problem_content: str, domain_name: str) -> str:
        """Ensure a generated problem points to the desired PDDL domain."""
        domain_pattern = re.compile(r'\(\s*:domain\s+[^)\s]+\s*\)', re.IGNORECASE)
        replacement = f"(:domain {domain_name})"
        if domain_pattern.search(problem_content):
            return domain_pattern.sub(replacement, problem_content, count=1)

        define_match = re.search(r'\(define\s+\(problem\s+[^)]+\)', problem_content, re.IGNORECASE)
        if not define_match:
            return problem_content
        insert_at = define_match.end()
        return problem_content[:insert_at] + f"\n  {replacement}" + problem_content[insert_at:]

    def _domain_file_for_problem_domain(self, domain_name: str) -> Optional[str]:
        """Resolve the domain file that should be used for a generated problem."""
        if not domain_name:
            return None

        if domain_name == "allactionrobot":
            domain_path = str(self.config.allaction_domain_path())
            return domain_path if os.path.isfile(domain_path) else None

        real_robot_name = self.current_robot_domain_names.get(domain_name, domain_name)
        domain_path = str(self.config.robot_domain_path(f"{real_robot_name}.pddl"))
        return domain_path if os.path.isfile(domain_path) else None

    def run_allaction_planners(self) -> List[Dict[str, Any]]:
        """Run downward on all generated allaction problem files."""
        try:
            planner_path = str(self.config.planner_executable)
            raw_problem_file_path = self._get_raw_problem_file_path()
            if not raw_problem_file_path or not os.path.exists(raw_problem_file_path):
                print("no problem_file")
                return []

            plan_file_path = self._get_plan_file_path()
            os.makedirs(plan_file_path, exist_ok=True)
            problem_files = sorted(f for f in os.listdir(raw_problem_file_path) if f.endswith('.pddl'))
            domain_file = str(self.config.allaction_domain_path())
            planner_records: List[Dict[str, Any]] = []
            plan_output_files: List[str] = []

            for problem_file in problem_files:
                problem_file_full = os.path.join(raw_problem_file_path, problem_file)
                safe_name = self._sanitize_filename(problem_file.replace(".pddl", ""))
                output_file = os.path.join(plan_file_path, f"{safe_name}_plan.txt")
                command = [
                    planner_path,
                    "--plan-file",
                    output_file,
                    "--alias",
                    str(self.config.get("planner", "alias", "seq-opt-lmcut")),
                    domain_file,
                    problem_file_full,
                ]
                started_at = time.time()

                try:
                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=int(self.config.get("planner", "timeout_seconds", 300)),
                    )

                    command_path = f"08_planner/commands/{safe_name}_command.txt"
                    stdout_path = f"08_planner/stdout/{safe_name}_stdout.txt"
                    stderr_path = f"08_planner/stderr/{safe_name}_stderr.txt"
                    self._write_text_artifact(command_path, " ".join(command))
                    self._write_text_artifact(stdout_path, result.stdout)
                    self._write_text_artifact(stderr_path, result.stderr)
                    if os.path.exists(output_file):
                        plan_output_files.append(output_file)
                    planner_records.append({
                        "problem_file": problem_file,
                        "domain_file": domain_file,
                        "command_path": command_path,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                        "return_code": result.returncode,
                        "duration_seconds": round(time.time() - started_at, 3),
                        "compatibility_output": output_file,
                    })

                    if result.stderr:
                        print(f"Warnings/Errors for {problem_file}:", result.stderr)

                except subprocess.TimeoutExpired:
                    print(f"Planner timed out for {problem_file}")
                    planner_records.append({
                        "problem_file": problem_file,
                        "status": "timeout",
                    })
                except Exception as e:
                    print(f"Error processing file {problem_file}: {str(e)}")
                    planner_records.append({
                        "problem_file": problem_file,
                        "status": "error",
                        "error": str(e),
                    })

            planner_manifest_path = self.config.artifact("planner_manifest", "08_planner/planner_manifest.json")
            self._write_json_artifact(planner_manifest_path, planner_records)
            self._record_artifact("planner", "manifest", planner_manifest_path)
            self.current_task_manifest.setdefault("planner", {})["plan_output_files"] = plan_output_files
            self._persist_manifest()
            return planner_records

        except Exception as e:
            print(f"Error in run_allaction_planners: {str(e)}")
            raise

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
        """Build subtask requirements from allaction plans, solve CP-SAT, and write subtasks.json."""
        requirements = self._build_subtask_requirements(
            subtasks=subtasks,
            decomposed_plan=decomposed_plan,
            problem_pddl=problem_pddl,
            planner_records=planner_records,
            objects_ai=objects_ai,
            preferred_predecessors=preferred_predecessors,
        )
        assignments = self._solve_subtask_assignment(requirements, available_robots)

        output: List[Dict[str, Any]] = []
        for req in requirements:
            assigned_robot = assignments[req["subtask_id"]]["robot_name"]
            output.append({
                "subtask_id": req["subtask_id"],
                "name": req["name"],
                "predecessor_ids": req["predecessor_ids"],
                "required_skills": req["required_skills"],
                "min_mass_capacity": req["min_mass_capacity"],
                "assigned_robot": assigned_robot,
            })

        allocate_subtasks_artifact = self.config.artifact("allocate_subtasks", "02_allocate/subtasks.json")
        self._write_json_artifact(allocate_subtasks_artifact, output)
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
        records_by_subtask = {
            subtask_id: record
            for record in planner_records
            for subtask_id in [self._subtask_id_from_filename(record.get("problem_file", ""))]
            if subtask_id is not None
        }
        object_masses = self._parse_object_masses(objects_ai)
        predecessors = (
            self._normalize_predecessor_map(preferred_predecessors, len(subtasks))
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
            if not plan_path or not os.path.exists(plan_path):
                raise PDDLError(f"No allaction plan file for subtask {subtask_id}: {plan_path}")

            plan_text = self.file_processor.read_file(plan_path)
            actions_in_plan = self._parse_plan_actions(plan_text)
            if not actions_in_plan:
                raise PDDLError(f"No plan actions found for subtask {subtask_id}: {plan_path}")

            required_skills = self._extract_required_skills(actions_in_plan)
            min_mass_capacity = self._calculate_pickup_mass_peak(actions_in_plan, object_masses)
            requirements.append({
                "subtask_id": subtask_id,
                "name": self._extract_subtask_name(subtask, subtask_id),
                "predecessor_ids": predecessors.get(subtask_id, []),
                "required_skills": required_skills,
                "min_mass_capacity": round(min_mass_capacity, 6),
                "duration": max(1, len(actions_in_plan)),
                "plan_path": plan_path,
            })

        return requirements

    def _solve_subtask_assignment(
        self,
        requirements: List[Dict[str, Any]],
        available_robots: List[dict],
    ) -> Dict[int, Dict[str, Any]]:
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:
            raise PDDLError(
                "OR-Tools is required for CP-SAT allocation in pddlrun_llmseparate_v2.py. "
                "Install it with `pip install ortools` in the active environment."
            ) from exc

        if not requirements:
            return {}
        if not available_robots:
            raise PDDLError("No robots available for CP-SAT allocation")

        task_ids = [req["subtask_id"] for req in requirements]
        task_index_by_id = {task_id: idx for idx, task_id in enumerate(task_ids)}
        robot_count = len(available_robots)
        horizon = sum(int(req["duration"]) for req in requirements)
        horizon = max(horizon, 1)

        feasible: Dict[Tuple[int, int], bool] = {}
        for task_idx, req in enumerate(requirements):
            required = {self._canonical_skill_name(skill) for skill in req["required_skills"]}
            for robot_idx, robot in enumerate(available_robots):
                robot_skills = {self._canonical_skill_name(skill) for skill in robot.get("skills", [])}
                mass_capacity = float(robot.get("mass_capacity", robot.get("mass", 0)) or 0)
                feasible[(task_idx, robot_idx)] = required.issubset(robot_skills) and mass_capacity >= req["min_mass_capacity"]

            if not any(feasible[(task_idx, robot_idx)] for robot_idx in range(robot_count)):
                raise PDDLError(
                    f"No feasible robot for subtask {req['subtask_id']} "
                    f"skills={req['required_skills']} min_mass_capacity={req['min_mass_capacity']}"
                )

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
        intervals_by_robot: Dict[int, List[Any]] = {robot_idx: [] for robot_idx in range(robot_count)}

        for task_idx, req in enumerate(requirements):
            duration = int(req["duration"])
            model.Add(ends[task_idx] == starts[task_idx] + duration)
            choices = []
            for robot_idx in range(robot_count):
                var = model.NewBoolVar(f"x_{req['subtask_id']}_{robot_idx + 1}")
                assigned[(task_idx, robot_idx)] = var
                if not feasible[(task_idx, robot_idx)]:
                    model.Add(var == 0)
                choices.append(var)
                intervals_by_robot[robot_idx].append(
                    model.NewOptionalIntervalVar(
                        starts[task_idx],
                        duration,
                        ends[task_idx],
                        var,
                        f"interval_{req['subtask_id']}_{robot_idx + 1}",
                    )
                )
            model.Add(sum(choices) == 1)

        for robot_idx in range(robot_count):
            model.AddNoOverlap(intervals_by_robot[robot_idx])

        for req in requirements:
            task_idx = task_index_by_id[req["subtask_id"]]
            for predecessor_id in req["predecessor_ids"]:
                if predecessor_id not in task_index_by_id:
                    continue
                predecessor_idx = task_index_by_id[predecessor_id]
                model.Add(ends[predecessor_idx] <= starts[task_idx])

        makespan = model.NewIntVar(0, horizon, "makespan")
        model.AddMaxEquality(makespan, ends)
        used_robots = []
        for robot_idx in range(robot_count):
            used = model.NewBoolVar(f"used_robot_{robot_idx + 1}")
            robot_assignments = [assigned[(task_idx, robot_idx)] for task_idx in range(len(requirements))]
            for var in robot_assignments:
                model.Add(var <= used)
            model.Add(sum(robot_assignments) >= used)
            used_robots.append(used)

        tie_break_robot_order = sum(
            (robot_idx + 1) * assigned[(task_idx, robot_idx)]
            for task_idx in range(len(requirements))
            for robot_idx in range(robot_count)
        )
        used_weight = len(requirements) * robot_count + 1
        makespan_weight = used_weight * (robot_count + 1)
        model.Minimize(makespan * makespan_weight + sum(used_robots) * used_weight + tie_break_robot_order)

        solver = cp_model.CpSolver()
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise PDDLError(f"CP-SAT allocation failed with status {solver.StatusName(status)}")

        assignments: Dict[int, Dict[str, Any]] = {}
        for task_idx, req in enumerate(requirements):
            chosen_robot_idx = None
            for robot_idx in range(robot_count):
                if solver.Value(assigned[(task_idx, robot_idx)]):
                    chosen_robot_idx = robot_idx
                    break
            if chosen_robot_idx is None:
                raise PDDLError(f"CP-SAT did not assign subtask {req['subtask_id']}")
            robot = available_robots[chosen_robot_idx]
            assignments[req["subtask_id"]] = {
                "robot_name": robot.get("name", f"robot{chosen_robot_idx + 1}"),
                "start": solver.Value(starts[task_idx]),
                "end": solver.Value(ends[task_idx]),
            }

        return assignments

    def _subtask_id_from_filename(self, filename: str) -> Optional[int]:
        match = re.search(r'subtask[_-](\d+)', filename, re.IGNORECASE)
        return int(match.group(1)) if match else None

    def _extract_subtask_name(self, subtask: str, subtask_id: int) -> str:
        for line in subtask.splitlines():
            match = _match_subtask_header_line(line, allow_bare=True)
            if match:
                normalized = _normalize_subtask_header_line(line, allow_bare=True)
                normalized = re.sub(r'^#+\s*Sub\s*Task\s*\d+\s*[:.\-]?\s*', '', normalized, flags=re.IGNORECASE)
                return normalized.strip(" *#:\t") or f"Subtask {subtask_id}"
        first_line = next((line.strip() for line in subtask.splitlines() if line.strip()), "")
        return first_line.strip(" *#:\t") or f"Subtask {subtask_id}"

    def _parse_plan_actions(self, plan_text: str) -> List[Dict[str, Any]]:
        actions_in_plan: List[Dict[str, Any]] = []
        for line in plan_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(";"):
                continue
            if stripped.startswith("(") and stripped.endswith(")"):
                inner = stripped[1:-1].strip()
            else:
                inner = re.sub(r'\s*\(\d+\)\s*$', '', stripped).strip()
            if not inner:
                continue
            parts = inner.split()
            if not parts:
                continue
            canonical_skill = self._canonical_skill_name(parts[0])
            actions_in_plan.append({
                "raw": stripped,
                "skill": canonical_skill,
                "args": parts[1:],
            })
        return actions_in_plan

    def _extract_required_skills(self, actions_in_plan: List[Dict[str, Any]]) -> List[str]:
        required_skills: List[str] = []
        seen: Set[str] = set()
        for action in actions_in_plan:
            skill = action["skill"]
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
        release_actions = {"PutObject", "DropHandObject", "ThrowObject"}

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
            elif skill in release_actions and len(args) >= 2:
                object_key = self._object_key(args[1])
                if object_key in held:
                    current_mass -= held.pop(object_key)

        return max(0.0, peak_mass)

    def _parse_object_masses(self, objects_ai: str) -> Dict[str, float]:
        if not objects_ai:
            return {}
        text = objects_ai.strip()
        if "=" in text:
            text = text.split("=", 1)[1].strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return {}

        try:
            parsed = ast.literal_eval(text[start:end + 1])
        except (SyntaxError, ValueError):
            return {}

        object_masses: Dict[str, float] = {}
        if not isinstance(parsed, list):
            return object_masses
        for item in parsed:
            if not isinstance(item, dict) or "name" not in item:
                continue
            mass_value = item.get("mass", item.get("mass_capacity", 0.0))
            try:
                mass = float(mass_value or 0.0)
            except (TypeError, ValueError):
                mass = 0.0
            key = self._object_key(str(item["name"]))
            object_masses[key] = max(object_masses.get(key, 0.0), mass)
        return object_masses

    def _canonical_skill_name(self, action_name: str) -> str:
        key = re.sub(r'[^a-z0-9]', '', str(action_name).lower())
        aliases = {
            "gotoobject": "GoToObject",
            "openobject": "OpenObject",
            "closeobject": "CloseObject",
            "breakobject": "BreakObject",
            "sliceobject": "SliceObject",
            "switchon": "SwitchOn",
            "switchoff": "SwitchOff",
            "pickupobject": "PickupObject",
            "putobject": "PutObject",
            "cleanobject": "CleanObject",
            "runmicrowave": "RunMicrowave",
            "runcoffeemachine": "RunCoffeeMachine",
            "runtoaster": "RunToaster",
            "drophandobject": "DropHandObject",
            "throwobject": "ThrowObject",
            "pushobject": "PushObject",
            "pullobject": "PullObject",
        }
        aliases.update(SPECIAL_TASK_SKILL_ALIASES)
        return aliases.get(key, str(action_name).strip())

    def _object_key(self, value: str) -> str:
        return re.sub(r'[^a-z0-9]', '', str(value).lower())

    def _infer_predecessors(
        self,
        decomposed_plan: str,
        problem_pddl: List[str],
    ) -> Dict[int, List[int]]:
        edges = self._infer_text_precedence_edges(decomposed_plan)

        init_facts_by_subtask: Dict[int, Set[str]] = {}
        goal_facts_by_subtask: Dict[int, Set[str]] = {}
        for idx, problem in enumerate(problem_pddl, start=1):
            init_facts_by_subtask[idx] = self._extract_pddl_facts(problem, "init")
            goal_facts_by_subtask[idx] = self._extract_pddl_facts(problem, "goal")

        for producer_idx, goal_facts in goal_facts_by_subtask.items():
            for consumer_idx, init_facts in init_facts_by_subtask.items():
                if producer_idx == consumer_idx:
                    continue
                if goal_facts and goal_facts.intersection(init_facts):
                    edges.add((producer_idx, consumer_idx))

        predecessors: Dict[int, List[int]] = {idx: [] for idx in range(1, len(problem_pddl) + 1)}
        for before, after in edges:
            if before == after or after not in predecessors:
                continue
            if before not in predecessors[after]:
                predecessors[after].append(before)

        return {idx: sorted(values) for idx, values in predecessors.items()}

    def _infer_text_precedence_edges(self, decomposed_plan: str) -> Set[Tuple[int, int]]:
        edges: Set[Tuple[int, int]] = set()
        sentences = re.split(r'(?<=[.!?])\s+|\n+', decomposed_plan)
        for sentence in sentences:
            lowered = sentence.lower()
            if "parallel" in lowered or "independent" in lowered:
                continue
            subtask_ids = [int(value) for value in re.findall(r'\bsub\s*task\s*(\d+)\b', sentence, flags=re.IGNORECASE)]
            if len(subtask_ids) < 2:
                continue

            if " after " in lowered:
                after_match = re.search(
                    r'sub\s*task\s*(\d+).*?\bafter\b.*?sub\s*task\s*(\d+)',
                    sentence,
                    flags=re.IGNORECASE,
                )
                if after_match:
                    edges.add((int(after_match.group(2)), int(after_match.group(1))))
                    continue

            if " depends " in lowered or " depends on " in lowered:
                depends_match = re.search(
                    r'sub\s*task\s*(\d+).*?\bdepends(?:\s+on)?\b.*?sub\s*task\s*(\d+)',
                    sentence,
                    flags=re.IGNORECASE,
                )
                if depends_match:
                    edges.add((int(depends_match.group(2)), int(depends_match.group(1))))
                    continue

            if " follows " in lowered or " following " in lowered:
                follows_match = re.search(
                    r'sub\s*task\s*(\d+).*?\b(?:follows?|following)\b.*?sub\s*task\s*(\d+)',
                    sentence,
                    flags=re.IGNORECASE,
                )
                if follows_match:
                    edges.add((int(follows_match.group(2)), int(follows_match.group(1))))
                    continue

            if any(marker in lowered for marker in (" then ", " before ", " sequential")):
                for before, after in zip(subtask_ids, subtask_ids[1:]):
                    edges.add((before, after))
        return edges

    def _extract_pddl_facts(self, pddl_content: str, section_name: str) -> Set[str]:
        section = self._extract_pddl_section(pddl_content, section_name)
        if not section:
            return set()
        section = re.sub(r';.*', '', section)
        section = re.sub(r'\(\s*not\s+\([^()]*\)\s*\)', '', section, flags=re.IGNORECASE)

        facts: Set[str] = set()
        for match in re.finditer(r'\(\s*([A-Za-z][A-Za-z0-9_-]*)\s+([^()]*)\)', section):
            predicate = match.group(1)
            if predicate.lower() in {"and", "not"}:
                continue
            args = [arg for arg in match.group(2).split() if arg and not arg.startswith("-")]
            if not args:
                continue
            fact_key = self._object_key(predicate) + ":" + ",".join(self._object_key(arg) for arg in args)
            facts.add(fact_key)
        return facts

    def _extract_pddl_section(self, pddl_content: str, section_name: str) -> str:
        match = re.search(r'\(\s*:' + re.escape(section_name) + r'\b', pddl_content, flags=re.IGNORECASE)
        if not match:
            return ""

        start = match.start()
        depth = 0
        for idx in range(start, len(pddl_content)):
            char = pddl_content[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return pddl_content[start:idx + 1]
        return pddl_content[start:]
    
    def _generate_allocation_plan(self, decomposed_plan: str, robots: List[dict], objects_ai: str) -> str:
        """Generate allocation plan for decomposed tasks.
        
        """
        try:
            # Read allocation prompt file
            prompt_file = self.config.prompt_file(f"{self.prompt_allocation_set}_solution.txt")
            with open(prompt_file, "r", encoding="utf-8") as allocated_prompt_file:
                allocated_prompt = allocated_prompt_file.read()
            
            # Build prompt incrementally like the original
            prompt = "\n"
            prompt += allocated_prompt
            prompt += decomposed_plan
            prompt += f"\n# TASK ALLOCATION"
            prompt += f"\n# Scenario: There are {len(robots)} robots available. The task should be performed using the minimum number of robots necessary. Robot should be assigned to subtasks that match its skills and mass capacity. Using your reasoning come up with a solution to satisfy all constraints."
            prompt += f"\n\nrobots = {robots}"
            prompt += f"\n{objects_ai}"
            prompt += f"\n\n# IMPORTANT: The AI should ensure that the robots assigned to the tasks have all the necessary skills to perform the tasks. IMPORTANT: Determine whether the subtasks must be performed sequentially or in parallel, or a combination of both and allocate robots based on availability. "
            prompt += f"\n# SOLUTION\n"
            prompt += f"\n# Additional Output Rules:"
            prompt += f"\n# - Only assign a robot if it has every required skill."
            prompt += f"\n{SPECIAL_TASK_SKILL_PROMPT_RULE}"
            prompt += f"\n# - If multiple robots satisfy all constraints equally, choose the robot with the smallest robot number/name order."
            prompt += f"\n# - Only mention mass capacity if it prevents assignment."
            prompt += f"\n# - The SOLUTION must strictly follow the concise reasoning style shown in the examples."
            prompt += f"\n# - For the **Sequence of Operations** part: if two or more subtasks can be executed in parallel (i.e., they are independent), they MUST be placed on the same line, separated by a semicolon and no newline. "
            prompt += f"\n#   Example correct format: Subtask 1: Robot 1;Subtask 2: Robot 2;"
            prompt += f"\n#   Sequential subtasks that depend on others should appear on their own new line."
            prompt += f"\n# - End with one final machine-readable block headed exactly '# Sequence of Operations:'."
            prompt += f"\n# - Every assignment in that final block must use numeric subtask and robot ids, e.g. 'Subtask 1: Robot 2;'."
            prompt += f"\n# - Do not use placeholders or non-numeric assignments such as 'Subtask;Robot;', 'Subtask A', 'Robot ?', or 'Robot A'."
            prompt += f"\n# - Do not output self-corrections or extra explanation after the final '# Sequence of Operations:' block.\n"
            allocate_prompt_artifact = self.config.artifact("allocate_prompt", "02_allocate/01_allocate_prompt.txt")
            allocate_output_artifact = self.config.artifact("allocate_output", "02_allocate/02_allocate_output.txt")
            self._write_text_artifact(allocate_prompt_artifact, prompt)
            self._record_artifact("allocate", "prompt", allocate_prompt_artifact)
            
            messages = [{"role": "user", "content": prompt}]
            call_config = self.config.llm_call("allocate")
            _, text = self.llm.query_model(
                messages,
                self.model,
                frequency_penalty=call_config.get("frequency_penalty", 0.69),
            )
            self._write_text_artifact(allocate_output_artifact, text)
            self._record_artifact("allocate", "output", allocate_output_artifact)
            self._persist_manifest()
            
            return text
            
        except Exception as e:
            raise PDDLError(f"Error generating allocation plan: {str(e)}")

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
                code_prompt = code_prompt_file.read()
            
            # Build base prompt once
            base_prompt = " finish the problem content summary strictly following the example format"
            base_prompt += "\n\n" + code_prompt + "\n\n"
            
            code_plan = []
            # Process each plan
            for i, (plan, solution) in enumerate(zip(decomposed_plans, allocated_plans)):
                # Build prompt for this plan
                prompt = base_prompt + plan
                prompt += f"\n# TASK ALLOCATION"
                prompt += f"\n\nrobots = {available_robots[i]}"
                prompt += solution
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
    ) -> List[str]:
        """Generate PDDL problem files from subtasks and robot assignments.

        Args:
            subtasks: List of subtask text
            robot_assignments: Dict mapping subtask index to robot number
            objects_ai: AI objects description

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

        problem_pddl = self.problemextracting(
            subtasks=subtasks,
            robot_assignments=robot_assignments,
            llm=self.llm,
            model=self.model,
            file_processor=self.file_processor,
            objects_ai=objects_ai,
            prompt_allocation_set=self.prompt_allocation_set
        )
        self._write_json_artifact(
            generated_problem_files_artifact,
            [{"index": idx + 1, "content": content} for idx, content in enumerate(problem_pddl)]
        )
        self._record_artifact("problem_files", "generated_problem_files", generated_problem_files_artifact)
        self._persist_manifest()

        return problem_pddl
    

    def problemextracting(
            self,
            subtasks: List[str],
            robot_assignments: Dict[int, int],
            llm: 'LLMHandler',
            model: str,
            file_processor: 'FileProcessor',
            objects_ai: str,
            prompt_allocation_set: str
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

        Returns:
            List[str]: Generated PDDL problem files
        """
        problem_pddl: List[str] = []

        for subtask_idx, subtask in enumerate(subtasks, start=1):
            robot_num = robot_assignments.get(subtask_idx, 1)
            normalized_robot_name = f"robot{robot_num}"
            real_robot_name = self.current_robot_domain_names.get(normalized_robot_name, normalized_robot_name)
            robotassignnumber = f"{real_robot_name}.pddl"
            domain_path = str(self.config.robot_domain_path(robotassignnumber))

            domain_content = file_processor.read_file(domain_path) or ""
            if not domain_content:
                print(f"Domain file not found or empty: {domain_path}")
                continue
            domain_content = self._replace_domain_robot_name(
                domain_content,
                real_robot_name,
                normalized_robot_name,
            )

            problem_fileexamplepath = self.config.prompt_file(f"{prompt_allocation_set}_problem.txt")
            problem_examplecontent = file_processor.read_file(str(problem_fileexamplepath)) or ""

            prompt = (
                "\n" + problem_examplecontent +
                " Finish the tasks like example\n"
                "Subtask examination from action perspective:" + subtask +
                "\nDomain file content:" + domain_content +
                "\n based on the objects available for potential usage below." + objects_ai +
                "\nTask description: generate the problem file. Based on the objects above, "
                "the domain file preconditions, actions, and subtask examination. "
                "IMPORTANT the robot initiates strictly as not inaction and robot "
                "(which includes location)\n"
                "#IMPORTANT, strictly follow the structure, stop generating after the Problem file generation is done."
            )
            prompt_path = f"05_problem_generation/prompts/subtask_{subtask_idx:02d}_prompt.txt"
            output_path0 = f"05_problem_generation/outputs/subtask_{subtask_idx:02d}_problem.raw.txt"
            output_path1 = f"05_problem_generation/outputs/subtask_{subtask_idx:02d}_problem.pddl"
            self._write_text_artifact(prompt_path, prompt)

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
            self._write_text_artifact(output_path0, text)
            self._write_text_artifact(output_path1, extracted_problem)
            problem_pddl.append(extracted_problem)

        return problem_pddl

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

        in_problem = False

        lines = text[start_idx:].split('\n')

        problem_lines = []
        for line in lines:
            if not in_problem:
                if '(define (problem' in line:
                    in_problem = True
                    problem_lines.append(line)
                else:
                    continue
            else:
                striped = line.strip()
                if len(striped) == 0:
                    break

                if '#' in line or '```' in line:
                    break

                problem_lines.append(line)

        if not problem_lines or problem_lines[-1].strip() != ')':
            return text

        return '\n'.join(problem_lines)

    def _validate_and_plan(self) -> List[Dict[str, Any]]:
        """Validate and plan all problem files."""
        try:
            # First run LLM validator
            #print("Running LLM validator...")
            self.run_llmvalidator()
            #input("Press Enter to continue")
            # Wait for validation to complete
            #print("Waiting 50 seconds for validation to complete...")
            #time.sleep(50)
            
            # Then run planners
            #print("Running planners...")
            return self.run_planners()
            #input("Press Enter to continue")
            
        except Exception as e:
            raise PDDLError(f"Error in validation and planning: {str(e)}")

    def run_llmvalidator(self) -> List[Dict[str, Any]]:
        """Run LLM validation on problem files."""
        try:
            raw_problem_file_path = self._get_raw_problem_file_path()
            if not raw_problem_file_path or not os.path.exists(raw_problem_file_path):
                print("no raw problem_file")
                return []

            problem_files = sorted(f for f in os.listdir(raw_problem_file_path) if f.endswith('.pddl'))
            validation_records: List[Dict[str, Any]] = []
            validated_problem_files: List[str] = []

            for problem_file in problem_files:
                try:
                    problem_file_full = os.path.join(raw_problem_file_path, problem_file)
                    domain_name = self.file_processor.extract_domain_name(problem_file_full)
                    if not domain_name:
                        print(f"No domain specified in {problem_file}")
                        validation_records.append({
                            "problem_file": problem_file,
                            "status": "skipped",
                            "error": "No domain specified",
                        })
                        continue
                    
                    domain_file = self._domain_file_for_problem_domain(domain_name)
                    if not domain_file:
                        print(f"No domain file found for domain {domain_name}")
                        validation_records.append({
                            "problem_file": problem_file,
                            "domain_name": domain_name,
                            "status": "skipped",
                            "error": f"No domain file found for domain {domain_name}",
                        })
                        continue

                    domain_content = self.file_processor.read_file(domain_file)
                    problem_content = self.file_processor.read_file(problem_file_full)

                    prompt = (f"Domain Description:\n"
                            f"{domain_content}\n\n"
                            f"Problem Description:\n"
                            f"{problem_content}\n\n"
                            "Validate the preconditions in problem file to ensure all precondition listed object "
                            "is included and also in domain file, and go over structure to check the parenthesis "
                            "and syntext. Check and return only the validated problem file.")
                    safe_name = self._sanitize_filename(problem_file.replace(".pddl", ""))
                    input_path = f"07_validate/inputs/{safe_name}_input.pddl"
                    prompt_path = f"07_validate/prompts/{safe_name}_prompt.txt"
                    output_path0 = f"07_validate/outputs/{safe_name}_validated.raw.txt"
                    output_path1 = f"07_validate/outputs/{safe_name}_validated.pddl"
                    self._write_text_artifact(input_path, problem_content)
                    self._write_text_artifact(prompt_path, prompt)
                
                    messages = [{"role": "system", "content": "You are a Robot PDDL problem Expert"},
                            {"role": "user", "content": prompt}]
                    call_config = self.config.llm_call("llm_validator")
                    _, text = self.llm.query_model(
                        messages,
                        self.model,
                        frequency_penalty=call_config.get("frequency_penalty", 0.4),
                    )

                    extracted_problem = self._extract_pddl_problem_block(text)
                    extracted_problem = self._force_problem_domain(extracted_problem, domain_name)
                    self._write_text_artifact(output_path0, text)
                    self._write_text_artifact(output_path1, extracted_problem)
                    validated_full_path = os.path.join(
                        self.current_task_run_dir,
                        output_path1,
                    ) if self.current_task_run_dir else output_path1
                    validated_problem_files.append(validated_full_path)
                    validation_records.append({
                        "problem_file": problem_file,
                        "domain_name": domain_name,
                        "domain_file": domain_file,
                        "input_path": input_path,
                        "prompt_path": prompt_path,
                        "raw_output_path": output_path0,
                        "validated_problem_path": output_path1,
                        "status": "validated",
                    })
                    
                except Exception as e:
                    print(f"Error processing file {problem_file}: {str(e)}")
                    validation_records.append({
                        "problem_file": problem_file,
                        "status": "error",
                        "error": str(e),
                    })
                    continue

            validation_manifest_path = self.config.artifact("validation_manifest", "07_validate/validation_manifest.json")
            self._write_json_artifact(validation_manifest_path, validation_records)
            self._record_artifact("validate", "manifest", validation_manifest_path)
            self.current_task_manifest.setdefault("validate", {})["validated_problem_files"] = validated_problem_files
            self._persist_manifest()
            return validation_records
            
                    
        except Exception as e:
            print(f"Error in run_llmvalidator: {str(e)}")
            raise

    def run_planners(self) -> List[Dict[str, Any]]:
        """Run PDDL planners on problem files."""
        try:
            planner_path = str(self.config.planner_executable)
            validated_problem_file_path = self._get_validated_problem_file_path()
            if not validated_problem_file_path or not os.path.exists(validated_problem_file_path):
                print("no problem_file")
                return []
            plan_file_path = self._get_plan_file_path()
            os.makedirs(plan_file_path, exist_ok=True)
            problem_files = sorted(f for f in os.listdir(validated_problem_file_path) if f.endswith('.pddl'))  #PG: Changed to validated_subtask_path
            planner_records: List[Dict[str, Any]] = []
            plan_output_files: List[str] = []
            for problem_file in problem_files:
                try:
                    problem_file_full = os.path.join(validated_problem_file_path, problem_file) #PG: Changed to validated_subtask_path
                    domain_name = self.file_processor.extract_domain_name(problem_file_full)
                    if not domain_name:
                        print(f"No domain specified in {problem_file}")
                        planner_records.append({
                            "problem_file": problem_file,
                            "status": "skipped",
                            "error": "No domain specified",
                        })
                        continue

                    domain_file = self._domain_file_for_problem_domain(domain_name)
                    if not domain_file:
                        print(f"No domain file found for domain {domain_name}")
                        planner_records.append({
                            "problem_file": problem_file,
                            "domain_name": domain_name,
                            "status": "skipped",
                            "error": f"No domain file found for domain {domain_name}",
                        })
                        continue
                    
                    safe_name = self._sanitize_filename(problem_file.replace(".pddl", ""))
                    output_file = os.path.join(plan_file_path, f"{safe_name}_plan.txt")
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
                    if os.path.exists(output_file):
                        plan_output_files.append(output_file)
                    planner_records.append({
                        "problem_file": problem_file,
                        "domain_name": domain_name,
                        "domain_file": domain_file,
                        "command_path": command_path,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                        "return_code": result.returncode,
                        "duration_seconds": round(time.time() - started_at, 3),
                        "compatibility_output": output_file,
                    })

                    if result.stderr:
                        print(f"Warnings/Errors for {problem_file}:", result.stderr)
                        
                except subprocess.TimeoutExpired:
                    print(f"Planner timed out for {problem_file}")
                    planner_records.append({
                        "problem_file": problem_file,
                        "status": "timeout"
                    })
                except Exception as e:
                    print(f"Error processing file {problem_file}: {str(e)}")
                    planner_records.append({
                        "problem_file": problem_file,
                        "status": "error",
                        "error": str(e)
                    })
                    continue

            planner_manifest_path = self.config.artifact("planner_manifest", "08_planner/planner_manifest.json")
            self._write_json_artifact(planner_manifest_path, planner_records)
            self._record_artifact("planner", "manifest", planner_manifest_path)
            self.current_task_manifest.setdefault("planner", {})["plan_output_files"] = plan_output_files
            self._persist_manifest()
            return planner_records
                    
        except Exception as e:
            print(f"Error in run_planners: {str(e)}")
            raise

    def _combine_all_plans(self, decomposed_plan:str, sequence_operations:List[str]) -> str:
        """Combine all generated plan files into a single plan.
 
        """
        plan_file_path = self._get_plan_file_path()
        plan_files = [os.path.join(plan_file_path, f) for f in os.listdir(plan_file_path) if f.endswith('_validated_plan.txt')]
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

    def process_bddl_task(self, bddl_file_path: str, available_robots: List[dict]) -> None:
        """Process a task from BDDL file format.
        
        Args:
            bddl_file_path (str): Path to BDDL file
            available_robots (List[dict]): List of available robots
        """
        # Parse BDDL file
        bddl_data = self.file_processor.parse_bddl_file(bddl_file_path)
        
        # Convert task name to instruction
        task_instruction = bddl_data["task_name"].replace("-", " ").replace("_", " ")
        
        # Process task as before but with additional BDDL context
        self.process_tasks(
            test_tasks=[task_instruction],
            available_robots=[available_robots],
            objects_ai=bddl_data["objects"],
            bddl_context=bddl_data  # Pass full BDDL data for reference
        )

    def create_bddl_dataset(self, tasks: List[str], output_dir: str) -> None:
        """Create BDDL format files for a list of tasks.
        
        Args:
            tasks (List[str]): List of task descriptions
            output_dir (str): Directory to save BDDL files
        """
        os.makedirs(output_dir, exist_ok=True)
        
        for i, task in enumerate(tasks):
            # Generate BDDL content
            bddl_content = self._generate_bddl_content(
                task_name=task.lower().replace(" ", "_"),
                task_index=i,
                objects=self.objects_ai,  # Use existing objects
            )
            
            # Save BDDL file
            output_path = os.path.join(output_dir, f"problem{i}.bddl")
            self.file_processor.write_file(output_path, bddl_content)


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
    )

    return {"task_run_dir": task_manager.current_task_run_dir, 
            "tc": task_manager.tc,
            "total": task_manager.total}
    

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


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bddl-file", type=str, help="Path to BDDL file")
    parser.add_argument(
        "--floor-plan", 
        type=str, 
        required=False,  # Changed from True
        help="Required unless --bddl-file is provided"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="deepseek-chat",
        choices=get_available_models()
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

    args = parser.parse_args()
    
    # Validate that either bddl_file or floor_plan is provided
    if not args.bddl_file and args.floor_plan is None:
        parser.error("Either --bddl-file or --floor-plan must be provided")
        
    return args

def main():
    """Main execution function."""
    try:
        # Parse arguments
        args = parse_arguments()
        base_path = str(_repo_root())
        run_config = load_run_config(base_path)
        
        # Initialize task manager
        task_manager = TaskManager(
            base_path=base_path,
            model=args.model,
            prompt_decompse_set=args.prompt_decompse_set,
            prompt_allocation_set=args.prompt_allocation_set,
            config=run_config,
        )
        
        if args.bddl_file:
            # Process single BDDL task
            bddl_data = task_manager.file_processor.parse_bddl_file(args.bddl_file)
            print("\nBDDL Data:")
            print(f"Task Name: {bddl_data['task_name']}")
            print(f"Objects: {bddl_data['objects']}")
            print(f"Init State: {bddl_data['init_state']}")
            print(f"Goal State: {bddl_data['goal_state']}\n")
            
            # Convert task name to instruction
            task_instruction = bddl_data["task_name"].replace("-", " ").replace("_", " ")
            
            # Use a default robot configuration with more capabilities
            available_robots = [{
                "name": "robot1",
                "skills": ["grasp", "place", "pour", "move", "pick", "hold"],
                "mass_capacity": 10.0
            }]
            
            # Format objects for processing
            objects_ai = f"\n\nobjects = {bddl_data['objects']}"
            
            # Process the task
            task_manager.process_tasks(
                test_tasks=[task_instruction],
                available_robots=[available_robots],
                objects_ai=objects_ai
            )
            
            # Log results for BDDL task
            if args.log_results:
                task_manager.log_results(
                    task=task_instruction,
                    idx=0,
                    available_robots=available_robots,
                    gt_test_tasks=[""],  # No ground truth for BDDL tasks
                    trans_cnt_tasks=[0],
                    min_trans_cnt_tasks=[0],
                    objects_ai=objects_ai,
                    bddl_file_path=args.bddl_file
                )
        else:
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
                floor_plan=args.floor_plan,
                task_record=selected_record,
                prompt_decompse_set=args.prompt_decompse_set,
                prompt_allocation_set=args.prompt_allocation_set,
                objects_ai=objects_ai,
                config=run_config,
                test_set=args.test_set,
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



    
