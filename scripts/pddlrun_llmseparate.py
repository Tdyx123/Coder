import copy
import glob
import json
import os
import argparse
import yaml
from pathlib import Path
from datetime import datetime
import random
import subprocess
import time
import re
import shutil
import sys
from typing import List, Dict, Tuple, Optional, Union, Any

from llm_client import (
    complete_with_provider,
    extract_text,
    extract_usage,
    get_available_models as get_litellm_models,
    get_provider_for_model,
    is_rate_limit_error,
    is_retryable_error,
    load_providers,
)
from llm_logger import log_llm_call, get_llm_logger
import ai2thor.controller

import difflib  #PG: Added

import sys
sys.path.append(".")

import resources.actions as actions
import resources.robots as robots

CONFIG_FILE_NAME = "pddlrun_llmseparate_config.yaml"

def get_available_models():
    """Get list of available models from providers.yaml"""
    providers_file = Path(__file__).parent / 'providers.yaml'
    return get_litellm_models(providers_file)


def load_run_storage_config(base_path: str) -> Dict[str, Any]:
    """Load runtime storage configuration for intermediate artifacts."""
    config_path = Path(__file__).parent / CONFIG_FILE_NAME
    default_base_dir = os.path.join(base_path, "logs", "intermediate_runs")
    config = {
        "storage": {
            "base_dir": default_base_dir
        }
    }

    if not config_path.exists():
        return config

    with open(config_path, "r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}

    if not isinstance(loaded, dict):
        raise PDDLError(f"Invalid config format in {config_path}")

    storage = loaded.get("storage", {})
    if not isinstance(storage, dict):
        raise PDDLError(f"Invalid 'storage' section in {config_path}")

    configured_base_dir = storage.get("base_dir")
    if configured_base_dir:
        if not os.path.isabs(configured_base_dir):
            configured_base_dir = os.path.join(base_path, configured_base_dir)
        config["storage"]["base_dir"] = configured_base_dir

    return config

# Constants
DEFAULT_MAX_TOKENS = 128
DEFAULT_TEMPERATURE = 0
DEFAULT_RETRY_DELAY = 20
MAX_RETRIES = 3

# Action mapping from actions module

class PDDLError(Exception):
    """Base exception class for PDDL-related errors."""
    pass

class ValidationError(PDDLError):
    """Exception raised for PDDL validation errors."""
    pass

class PlanningError(PDDLError):
    """Exception raised for PDDL planning errors."""
    pass

class LLMError(Exception):
    """Exception raised for Language Model related errors."""
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
    def get_ai2_thor_objects(floor_plan: int) -> List[Dict[str, Any]]:
        """Get objects from AI2Thor environment.
        
        Args:
            floor_plan (int): Floor plan number
            
        Returns:
            List[Dict[str, Any]]: List of objects with their properties
        """
        controller = None
        try:
            controller = ai2thor.controller.Controller(scene=f"FloorPlan{floor_plan}")
            obj = [obj["objectType"] for obj in controller.last_event.metadata["objects"]]
            obj_mass = [obj["mass"] for obj in controller.last_event.metadata["objects"]]
            return PDDLUtils.convert_to_dict_objprop(obj, obj_mass)
        finally:
            if controller:
                controller.stop()

class FileProcessor:
    """Handles file operations and text processing for PDDL files.
    
    This class manages reading, writing, and processing of PDDL files and related
    text content. It provides methods for file operations and text manipulation
    specific to PDDL task processing.
    """
    
    def __init__(self, base_path: str):
        """Initialize the file processor.
        
        Args:
            base_path (str): Base path for file operations
        """
        self.base_path = base_path
        self.subtask_path = os.path.join(base_path, "resources", "generated_subtask")
        self.validated_subtask_path = os.path.join(base_path, "resources", "validated_subtask")  #PG: Added for validation
        self.each_run_path = os.path.join(base_path, "resources", "each_run")
        os.makedirs(self.subtask_path, exist_ok=True)
        os.makedirs(self.validated_subtask_path, exist_ok=True)  #PG: Added for validation
        os.makedirs(self.each_run_path, exist_ok=True)
    
    def read_file(self, file_path: str) -> str:
        """Read contents of a file.
        
        Args:
            file_path (str): Path to the file to read
            
        Returns:
            str: Contents of the file
        """
        try:
            with open(file_path, 'r') as file:
                return file.read()
        except FileNotFoundError:
            raise PDDLError(f"File not found: {file_path}")
        except Exception as e:
            raise PDDLError(f"Error reading file {file_path}: {str(e)}")
    
    def write_file(self, file_path: str, content: str) -> None:
        """Write content to a file.
        
        Args:
            file_path (str): Path to write to
            content (str): Content to write
        """
        try:
            with open(file_path, 'w') as file:
                file.write(content)
        except Exception as e:
            raise PDDLError(f"Error writing to file {file_path}: {str(e)}")

    def write_json(self, file_path: str, content: Any) -> None:
        """Write JSON content to a file."""
        try:
            with open(file_path, 'w', encoding='utf-8') as file:
                json.dump(content, file, indent=2, ensure_ascii=False)
        except Exception as e:
            raise PDDLError(f"Error writing JSON to file {file_path}: {str(e)}")
    
    def split_pddl_tasks(
        self,
        code_plan: Union[str, List[str]],
        isValidated: bool,
        output_directory: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Split PDDL tasks and save them to files.
        
        Args:
            code_plan (List[str]): List of PDDL plans to split
        """
        try:
            # Convert single plan to list
            if isinstance(code_plan, str):
                code_plan = [code_plan]

            saved_tasks: List[Dict[str, Any]] = []
            
            # Create timestamped directory
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            timestamp_directory = os.path.join(self.each_run_path, timestamp)
            os.makedirs(timestamp_directory, exist_ok=True)
            if output_directory:
                os.makedirs(output_directory, exist_ok=True)
            
            for i, plan in enumerate(code_plan):
                tasks = re.split(r"\s*\(define\s*\(problem", plan)
                
                for j, task in enumerate(tasks[1:]):
                    task = "(define (problem" + task
                    task = self.balance_parentheses(task)
                    
                    match = re.search(r'\(problem\s+(\w+)\)', task, re.IGNORECASE)
                    if match:
                        task_name = match.group(1)
                        filename = f"{i+1}_{j+1}_{task_name}.pddl"
                    else:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        filename = f"{i+1}_{j+1}_{timestamp}.pddl"
                    
                    # Save to both timestamped directory and generated_subtask
                    filepath = os.path.join(timestamp_directory, filename)
                    self.write_file(filepath, task)

                    custom_output_path = None
                    if output_directory:
                        custom_output_path = os.path.join(output_directory, filename)
                        self.write_file(custom_output_path, task)
                    
                    # Also save to generated_subtask for compatibility
                    if isValidated:
                        subtask_filepath = os.path.join(self.validated_subtask_path, filename)  #PG: Changed for validation
                    else:
                        subtask_filepath = os.path.join(self.subtask_path, filename)
                    #print("Saving pddl at path:", subtask_filepath)
                    self.write_file(subtask_filepath, task)
                    saved_tasks.append({
                        "source_plan_index": i,
                        "task_index": j,
                        "filename": filename,
                        "timestamp_copy": filepath,
                        "custom_output_copy": custom_output_path,
                        "compatibility_copy": subtask_filepath,
                    })
            return saved_tasks
            
        except Exception as e:
            raise PDDLError(f"Error splitting PDDL tasks: {str(e)}")
    
    def balance_parentheses(self, content: str) -> str:
        """Balance parentheses in PDDL content.
        
        Args:
            content (str): PDDL content to process
            
        Returns:
            str: Processed PDDL content with balanced parentheses
        """
        open_count = 0
        start_index = -1
        end_index = -1
        
        for i, char in enumerate(content):
            if char == '(':
                if open_count == 0:
                    start_index = i
                open_count += 1
            elif char == ')':
                open_count -= 1
                if open_count == 0:
                    end_index = i
                    break
        
        if start_index != -1 and end_index != -1:
            return content[start_index:end_index+1]
        return ""
    

#PG: Edited from original to improve robustness and handle edge cases

    def split_and_store_tasks(
        self,
        content: str,
        llm: Optional['LLMHandler'] = None,
        model: Optional[str] = None
    ) -> Tuple[List[str], str]:
        """Split and store tasks from content.

        Returns:
            Tuple[List[str], str]: (subtasks list, sequence operations)
        """
        import re, difflib  # keep difflib since you use it later

        # FIX: always initialize so prints/returns are safe
        sequence_operations = ""

        # 1) Extract problem_summary and sequence_operations
        summary_match = re.search(
            r'(?:#?\s*)?Problem\s*content\s*summary\s*:?(.*?)(?=(?:#?\s*)?Sequence\s*of\s*Operations?\s*:?)',
            content, re.DOTALL | re.IGNORECASE
        )
        if summary_match:
            problem_summary = summary_match.group(1).strip()
        else:
            split_marker = re.search(r'(?:#?\s*)?Sequence\s*of\s*Operations?\s*:', content, re.IGNORECASE)
            if split_marker:
                problem_summary = content[:split_marker.start()].strip()
                sequence_operations = content[split_marker.end():].strip()
            else:
                problem_summary = content.strip()
                sequence_operations = "failed to extract2"

        # 2) FIX: robust subtask extraction — only capture blocks that start with a SubTask header
        # Supports "#SubTask 1:" / "# SubTask 2:" / "SubTask 3:" (case-insensitive)
        subtask_block_re = re.compile(
            r'(?im)^\s*#?\s*Sub\s*Task\s*\d+\s*:\s*.*?(?=^\s*#?\s*Sub\s*Task\s*\d+\s*:\s*|\Z)',
            re.DOTALL
        )
        subtasks = [m.group(0).strip() for m in subtask_block_re.finditer(problem_summary)]

        # 2a) If no SubTask header found, treat the whole thing as one subtask (safe fallback)
        if not subtasks:
            subtasks = [problem_summary.strip()] if problem_summary.strip() else []

        #print("Subtasks", subtasks)
        #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

        # 3) Verify/repair structure (unchanged except for using a robust header matcher)
        fixed_subtasks = []
        structure_ok = re.compile(
            r'\*\*Assigned\s*Robots?\*\*\s*:\s*.*?\n\*\*Objects\s*Involved\*\*\s*:\s*.*',
            re.DOTALL | re.IGNORECASE
        )
        fallback_plain = re.compile(
            r'\bAssigned\s*Robots?\b\s*:\s*.*?\n\bObjects\s*Involved\b\s*:\s*.*',
            re.DOTALL | re.IGNORECASE
        )

        for subtask in subtasks:
            #print("Subtask:", subtask)
            #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

            if structure_ok.search(subtask) or fallback_plain.search(subtask):
                fixed_subtasks.append(subtask)
                continue

            if llm and model:
                fix_prompt = (
                    "The following subtask description needs to be reformatted. Please reformat it to strictly follow this structure:\n\n"
                    "#SubTask [number]: [Task Name]\n\n"
                    "# Initial Precondition analyze due to previous subtask:\n"
                    "#1. [precondition description]\n\n"
                    "[Action descriptions with Parameters, Preconditions, and Effects]\n\n"
                    "**Assigned Robot**: [robot number or 'team']\n"
                    "**Objects Involved**: [list of objects]\n\n"
                    "Important formatting rules:\n"
                    "1. Each section must start with the exact headers shown above\n"
                    "2. The order must be: SubTask header, Preconditions, Action descriptions, Assigned Robot, Objects Involved\n"
                    "3. Use '**Assigned Robot**:' and '**Objects Involved**:' exactly as shown with double asterisks\n"
                    "4. Include all action descriptions with their Parameters, Preconditions, and Effects\n"
                    "5. Keep the original action descriptions if they exist\n\n"
                    "Original subtask:\n" + subtask + "\n\n"
                    "Please provide ONLY the reformatted version following the structure above. Do not add any explanations or additional text."
                )

                messages = [
                    {"role": "system", "content": "You are a Robot PDDL problem Expert. Your task is to reformat subtask descriptions to match a specific structure. Do not add any explanations or additional text."},
                    {"role": "user", "content": fix_prompt}
                ]
                _, fixed_subtask = llm.query_model(messages, model, max_tokens=1400, frequency_penalty=0.4)
                    

                #print("=== Testing match on fixed subtask ===")
                #print("Fixed subtask:", repr(fixed_subtask))
                #print("Match result:", structure_ok.search(fixed_subtask) or fallback_plain.search(fixed_subtask))

                if structure_ok.search(fixed_subtask) or fallback_plain.search(fixed_subtask):
                    fixed_subtasks.append(fixed_subtask)
                else:
                    print("LLM structure fix failed, using original subtask")
                    #print("\n".join(difflib.ndiff(fixed_subtask.splitlines(), structure_ok.pattern.splitlines())))
                    fixed_subtasks.append(subtask)
            else:
                print("LLM handler not provided, using original subtask")
                fixed_subtasks.append(subtask)

        return fixed_subtasks, sequence_operations






    '''
    def split_and_store_tasks(self, content: str, llm: Optional['LLMHandler'] = None, model: Optional[str] = None) -> Tuple[List[str], str]:
        """Split and store tasks from content.
        
        Returns:
            Tuple[List[str], str]: Tuple of (subtasks list, sequence operations)
        """

        #print("Split and store tasks")
        #print("Content", content)

        #PG: Edited
        summary_match = re.search( r'(?:#?\s*)?Problem\s*content\s*summary\s*:?(.*?)(?=(?:#?\s*)?Sequence\s*of\s*Operations?\s*:?)', content, re.DOTALL | re.IGNORECASE )
        if summary_match:
            problem_summary = summary_match.group(1).strip()
        else:
            # fallback to "everything before sequence section"
            split_marker = re.search(r'(?:#?\s*)?Sequence\s*of\s*Operations?\s*:', content, re.IGNORECASE)
            if split_marker:
                problem_summary = content[:split_marker.start()].strip()
                sequence_operations = content[split_marker.end():].strip()
            else:
                problem_summary = content.strip()
                sequence_operations = "failed to extract2"

        #print("Summary_match:", summary_match)

        #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        #print("Problem summary:", problem_summary)
        #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

        #print("Sequence operations:", sequence_operations)
        #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

        #PG: Commented the following in original
        
        # More flexible pattern for summary and sequence
        summary_pattern = re.compile(r'(?:#?\s*)?Problem\s*content\s*summary\s*:?(.*?)(?=(?:#?\s*)?Sequence\s*of\s*Operations?\s*:?)', re.DOTALL | re.IGNORECASE)
        sequence_pattern = re.compile(r'(?:#?\s*)?Sequence\s*of\s*Operations?\s*:\s*(.*)', re.DOTALL | re.IGNORECASE)
        
        summary_match = summary_pattern.search(content)
        problem_summary = summary_match.group(1).strip() if summary_match else "failed to extract1"
        print("Summary match", summary_match)
        print("problem_summary", problem_summary)


        sequence_match = sequence_pattern.search(content)
        sequence_operations = sequence_match.group(1).strip() if sequence_match else "failed to extract2"
        
        print("Sequence match", sequence_match)
        print("sequence Operation", sequence_operations)
        
        
        # More flexible pattern for splitting subtasks
        subtasks = re.split(r'(?:#?\s*)?subtask\s*:?\s*', problem_summary, flags=re.IGNORECASE)
        subtasks = [subtask.strip() for subtask in subtasks if subtask.strip()]
        print("Subtasks", subtasks)
        print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        # Fix structure of each subtask if needed
        fixed_subtasks = []
        for subtask in subtasks:
            print("Subtask:", subtask)
            print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
            # Pattern to match the format expected by problemextracting

            #Pg:Edited
            pattern = re.compile(
                r'\*\*Assigned\s*Robots?\*\*\s*:\s*.*?\n\*\*Objects\s*Involved\*\*\s*:\s*.*',
                re.DOTALL | re.IGNORECASE
            )

            #PG: Following is commented from original
            
            pattern = re.compile(
                r'\s*\*{0,2}\s*Assigned\s*Robot\s*\??:?\*{0,2}\s*\??(.*?)\s*\*{0,2}\s*Objects\s*Involved\s*:\??\*{0,2}', 
                re.DOTALL | re.IGNORECASE
            )
            
            #print("pattern", pattern)
            #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
            if not pattern.search(subtask):
                if llm and model:
                    #print("Fixing structure of subtask with LLM")
                    fix_prompt = (
                        "The following subtask description needs to be reformatted. Please reformat it to strictly follow this structure:\n\n"
                        "#SubTask [number]: [Task Name]\n\n"
                        "# Initial Precondition analyze due to previous subtask:\n"
                        "#1. [precondition description]\n\n"
                        "[Action descriptions with Parameters, Preconditions, and Effects]\n\n"
                        "**Assigned Robot**: [robot number or 'team']\n"
                        "**Objects Involved**: [list of objects]\n\n"
                        "Important formatting rules:\n"
                        "1. Each section must start with the exact headers shown above\n"
                        "2. The order must be: SubTask header, Preconditions, Action descriptions, Assigned Robot, Objects Involved\n"
                        "3. Use '**Assigned Robot**:' and '**Objects Involved**:' exactly as shown with double asterisks\n"
                        "4. Include all action descriptions with their Parameters, Preconditions, and Effects\n"
                        "5. Keep the original action descriptions if they exist\n\n"
                        "Original subtask:\n" + subtask + "\n\n"
                        "Please provide ONLY the reformatted version following the structure above. Do not add any explanations or additional text."
                    )

                    #print("Fixed prompt", fix_prompt)
                    #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                    
                    if "gpt" not in model:
                        _, fixed_subtask = llm.query_model(fix_prompt, model, max_tokens=1000, stop=["def"], frequency_penalty=0.30)
                    else:
                        messages = [
                            {"role": "system", "content": "You are a Robot PDDL problem Expert. Your task is to reformat subtask descriptions to match a specific structure. Do not add any explanations or additional text."},
                            {"role": "user", "content": fix_prompt}
                        ]
                        _, fixed_subtask = llm.query_model(messages, model, max_tokens=1400, frequency_penalty=0.4)
                    
                    #print("Fixed subtask", fixed_subtask)
                    #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                    print("=== Testing match on fixed subtask ===")
                    print("Fixed subtask:", repr(fixed_subtask))
                    print("Match result:", pattern.search(fixed_subtask))
                    # Verify the fix worked
                    if pattern.search(fixed_subtask):
                        fixed_subtasks.append(fixed_subtask)
                    else:
                        print("LLM structure fix failed, using original subtask")
                        print("\n".join(difflib.ndiff(fixed_subtask.splitlines(), pattern.pattern.splitlines())))  #PG: Added
                        fixed_subtasks.append(subtask)
                else:
                    print("LLM handler not provided, using original subtask")
                    fixed_subtasks.append(subtask)
            else:
                fixed_subtasks.append(subtask)
        
        return fixed_subtasks, sequence_operations
    '''



        
    def extract_domain_name(self, problem_file_path: str) -> Optional[str]:
        """Extract the domain name from a problem PDDL file.
        
        Args:
            problem_file_path (str): Path to the problem PDDL file
            
        Returns:
            Optional[str]: Domain name if found, None otherwise
        """
        try:
            domain_pattern = re.compile(r'\(\s*:domain\s+(\S+)\s*\)')
            content = self.read_file(problem_file_path)
            match = domain_pattern.search(content)
            return match.group(1) if match else None
        except Exception as e:
            print(f"Error extracting domain name from {problem_file_path}: {str(e)}")
            return None

    def find_domain_file(self, domain_name: str) -> Optional[str]:
        """Find the domain file for a given domain name.
        
        Args:
            domain_name (str): Name of the domain to find
            
        Returns:
            Optional[str]: Path to domain file if found, None otherwise
        """
        try:
            domain_path = os.path.join(self.base_path, "resources", f"{domain_name}.pddl")
            return domain_path if os.path.isfile(domain_path) else None
        except Exception as e:
            print(f"Error finding domain file for {domain_name}: {str(e)}")
            return None

    def clean_directory(self, directory_path: str) -> None:
        """Clean a directory by removing all files and subdirectories.
        
        Args:
            directory_path (str): Path to directory to clean
        """
        if os.path.exists(directory_path):
            for filename in os.listdir(directory_path):
                file_path = os.path.join(directory_path, filename)
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)

    def extract_plan_from_output(self, content: str) -> str:
        """Extract clean plan from planner output.
        
        Args:
            content (str): Raw planner output content
            
        Returns:
            str: Cleaned plan text
        """
        if not content or not isinstance(content, str):
            raise ValueError("Invalid content provided to extract_plan_from_output")
            
        try:
            plan_pattern = re.compile(r"^\s*\w+\s+\w+\s+\w+\s+\(\d+\)\s*$", re.MULTILINE)
            plan = plan_pattern.findall(content)
            return "\n".join(plan) if plan else ""
        except Exception as e:
            print(f"Error extracting plan from output: {str(e)}")
            return ""

    def calculate_task_completion_rate(self) -> Tuple[int, int]:
        """Calculate task completion rate from plan files.
        
        Returns:
            Tuple[int, int]: (number of completed tasks, total number of tasks)
        """
        TC = 0
        total_subtasks = 0

        for file_path in glob.glob(os.path.join(self.subtask_path, '*_plan.txt')):
            print("Calculating completion for file:", file_path)
            total_subtasks += 1
            content = self.read_file(file_path)
            TC += content.count('Solution found!')
            print(f"File: {file_path}, Solutions found: {content.count('Solution found!')}")
        
        print(f"Total completed tasks: {TC}, Total subtasks: {total_subtasks}")
        return TC, total_subtasks

    def parse_bddl_file(self, bddl_file_path: str) -> Dict[str, Any]:
        """Parse BDDL file and extract key components.
        
        Args:
            bddl_file_path (str): Path to BDDL file
            
        Returns:
            Dict with keys:
                - task_name: str
                - objects: List[Dict]
                - init_state: List[str]
                - goal_state: List[str]
        """
        try:
            content = self.read_file(bddl_file_path)
            
            # Extract task name from problem definition
            task_pattern = r'\(define \(problem (.*?)\)'
            task_match = re.search(task_pattern, content)
            task_name = task_match.group(1) if task_match else ""
            
            # Extract objects section
            objects_pattern = r'\(:objects(.*?)\)'
            objects_match = re.search(objects_pattern, content, re.DOTALL)
            objects_section = objects_match.group(1) if objects_match else ""
            
            # Extract init state
            init_pattern = r'\(:init(.*?)\)'
            init_match = re.search(init_pattern, content, re.DOTALL)
            init_state = init_match.group(1) if init_match else ""
            
            # Extract goal state
            goal_pattern = r'\(:goal(.*?)\)\s*\)'
            goal_match = re.search(goal_pattern, content, re.DOTALL)
            goal_state = goal_match.group(1) if goal_match else ""
            
            return {
                "task_name": task_name,
                "objects": objects_section.strip(),
                "init_state": init_state.strip(),
                "goal_state": goal_state.strip()
            }
        except Exception as e:
            raise PDDLError(f"Error parsing BDDL file: {str(e)}")

class LLMHandler:
    """Handles interactions with Language Models (LLMs).
    

    """
    
    def __init__(self):
        """Initialize the LLM handler."""
        self.providers = None
    
    def _load_providers(self):
        """Load providers from yaml file."""
        if self.providers is None:
            providers_file = Path(__file__).parent / 'providers.yaml'
            self.providers = load_providers(providers_file)
        return self.providers
    
    def _get_provider_for_model(self, model):
        """Get provider configuration for the given model."""
        provider = get_provider_for_model(model, self._load_providers())
        return provider, provider['name']
    
    def query_model(
        self, 
        prompt: Union[str, List[Dict]], 
        model: str, 
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        stop: Optional[List[str]] = None,
        logprobs: Optional[int] = 1,
        frequency_penalty: float = 0
    ) -> Tuple[dict, str]:
        """the language model 
        
        Args:
            prompt: Either a string or a list of message dicts
            model: The model to use
            max_tokens: Maximum number of tokens in the response
            temperature: Sampling temperature
            stop: Optional list of stop sequences
            logprobs: Optional number of logprobs to return
            frequency_penalty: Frequency penalty for token generation
        
        Returns:
            Tuple of (full response object, generated text)
            
        """
        retry_delay = DEFAULT_RETRY_DELAY
        provider_config, provider = self._get_provider_for_model(model)
        
        for attempt in range(MAX_RETRIES):
            try:
                start_time = time.time()
                response = complete_with_provider(
                    model=model,
                    prompt=prompt,
                    provider=provider_config,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                    frequency_penalty=frequency_penalty,
                )
                duration_ms = (time.time() - start_time) * 1000
                text = extract_text(response)
                usage = extract_usage(response)
                
                log_llm_call(
                    model=model,
                    provider=provider,
                    messages=prompt if isinstance(prompt, list) else [{'role': 'user', 'content': prompt}],
                    params={
                        'max_tokens': max_tokens,
                        'temperature': temperature,
                        'frequency_penalty': frequency_penalty
                    },
                    response_text=text,
                    usage=usage,
                    duration_ms=duration_ms
                )
                
                return response, text
                    
            except Exception as e:
                if is_rate_limit_error(e):
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    raise LLMError("Rate limit exceeded")

                if is_retryable_error(e):
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(retry_delay)
                        continue
                    raise LLMError(f"API Error after all retries: {str(e)}")

                raise LLMError(f"Unexpected error in LLM query: {str(e)}")

class PDDLValidator:
    """Handles PDDL validation operations"""
    
    def __init__(self, llm_handler: LLMHandler, file_processor: FileProcessor):
        """Initialize the PDDL validator.
        
        Args:
            llm_handler (LLMHandler)
            file_processor (FileProcessor)
        """
        self.llm = llm_handler
        self.file_processor = file_processor
    
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
            _, validated_text = self.llm.query_model(messages, self.model, max_tokens=1400, frequency_penalty=0.4)
            
            # Save the validated content back to the problem file
            self.file_processor.write_file(problem_file, validated_text)
            
        except Exception as e:
            raise ValidationError(f"Error validating PDDL problem: {str(e)}")

class PDDLPlanner:
    
    def __init__(self, base_path: str, file_processor: FileProcessor):
        """Initialize the PDDL planner.

        """
        self.base_path = base_path
        self.file_processor = file_processor
        self.planner_path = os.path.join(base_path, "downward", "fast-downward.py")
    
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
                "seq-opt-lmcut",
                domain_file,
                problem_file
            ]
            
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Save the plan output
            output_file = problem_file.replace('.pddl', '_plan.txt')
            self.file_processor.write_file(output_file, result.stdout)
            
            if result.stderr:
                print(f"Warnings/Errors for {problem_file}:", result.stderr)
                
        except Exception as e:
            raise PlanningError(f"Error running PDDL planner: {str(e)}")
    
    def calculate_completion_rate(self) -> Tuple[int, int]:
        """
        
        Returns:
            Tuple[int, int]: (number of completed tasks, total number of tasks)
        """
        TC = 0
        total_subtasks = 0
        
        try:
            for file_path in glob.glob(os.path.join(self.file_processor.validated_subtask_path, '*_plan.txt')):   #PG: Changed for validation
                total_subtasks += 1
                content = self.file_processor.read_file(file_path)
                TC += content.count('Solution found!')
                
            return TC, total_subtasks
            
        except Exception as e:
            raise PlanningError(f"Error calculating completion rate: {str(e)}")

class TaskManager:
    """Manages task processing and coordination.
    
    This is the main orchestrator class that coordinates all operations
 result logging.
    """
    
    def __init__(self, base_path: str, model: str, prompt_decompse_set: str = "pddl_train_task_decomposesep", prompt_allocation_set: str = "pddl_train_task_allocationsep"):
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
        self.runtime_config = load_run_storage_config(base_path)
        
        # Initialize components
        self.llm = LLMHandler()
        self.file_processor = FileProcessor(base_path)
        self.validator = PDDLValidator(self.llm, self.file_processor)
        self.planner = PDDLPlanner(base_path, self.file_processor)
        
        # Initialize paths
        self.resources_path = os.path.join(base_path, "resources")
        self.logs_path = os.path.join(".", "logs")  
        self.intermediate_base_path = self.runtime_config["storage"]["base_dir"]
        os.makedirs(self.logs_path, exist_ok=True)
        os.makedirs(self.intermediate_base_path, exist_ok=True)
        
        # Clean generated subtask directory
        self.clean_generated_subtask_directory(False)
        self.clean_generated_subtask_directory(True)  #PG: Added for validation
        
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
        
        # Get action mapping from actions module
        
        # Initialize objects_ai as None, will be set in process_tasks
        self.objects_ai = None
        self.current_task_run_dir: Optional[str] = None
        self.current_task_manifest: Dict[str, Any] = {}

    def _sanitize_filename(self, value: str) -> str:
        """Convert a value into a filesystem-safe filename fragment."""
        sanitized = re.sub(r'[<>:"/\\|?*\s]+', '_', value.strip())
        sanitized = sanitized.strip('._')
        return sanitized or "task"

    def _write_text_artifact(self, relative_path: str, content: Any) -> Optional[str]:
        """Write a text artifact under the current task run directory."""
        if not self.current_task_run_dir:
            return None

        artifact_path = os.path.join(self.current_task_run_dir, relative_path)
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        self.file_processor.write_file(artifact_path, str(content))
        return artifact_path

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

    def _persist_manifest(self) -> None:
        """Persist the current task manifest to disk."""
        if self.current_task_run_dir and self.current_task_manifest:
            self._write_json_artifact("run_manifest.json", self.current_task_manifest)

    def _prepare_task_run_dir(self, task_idx: int, task: str, robots: List[dict], objects_ai: str, domain_content: str) -> None:
        """Create and initialize the storage directory for the current task."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"{task_idx + 1:03d}_{self._sanitize_filename(task)[:80]}_{timestamp}"
        self.current_task_run_dir = os.path.join(self.intermediate_base_path, folder_name)
        os.makedirs(self.current_task_run_dir, exist_ok=True)
        self.current_task_manifest = {
            "task_index": task_idx,
            "task": task,
            "model": self.model,
            "created_at": timestamp,
            "storage_base_dir": self.intermediate_base_path,
            "artifacts": {}
        }

        inputs = {
            "task": task,
            "robots": robots,
            "objects_ai": objects_ai,
            "model": self.model,
            "prompt_decompose_set": self.prompt_decompse_set,
            "prompt_allocation_set": self.prompt_allocation_set
        }
        self._write_json_artifact("inputs/task_context.json", inputs)
        self._record_artifact("inputs", "task_context", "inputs/task_context.json")
        self._write_text_artifact("inputs/domain_content.pddl", domain_content)
        self._record_artifact("inputs", "domain_content", "inputs/domain_content.pddl")
        self._persist_manifest()

    def clean_generated_subtask_directory(self, isValidated: bool = False) -> None:
        """Clean the generated subtask directory."""
        if isValidated:
            directory = os.path.join(self.resources_path, "validated_subtask")  #PG: Changed for validation
        else:
            directory = os.path.join(self.resources_path, "generated_subtask")
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
            for robots_list in robots_test_tasks:
                task_robots = []
                for i, r_id in enumerate(robots_list):
                    rob = robots.robots[r_id-1]  # Direct reference like original
                    rob['name'] = f'robot{i+1}'  # Use f-string for consistency
                    task_robots.append(rob)
                available_robots.append(task_robots)
            
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
        
        #print(f"Creating log folder: {log_folder}")
        os.makedirs(log_folder)
        
        try:
            print(f"Writing plans for task {idx + 1}")
            self._write_plan(log_folder, "code_planpddl.py", self.code_planpddl[idx])
            #print(f"Successfully wrote code_planpddl for task {idx + 1}")
            self._write_plan(log_folder, "combined_plan.py", self.combined_plan[idx])
            #print(f"Successfully wrote combined_plan for task {idx + 1}")
            self._write_plan(log_folder, "decomposed_plan.py", self.decomposed_plan[idx])
            #print(f"Successfully wrote decomposed_plan for task {idx + 1}")
            self._write_plan(log_folder, "allocated_plan.py", self.allocated_plan[idx])
            #print(f"Successfully wrote allocated_plan for task {idx + 1}")
            self._write_plan(log_folder, "code_plan.py", self.code_plan[idx])
            #print(f"Successfully wrote code_plan for task {idx + 1}")
            self._write_plan(log_folder, "validated_plan.py", self.validated_plan[idx])  #PG: Added for validation
            #print(f"Successfully wrote validated_plan for task {idx + 1}")
            
            # Log main information
            TC, total_subtasks = self.tc[idx], self.total_subtasks[idx]
            print(f"Task {idx + 1} - TC: {TC}, Total Subtasks: {total_subtasks}")


            with open(os.path.join(log_folder, "log.txt"), 'w') as f:
                f.write(task)
                f.write(f"\n\nModel: {self.model}")
                f.write(f"\n{objects_ai}")
                f.write(f"\nrobots = {available_robots[idx]}")
                f.write(f"\nground_truth = {gt_test_tasks[idx]}")
                f.write(f"\ntrans = {trans_cnt_tasks[idx]}")
                f.write(f"\nmin_trans = {min_trans_cnt_tasks[idx]}")
                f.write(f"\nTotalsuccesssubtask = {TC}")
                f.write(f"\nTotalsubtask = {total_subtasks}")
            
            # Copy generated subtasks
            subtask_folder = os.path.join(log_folder, "generated_subtask")
            os.makedirs(subtask_folder)
            source_folder = os.path.join(self.resources_path, "generated_subtask")
            for file_name in os.listdir(source_folder):
                full_file_name = os.path.join(source_folder, file_name)
                if os.path.isfile(full_file_name):
                    shutil.copy(full_file_name, subtask_folder)
            
            #PG: Added for validation
            # Copy validated subtasks
            validated_subtask_folder = os.path.join(log_folder, "validated_subtask")
            os.makedirs(validated_subtask_folder)
            source_validated_folder = os.path.join(self.resources_path, "validated_subtask")
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
                with open(os.path.join(folder, f"{filename}.{i}"), 'w') as f:
                    f.write(str(item))
        else:
            with open(os.path.join(folder, filename), 'w') as f:
                f.write(content)

    def process_tasks(self, test_tasks: List[str], available_robots: List[dict], objects_ai: str) -> None:
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
            
            # Get domain content
            allaction_domain_path = os.path.join(self.resources_path, "allactionrobot.pddl")
            domain_content = self.file_processor.read_file(allaction_domain_path)
            
            # Process each task
            for task_idx, (task, robots) in enumerate(zip(test_tasks, available_robots)):
                print(f"\n{'='*50}")
                print(f"Processing Task: {task}: {task_idx + 1}/{len(test_tasks)}")
                print(f"{'='*50}")
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

                # Generate and store allocation plan
                allocated_plan = self._generate_allocation_plan(decomposed_plan, robots, objects_ai)
                self.allocated_plan.append(allocated_plan)
                print("✓ Allocation plan generated")
                #print("Allocation Plan:\n", allocated_plan)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                
                #print("Waiting for content summary...")
                #time.sleep(60)  
                
                # Generate problem summary
                problem_summary = self._generate_problem_summary(decomposed_plan, allocated_plan, robots)
                print("✓ Problem summary generated")
                #print("Problem Summary:\n", problem_summary)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                #print("Waiting to generate problem files...")
                #time.sleep(60)  
                
                # Generate and store problem files
                code_plan = self._generate_problem_files(problem_summary)
                self.code_plan.append(code_plan) 
                print("✓ Problem files generated")
                # print("Code Plan:\n", code_plan)
                # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                # Split into subtasks
                split_manifest = self.file_processor.split_pddl_tasks(
                    code_plan,
                    False,
                    output_directory=os.path.join(self.current_task_run_dir, "06_split/generated_subtask")
                )
                self._write_json_artifact("06_split/generated_subtask_manifest.json", split_manifest)
                self._record_artifact("split", "generated_subtask_manifest", "06_split/generated_subtask_manifest.json")
                self._persist_manifest()
                print("✓ Split into subtasks complete")
                #input("Press Enter to continue")
                #print("Waiting for files to be processed...")
                #time.sleep(50)
                
                # Validate and plan
                self._validate_and_plan()
                print("✓ Validation and planning complete")
                
                # Combine and process plans
                combined_plan = self._combine_all_plans(decomposed_plan)
                self.combined_plan.append(combined_plan)
                print("✓ Plans combined")
                #print("Combined Plan:\n", combined_plan)
                #input("Press Enter to continue")

                # Match references and store final PDDL plan
                matched_plan = self._match_references_for_plan(combined_plan, objects_ai)
                self.code_planpddl.append(matched_plan)
                print("✓ References matched")
                print("Final PDDL Plan:\n", matched_plan)

                # Calculate completion rate
                tc, total = self.planner.calculate_completion_rate()
                self.tc.append(tc)
                self.total_subtasks.append(total)
                self.current_task_manifest["completion"] = {
                    "successful_subtasks": tc,
                    "total_subtasks": total
                }
                self._persist_manifest()
                print(f"Task {task_idx + 1} completion rate: {tc}/{total}")
                
            print(f"\n{'='*50}")
            print(f"All {len(test_tasks)} tasks processed")
            print(f"{'='*50}")
            
        except Exception as e:
            print(f"\n[ERROR] Task Processing Failed:")
            print(f"Error type: {type(e).__name__}")
            print(f"Error message: {str(e)}")
            print(f"Current task index: {task_idx if 'task_idx' in locals() else 'Not started'}")
            raise

    def _generate_decomposed_plan(self, task: str, domain_content: str, robots: List[dict], objects_ai: str) -> str:
        """Generate decomposed plan for a task."""
        try:
            # Read decomposition prompt file
            decompose_prompt_path = os.path.join(self.base_path, "data", "pythonic_plans", f"{self.prompt_decompse_set}.py")
            decompose_prompt = self.file_processor.read_file(decompose_prompt_path)
            
            # Construct the prompt incrementally like the original
            prompt = f"from pddl domain file with all possible actions: \n{domain_content}\n\n"
            prompt += objects_ai
            prompt += f"\nrobots = {robots}\n\n"
            prompt += "robot initiate 'as not inaction robot '(which defaults location too)\n\n"
            prompt += decompose_prompt
            prompt += "# GENERAL TASK DECOMPOSITION \n"
            prompt += "Decompose and parallel subtasks where ever possible.\n\n"
            prompt += f"# Task Description: {task}"
            self._write_text_artifact("01_decompose/01_decompose_prompt.txt", prompt)
            self._record_artifact("decompose", "prompt", "01_decompose/01_decompose_prompt.txt")
            
            messages = [{"role": "user", "content": prompt}]
            _, text = self.llm.query_model(messages, self.model, max_tokens=1300, frequency_penalty=0.0)
            self._write_text_artifact("01_decompose/02_decompose_output.txt", text)
            self._record_artifact("decompose", "output", "01_decompose/02_decompose_output.txt")
            self._persist_manifest()
            
            return text
            
        except Exception as e:
            raise PDDLError(f"Error generating decomposed plan: {str(e)}")
    
    def _generate_allocation_plan(self, decomposed_plan: str, robots: List[dict], objects_ai: str) -> str:
        """Generate allocation plan for decomposed tasks.
        
        """
        try:
            # Read allocation prompt file
            prompt_file = os.path.join(self.base_path, "data", "pythonic_plans", f"{self.prompt_allocation_set}_solution.py")
            with open(prompt_file, "r") as allocated_prompt_file:
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
            self._write_text_artifact("02_allocate/01_allocate_prompt.txt", prompt)
            self._record_artifact("allocate", "prompt", "02_allocate/01_allocate_prompt.txt")
            
            # Handle different GPT versions like the original
            messages = [{"role": "user", "content": prompt}]
            _, text = self.llm.query_model(messages, self.model, max_tokens=1500, frequency_penalty=0.69)
            self._write_text_artifact("02_allocate/02_allocate_output.txt", text)
            self._record_artifact("allocate", "output", "02_allocate/02_allocate_output.txt")
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
            prompt_file = os.path.join(self.base_path, "data", "pythonic_plans", f"{self.prompt_allocation_set}_summary.py")
            with open(prompt_file, "r") as code_prompt_file:
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
                _, text = self.llm.query_model(messages, self.model, max_tokens=1400, frequency_penalty=0.4)
                
                code_plan.append(text)
                self._write_text_artifact(output_path, text)
                self._record_artifact("summary", f"output_{i + 1:02d}", output_path)
            self._persist_manifest()
            
            return code_plan
            
        except Exception as e:
            raise PDDLError(f"Error generating problem summary: {str(e)}")

    def _generate_problem_files(self, problem_summary: Union[str, List[str]]) -> List[str]:
        """Generate PDDL problem files from plans.
        
        """
        # Handle list input by taking first summary
        if isinstance(problem_summary, list):
            problem_summary = problem_summary[0]
        
        problem_pddl = []
        self._write_text_artifact("04_problem_files/01_problem_summary_raw.txt", problem_summary)
        self._record_artifact("problem_files", "problem_summary", "04_problem_files/01_problem_summary_raw.txt")
        
        # Split into subtasks and sequence operations
        subtasks, sequence_operations = self.file_processor.split_and_store_tasks(
            problem_summary,
            llm=self.llm,
            model=self.model
        )

        # print("subtasks", subtasks)
        # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        # print("sequence operations", sequence_operations)
        # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


        # Store sequence operations for later use
        self.sequence_operations = sequence_operations
        self._write_text_artifact("04_problem_files/02_sequence_operations.txt", sequence_operations)
        self._record_artifact("problem_files", "sequence_operations", "04_problem_files/02_sequence_operations.txt")
        subtask_entries = []
        for idx, subtask in enumerate(subtasks, start=1):
            relative_path = f"04_problem_files/subtasks/subtask_{idx:02d}.txt"
            self._write_text_artifact(relative_path, subtask)
            subtask_entries.append({
                "index": idx,
                "path": relative_path
            })
        self._write_json_artifact("04_problem_files/03_subtasks.json", subtask_entries)
        self._record_artifact("problem_files", "subtasks_index", "04_problem_files/03_subtasks.json")
        
        # Process each subtask using the class method
        problem_pddl = self.problemextracting(
            subtasks=subtasks,
            llm=self.llm,
            model=self.model,
            file_processor=self.file_processor,
            objects_ai=self.objects_ai,
            prompt_allocation_set=self.prompt_allocation_set
        )
        self._write_json_artifact(
            "04_problem_files/04_generated_problem_files.json",
            [{"index": idx + 1, "content": content} for idx, content in enumerate(problem_pddl)]
        )
        self._record_artifact("problem_files", "generated_problem_files", "04_problem_files/04_generated_problem_files.json")
        self._persist_manifest()
        
        return problem_pddl
    

    def problemextracting(
            self,
            subtasks: List[str],
            llm: 'LLMHandler',
            model: str,
            file_processor: 'FileProcessor',
            objects_ai: str,
            prompt_allocation_set: str
        ) -> List[str]:
        """Extract problem files from subtasks."""
        problem_pddl: List[str] = []

        # Robust matchers for the Assigned Robots block
        assigned_block_re = re.compile(
            r'(?is)\*\*Assigned\s*Robots?\*\*\s*:\s*(.*?)\n\*\*Objects\s*Involved\*\*\s*:',
            re.IGNORECASE | re.DOTALL
        )
        assigned_block_fallback = re.compile(
            r'(?is)\bAssigned\s*Robots?\b\s*:\s*(.*?)\n\bObjects\s*Involved\b\s*:',
            re.IGNORECASE | re.DOTALL
        )

        # Match "robot 1" and "robots 1"
        robot_num_re = re.compile(r'\brobots?\s*(\d+)\b', re.IGNORECASE)

        # Phrases that imply "pick a single robot among the listed ones"
        single_choice_phrase_re = re.compile(
            r'\b(any\s+one|either|one\s+of|choose\s+one|pick\s+one|any\s+robot\s+among|any\s+of)\b',
            re.IGNORECASE
        )

        for subtask_idx, subtask in enumerate(subtasks, start=1):
            m = assigned_block_re.search(subtask) or assigned_block_fallback.search(subtask)
            if not m:
                print("Invalid subtask structure, skipping")
                continue

            assigned_robots = m.group(1).strip()

            # Extract robot numbers
            robot_numbers = robot_num_re.findall(assigned_robots)

            # Fallback: capture bare numbers if "robot(s)" isn't repeated before each number
            if not robot_numbers:
                robot_numbers = re.findall(r'\b\d+\b', assigned_robots)

            # Normalize like ["robot1", "robot2", ...]
            normalized_robot_numbers = [f"robot{num}" for num in robot_numbers]

            # Detect phrasing that indicates a single-choice (not team) among listed robots
            single_choice = bool(single_choice_phrase_re.search(assigned_robots))

            # Team detection
            is_team = ("team" in assigned_robots.lower()) or ("allactionrobot" in assigned_robots.lower())

            # If wording says "any one/either/one of/..." and we have numbers, treat as single-robot
            if single_choice and normalized_robot_numbers:
                # Deterministic selection policy: smallest robot number
                try:
                    smallest = min(int(n[len("robot"):]) for n in normalized_robot_numbers)
                    normalized_robot_numbers = [f"robot{smallest}"]
                except ValueError:
                    # Fallback to the first listed if parsing failed
                    normalized_robot_numbers = [normalized_robot_numbers[0]]
                is_team = False

            if (not single_choice) and (len(normalized_robot_numbers) > 1):
                is_team = True

            if is_team:
                # Team task: concatenate all team domains
                all_domain_contents = ""
                for robot in normalized_robot_numbers:
                    domain_path = os.path.join(self.base_path, "resources", f"{robot}.pddl")
                    domain_text = file_processor.read_file(domain_path)
                    if domain_text:
                        all_domain_contents += domain_text

                if not all_domain_contents:
                    print("No team robot domains found; skipping team prompt.")
                    continue

                problem_fileexamplepath = os.path.join(
                    self.base_path, "data", "pythonic_plans", f"{prompt_allocation_set}_teamproblem.py"
                )
                problem_examplecontent = file_processor.read_file(problem_fileexamplepath) or ""

                prompt = (
                    "\n" + problem_examplecontent +
                    "Strictly follow the structure and finish the tasks like example\n"
                    "Subtask examination from action perspective:" + subtask +
                    "\nDomain file content:" + all_domain_contents +
                    "\n based on the objects available below." + objects_ai +
                    "Task description: extract the problem files, based on the objects above, "
                    "the preconditions, actions, and subtask examination.\n"
                    "#IMPORTANT, strictly follow the structure, stop generating after the Problem file generation is done."
                )
                prompt_path = f"05_problem_generation/prompts/subtask_{subtask_idx:02d}_prompt.txt"
                output_path = f"05_problem_generation/outputs/subtask_{subtask_idx:02d}_problem.pddl"
                self._write_text_artifact(prompt_path, prompt)

                messages = [
                    {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                    {"role": "user", "content": prompt}
                ]
                _, text = llm.query_model(messages, model, max_tokens=1000, frequency_penalty=0.4)

                problem_pddl.append(text)
                self._write_text_artifact(output_path, text)

            else:
                # Single-robot case
                if not normalized_robot_numbers:
                    print("No robot number found in Assigned Robot; skipping.")
                    print("Assigned Robots content:", assigned_robots)
                    continue

                # Deterministic single-robot choice: if multiple remain, take smallest
                if len(normalized_robot_numbers) > 1:
                    try:
                        smallest = min(int(n[len("robot"):]) for n in normalized_robot_numbers)
                        normalized_robot_numbers = [f"robot{smallest}"]
                    except ValueError:
                        normalized_robot_numbers = [normalized_robot_numbers[0]]

                robotassignnumber = f"{normalized_robot_numbers[0].replace(' ', '')}.pddl"
                domain_path = os.path.join(self.base_path, "resources", robotassignnumber)
                # print("this is a solo work")
                # print(domain_path)

                domain_content = file_processor.read_file(domain_path) or ""
                if not domain_content:
                    print(f"Domain file not found or empty: {domain_path}")
                    continue

                problem_fileexamplepath = os.path.join(
                    self.base_path, "data", "pythonic_plans", f"{prompt_allocation_set}_problem.py"
                )
                problem_examplecontent = file_processor.read_file(problem_fileexamplepath) or ""

                prompt = (
                    "\n" + problem_examplecontent +
                    " Finish the tasks like example\n"
                    "Subtask examination from action perspective:" + subtask +
                    "\nDomain file content:" + domain_content +
                    "\n based on the objects available for potential usage below." + objects_ai +
                    "Task description: generate the problem file. Based on the objects above, "
                    "the domain file preconditions, actions, and subtask examination. "
                    "IMPORTANT the robot initiates strictly as not inaction and robot "
                    "(which includes location)\n"
                    "#IMPORTANT, strictly follow the structure, stop generating after the Problem file generation is done."
                )
                prompt_path = f"05_problem_generation/prompts/subtask_{subtask_idx:02d}_prompt.txt"
                output_path = f"05_problem_generation/outputs/subtask_{subtask_idx:02d}_problem.pddl"
                self._write_text_artifact(prompt_path, prompt)
            
                messages = [
                    {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                    {"role": "user", "content": prompt}
                ]
                _, text = llm.query_model(messages, model, max_tokens=1400, frequency_penalty=0.4)

                problem_pddl.append(text)
                self._write_text_artifact(output_path, text)

        return problem_pddl



    #PG: Edited the following function
    '''
    def problemextracting(
        self,
        subtasks: List[str],
        llm: 'LLMHandler',
        model: str,
        file_processor: 'FileProcessor',
        objects_ai: str,
        prompt_allocation_set: str
    ) -> List[str]:
        """Extract problem files from subtasks."""
        import os, re

        problem_pddl: List[str] = []

        # Same robust matchers used in split_and_store_tasks
        assigned_block_re = re.compile(
            r'(?is)\*\*Assigned\s*Robots?\*\*\s*:\s*(.*?)\n\*\*Objects\s*Involved\*\*\s*:',
            re.IGNORECASE | re.DOTALL
        )
        assigned_block_fallback = re.compile(
            r'(?is)\bAssigned\s*Robots?\b\s*:\s*(.*?)\n\bObjects\s*Involved\b\s*:',
            re.IGNORECASE | re.DOTALL
        )

        # Replace the robot_num_re with plural support
        robot_num_re = re.compile(r'\brobot\s*(\d+)\b', re.IGNORECASE)

        #print("Extracting problems")
        for subtask in subtasks:
            # print("subtasks are:")
            # print("Subtask:", subtask)
            # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

            m = assigned_block_re.search(subtask) or assigned_block_fallback.search(subtask)
            # print("assigned_robots_match is")
            # print(m)
            # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

            if not m:
                print("Invalid subtask structure, skipping")
                continue

            assigned_robots = m.group(1).strip()

            # Team detection
            is_team = ("team" in assigned_robots.lower()) or ("allactionrobot" in assigned_robots.lower())
            robot_numbers = robot_num_re.findall(assigned_robots)
            normalized_robot_numbers = [f"robot{num}" for num in robot_numbers]

            if is_team or len(normalized_robot_numbers) > 1:
                # print("this is a team task")
                # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                # print("Normalized robot numbers", normalized_robot_numbers)
                # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                # Concatenate all team domains
                all_domain_contents = ""
                for robot in normalized_robot_numbers:
                    domain_path = os.path.join(self.base_path, "resources", f"{robot}.pddl")
                    all_domain_contents += file_processor.read_file(domain_path)

                if not all_domain_contents:
                    print("No team robot domains found; skipping team prompt.")
                    continue

                problem_fileexamplepath = os.path.join(
                    self.base_path, "data", "pythonic_plans", f"{prompt_allocation_set}_teamproblem.py"
                )
                problem_examplecontent = file_processor.read_file(problem_fileexamplepath)

                prompt = (
                    "\n" + problem_examplecontent +
                    "Strictly follow the structure and finish the tasks like example\n"
                    "Subtask examination from action perspective:" + subtask +
                    "\nDomain file content:" + all_domain_contents +
                    "\n based on the objects availiable below." + objects_ai +
                    "Task description: extract out the problem files, based on the objects above, "
                    "the precondition, actions and subtask examination.\n"
                    "#IMPORTANT, strictly follow the structure ,stop generate after the Problem file generation is done."
                )

                if "gpt" not in model:
                    _, text = llm.query_model(prompt, model, max_tokens=1000, stop=["def"], frequency_penalty=0.30)
                else:
                    messages = [
                        {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                        {"role": "user", "content": prompt}
                    ]
                    _, text = llm.query_model(messages, model, max_tokens=1000, frequency_penalty=0.4)

                # print("Text", text)
                # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                problem_pddl.append(text)

            else:
                # Single-robot case
                # print("Normalized robot numbers", normalized_robot_numbers)
                # print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                if not normalized_robot_numbers:
                    print("No robot number found in Assigned Robot; skipping.")
                    print("Assigned Robots content:", assigned_robots)
                    continue

                robotassignnumber = f"{normalized_robot_numbers[0].replace(' ', '')}.pddl"
                domain_path = os.path.join(self.base_path, "resources", robotassignnumber)
                print("this is a solo work")
                print(domain_path)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                domain_content = file_processor.read_file(domain_path)
                problem_fileexamplepath = os.path.join(
                    self.base_path, "data", "pythonic_plans", f"{prompt_allocation_set}_problem.py"
                )
                problem_examplecontent = file_processor.read_file(problem_fileexamplepath)

                prompt = (
                    "\n" + problem_examplecontent +
                    " Finish the tasks like example\n"
                    "Subtask examination from action perspective:" + subtask +
                    "\nDomain file content:" + domain_content +
                    "\n based on the objects availiable for potential usage below." + objects_ai +
                    "Task description: generate the problem file. Based on the objects above, "
                    "the domain file precondition, actions and subtask examination. "
                    "IMPORTANT the robot initate strictly as not inaction and robot "
                    "(which includes location)\n"
                    "#IMPORTANT, strictly follow the structure ,stop generate after the Problem file generation is done."
                )

                if "gpt" not in model:
                    _, text = llm.query_model(prompt, model, max_tokens=1000, stop=["def"], frequency_penalty=0.30)
                else:
                    messages = [
                        {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                        {"role": "user", "content": prompt}
                    ]
                    _, text = llm.query_model(messages, model, max_tokens=1400, frequency_penalty=0.4)

                #print("Text", text)
                #print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                problem_pddl.append(text)

        # print("the problem_pddl is")
        # print(problem_pddl)
        return problem_pddl

    #PG: Commented the following
 
    def problemextracting(
        self,
        subtasks: List[str],
        llm: LLMHandler,
        model: str,
        file_processor: FileProcessor,
        objects_ai: str,
        prompt_allocation_set: str
        ) -> List[str]:
        """Extract problem files from subtasks."""
        problem_pddl: List[str] = []
        
        print("Extracting problems")
        for subtask in subtasks:
            print("subtasks are:")
            print(subtask)
            print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


            # Extract assigned robots using regex
            pattern = re.compile(r"\s*\*{0,2}\s*Assigned\s*Robot\s*\??:?\*{0,2}\s*\??(.*?)\s*\*{0,2}\s*Objects\s*Involved\s*:\??\*{0,2}", re.DOTALL | re.IGNORECASE)
            assigned_robots_match = pattern.search(subtask)
            print("assigned_robots_match is")
            print(assigned_robots_match)
            print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

            if not assigned_robots_match:
                print("Invalid subtask structure, skipping")
                continue

            assigned_robots = assigned_robots_match.group(1).strip()
            if "team" in assigned_robots.lower() or "allactionrobot" in assigned_robots.lower():
                print("this is a team task")
                print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                # Handle team robots
                all_domain_contents = ""
                team_pattern = re.compile(r"\s*\*{0,2}\s*robot\s*\??\s*(\d+)\*{0,2}", re.IGNORECASE)
                robot_numbers = team_pattern.findall(assigned_robots)
                normalized_robot_numbers = [f"robot{num}" for num in robot_numbers]
                print("Normalized robot numbers", normalized_robot_numbers)
                print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                for robot in normalized_robot_numbers:
                    domain_path = os.path.join(self.base_path, "resources", f"{robot}.pddl")
                    domain_content = file_processor.read_file(domain_path)
                    all_domain_contents += domain_content

                problem_fileexamplepath = os.path.join(self.base_path, "data", "pythonic_plans", f"{prompt_allocation_set}_teamproblem.py")
                problem_examplecontent = file_processor.read_file(problem_fileexamplepath)

                prompt = (
                    "\n" + problem_examplecontent +
                    "Strictly follow the structure and finish the tasks like example\n"
                    "Subtask examination from action perspective:" + subtask +
                    "\nDomain file content:" + domain_content +
                    "\n based on the objects availiable below." + objects_ai +
                    "Task description: extract out the problem files, based on the objects above, "
                    "the precondition, actions and subtask examination.\n"
                    "#IMPORTANT, strictly follow the structure ,stop generate after the Problem file generation is done."
                )

if "gpt" not in model:
                    _, text = llm.query_model(prompt, model, max_tokens=1000, stop=["def"], frequency_penalty=0.30)
                else:
                    messages = [
                        {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                        {"role": "user", "content": prompt}
                    ]
                    _, text = llm.query_model(messages, model, max_tokens=1400, frequency_penalty=0.4)


                print("Text", text)
                print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                problem_pddl.append(text)

            else:
                # Handle single robot
                robot_pattern = re.compile(r"\s*\*{0,2}\s*robot\s*\??\s*(\d+)\*{0,2}", re.IGNORECASE)
                robot_numbers = robot_pattern.findall(assigned_robots)
                normalized_robot_numbers = [f"robot{num}" for num in robot_numbers]
                print("Normalized robot numbers", normalized_robot_numbers)
                print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                
                if robot_numbers:
                    robotassignnumber = f"{normalized_robot_numbers[0].replace(' ', '')}.pddl"
                    domain_path = os.path.join(self.base_path, "resources", robotassignnumber)
                    print("this is a solo work")
                    print(domain_path)
                    print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

                    domain_content = file_processor.read_file(domain_path)
                    problem_fileexamplepath = os.path.join(self.base_path, "data", "pythonic_plans", f"{prompt_allocation_set}_problem.py")
                    problem_examplecontent = file_processor.read_file(problem_fileexamplepath)

                    prompt = (
                        "\n" + problem_examplecontent +
                        " Finish the tasks like example\n"
                        "Subtask examination from action perspective:" + subtask +
                        "\nDomain file content:" + domain_content +
                        "\n based on the objects availiable for potential usage below." + objects_ai +
                        "Task description: generate the problem file. Based on the objects above, "
                        "the domain file precondition, actions and subtask examination. "
                        "IMPORTANT the robot initate strictly as not inaction and robot "
                        "(which includes location)\n"
                        "#IMPORTANT, strictly follow the structure ,stop generate after the Problem file generation is done."
                    )

                    if "gpt" not in model:
                        _, text = llm.query_model(prompt, model, max_tokens=1000, stop=["def"], frequency_penalty=0.30)
                    else:            
                        messages = [
                            {"role": "system", "content": "You are a Robot PDDL problem Expert"},
                            {"role": "user", "content": prompt}
                        ]
                        _, text = llm.query_model(messages, model, max_tokens=1400, frequency_penalty=0.4)
                    
                    print("Text", text)
                    print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
                    problem_pddl.append(text)
                    
        print("the problem_pddl is")
        print(problem_pddl)
        return problem_pddl
    '''
    
    
    
    def _validate_and_plan(self) -> None:
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
            self.run_planners()
            #input("Press Enter to continue")
            
        except Exception as e:
            raise PDDLError(f"Error in validation and planning: {str(e)}")

    def run_llmvalidator(self) -> None:
        """Run LLM validation on problem files."""
        try:
            problem_files = [f for f in os.listdir(self.file_processor.subtask_path) if f.endswith('.pddl')]
            validation_records = []
            for problem_file in problem_files:
                try:
                    problem_file_full = os.path.join(self.file_processor.subtask_path, problem_file)
                    domain_name = self.file_processor.extract_domain_name(problem_file_full)
                    if not domain_name:
                        print(f"No domain specified in {problem_file}")
                        continue

                    domain_file = self.file_processor.find_domain_file(domain_name)
                    if not domain_file:
                        print(f"No domain file found for domain {domain_name}")
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
                    output_path = f"07_validate/outputs/{safe_name}_validated.pddl"
                    self._write_text_artifact(input_path, problem_content)
                    self._write_text_artifact(prompt_path, prompt)
                
                    messages = [{"role": "system", "content": "You are a Robot PDDL problem Expert"},
                            {"role": "user", "content": prompt}]
                    _, text = self.llm.query_model(messages, self.model, max_tokens=1400, frequency_penalty=0.4)

                    self.validated_plan.append(text)  #PG: Store validated plan
                    code_plan = [text]
                    self._write_text_artifact(output_path, text)
                    validation_manifest = self.file_processor.split_pddl_tasks(
                        code_plan,
                        True,
                        output_directory=os.path.join(self.current_task_run_dir, "07_validate", "validated_subtask")
                    )  #PG: Use True to indicate validated
                    validation_records.append({
                        "problem_file": problem_file,
                        "input_path": input_path,
                        "prompt_path": prompt_path,
                        "output_path": output_path,
                        "validated_split_files": validation_manifest,
                    })
                    
                except Exception as e:
                    print(f"Error processing file {problem_file}: {str(e)}")
                    continue
            self._write_json_artifact("07_validate/validation_manifest.json", validation_records)
            self._record_artifact("validate", "manifest", "07_validate/validation_manifest.json")
            self._persist_manifest()
                    
        except Exception as e:
            print(f"Error in run_llmvalidator: {str(e)}")
            raise

    def run_planners(self) -> None:
        """Run PDDL planners on problem files."""
        try:
            planner_path = os.path.join(self.base_path, "downward", "fast-downward.py")
            problem_files = [f for f in os.listdir(self.file_processor.validated_subtask_path) if f.endswith('.pddl')]  #PG: Changed to validated_subtask_path
            planner_records = []
            for problem_file in problem_files:
                try:
                    problem_file_full = os.path.join(self.file_processor.validated_subtask_path, problem_file) #PG: Changed to validated_subtask_path
                    domain_name = self.file_processor.extract_domain_name(problem_file_full)
                    if not domain_name:
                        print(f"No domain specified in {problem_file}")
                        continue

                    domain_file = self.file_processor.find_domain_file(domain_name)
                    if not domain_file:
                        print(f"No domain file found for domain {domain_name}")
                        continue

                    command = [
                        planner_path,
                        "--alias",
                        "seq-opt-lmcut",
                        domain_file,
                        problem_file_full
                    ]
                    started_at = time.time()

                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=300  # 5 minute timeout
                    )
                    
                    output_file = os.path.join(self.file_processor.validated_subtask_path, problem_file.replace('.pddl', '_plan.txt')) #PG: Changed to validated_subtask_path from subtask_path
                    self.file_processor.write_file(output_file, result.stdout)
                    safe_name = self._sanitize_filename(problem_file.replace(".pddl", ""))
                    command_path = f"08_planner/commands/{safe_name}_command.txt"
                    stdout_path = f"08_planner/stdout/{safe_name}_stdout.txt"
                    stderr_path = f"08_planner/stderr/{safe_name}_stderr.txt"
                    self._write_text_artifact(command_path, " ".join(command))
                    self._write_text_artifact(stdout_path, result.stdout)
                    self._write_text_artifact(stderr_path, result.stderr)
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
            self._write_json_artifact("08_planner/planner_manifest.json", planner_records)
            self._record_artifact("planner", "manifest", "08_planner/planner_manifest.json")
            self._persist_manifest()
                    
        except Exception as e:
            print(f"Error in run_planners: {str(e)}")
            raise

    def _combine_all_plans(self, decomposed_plan: Union[str, List[str]]) -> str:
        """Combine all generated plan files into a single plan.
 
        """
        # Handle list input by taking first plan
        if isinstance(decomposed_plan, list):
            decomposed_plan = decomposed_plan[0]
        
        base_path = os.path.join(self.resources_path, "validated_subtask")  #PG: Changed to validated_subtask from generated_subtask
        plan_files = [f for f in os.listdir(base_path) if f.endswith('_plan.txt')]
        prompt = ""
        # Add plans from files if they exist
        if plan_files:
            for idx, filename in enumerate(plan_files):
                filepath = os.path.join(base_path, filename)
                content = self.file_processor.read_file(filepath)
                plan = self.file_processor.extract_plan_from_output(content)
                if plan:  # Only add non-empty plans
                    prompt += f"\nPlan {idx + 1}:\n{plan}\n"
        
        # Add allocation examination and initial plan
        prompt += "\nallocation examination\n"
        prompt += self.sequence_operations if hasattr(self, 'sequence_operations') else ""
        prompt += "\ninitial plan examination\n"
        prompt += decomposed_plan
        
        prompt += ("\nyou are robot allocation expert, Your task is, based on inital plan examination "
                  "and allocation examination correct the subplans. Then based on your understanding "
                  "merge the subtasks together by using timed durative actions format, where parallel "
                  "tasks are performed at the same time. IMPORTANT: all 'variablelocation' should be "
                  "corrected to variable itself, since variable itself includes location. and result "
                  "must be in PDDL plan format.")
        self._write_text_artifact("09_combine/01_combine_prompt.txt", prompt)
        self._record_artifact("combine", "prompt", "09_combine/01_combine_prompt.txt")
        
        messages = [{"role": "user", "content": prompt}]
        _, text = self.llm.query_model(messages, self.model, max_tokens=1300, frequency_penalty=0.0)
        self._write_text_artifact("09_combine/02_combined_plan.txt", text)
        self._record_artifact("combine", "output", "09_combine/02_combined_plan.txt")
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
        self._write_text_artifact("10_final_match/01_match_prompt.txt", prompt)
        self._record_artifact("final_match", "prompt", "10_final_match/01_match_prompt.txt")
        
        messages = [{"role": "user", "content": prompt}]
        _, text = self.llm.query_model(messages, self.model, max_tokens=1300, frequency_penalty=0.0)
        self._write_text_artifact("10_final_match/02_final_plan.txt", text)
        self._record_artifact("final_match", "output", "10_final_match/02_final_plan.txt")
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

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bddl-file", type=str, help="Path to BDDL file")
    parser.add_argument(
        "--floor-plan", 
        type=int, 
        required=False,  # Changed from True
        help="Required unless --bddl-file is provided"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
        choices=get_available_models()
    )
    parser.add_argument(
        "--prompt-decompse-set",
        type=str,
        default="pddl_train_task_decomposesep",
        choices=['pddl_train_task_decompose']
    )
    parser.add_argument(
        "--prompt-allocation-set",
        type=str,
        default="pddl_train_task_allocationsep",
        choices=['pddl_train_task_allocation']
    )
    parser.add_argument(
        "--test-set",
        type=str,
        default="final_test",
        choices=['final_test']
    )
    parser.add_argument("--log-results", type=bool, default=True)
    
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
        
        # Initialize task manager
        task_manager = TaskManager(
            base_path=os.getcwd(),
            model=args.model,
            prompt_decompse_set=args.prompt_decompse_set,
            prompt_allocation_set=args.prompt_allocation_set
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
            # Original workflow
            test_file = os.path.join("data", args.test_set, f"FloorPlan{args.floor_plan}.jsonl")
            test_tasks, available_robots, gt_test_tasks, trans_cnt_tasks, min_trans_cnt_tasks = \
                task_manager.load_dataset(test_file)
            
            print(f"\n----Test set tasks----\n{test_tasks}\nTotal: {len(test_tasks)} tasks\n")
            
            # Get AI2thor objects 
            objects_ai = f"\n\nobjects = {PDDLUtils.get_ai2_thor_objects(args.floor_plan)}"
            
            # Process tasks with objects_ai
            task_manager.process_tasks(test_tasks, available_robots, objects_ai)
            
            # Log results if enabled
            if args.log_results:
                for idx, task in enumerate(test_tasks):
                    task_manager.log_results(
                        task=task,
                        idx=idx,
                        available_robots=available_robots,
                        gt_test_tasks=gt_test_tasks,
                        trans_cnt_tasks=trans_cnt_tasks,
                        min_trans_cnt_tasks=min_trans_cnt_tasks,
                        objects_ai=objects_ai
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



    




