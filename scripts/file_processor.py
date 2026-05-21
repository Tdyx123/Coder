import glob
import json
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from parsing_utils import ParsingUtils
from run_config import RunConfig, load_run_config


class PDDLError(Exception):
    """Base exception class for PDDL-related errors."""
    pass


class FileProcessor:
    """Handles file operations and text processing for PDDL files.

    This class manages reading, writing, and processing of PDDL files and related
    text content. It provides methods for file operations and text manipulation
    specific to PDDL task processing.
    """

    def __init__(
        self,
        base_path: str,
        config: Optional[RunConfig] = None,
        subtask_path: Optional[str] = None,
        validated_subtask_path: Optional[str] = None,
        each_run_path: Optional[str] = None,
    ):
        """Initialize the file processor.

        Args:
            base_path (str): Base path for file operations
        """
        self.base_path = base_path
        self.config = config or load_run_config(base_path, error_cls=PDDLError)
        self.subtask_path = ""
        self.validated_subtask_path = ""
        self.each_run_path = ""
        self.configure_workspace(
            subtask_path=subtask_path or str(self.config.path("storage", "default_generated_subtask_dir")),
            validated_subtask_path=validated_subtask_path or str(self.config.path("storage", "default_validated_subtask_dir")),
            each_run_path=each_run_path or str(self.config.path("storage", "default_each_run_dir")),
        )

    def configure_workspace(
        self,
        subtask_path: str,
        validated_subtask_path: str,
        each_run_path: str,
    ) -> None:
        """Configure the active workspace directories for generated artifacts."""
        self.subtask_path = subtask_path
        self.validated_subtask_path = validated_subtask_path
        self.each_run_path = each_run_path
        os.makedirs(self.subtask_path, exist_ok=True)
        os.makedirs(self.validated_subtask_path, exist_ok=True)
        os.makedirs(self.each_run_path, exist_ok=True)

    def read_file(self, file_path: str) -> str:
        """Read contents of a file.

        Args:
            file_path (str): Path to the file to read

        Returns:
            str: Contents of the file
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
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
            with open(file_path, 'w', encoding='utf-8') as file:
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
        sequence_operations = ""

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

        subtasks = ParsingUtils.extract_subtask_blocks(
            problem_summary,
            allow_bare=True,
            allow_hash_space_separator=False,
        )

        if not subtasks:
            subtasks = [problem_summary.strip()] if problem_summary.strip() else []

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
                call_config = self.config.llm_call("structure_fix")
                _, fixed_subtask = llm.query_model(
                    messages,
                    model,
                    max_tokens=call_config.get("max_tokens", 1400),
                    frequency_penalty=call_config.get("frequency_penalty", 0.4),
                )

                if structure_ok.search(fixed_subtask) or fallback_plain.search(fixed_subtask):
                    fixed_subtasks.append(fixed_subtask)
                else:
                    print("LLM structure fix failed, using original subtask")
                    fixed_subtasks.append(subtask)
            else:
                print("LLM handler not provided, using original subtask")
                fixed_subtasks.append(subtask)

        return fixed_subtasks, sequence_operations

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
            domain_path = str(self.config.robot_domain_path(f"{domain_name}.pddl"))
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

    def extract_plan_from_planfile(self, content: str) -> str:
        """Extract clean plan actions from a planner plan file."""
        if not content or not isinstance(content, str):
            raise ValueError("Invalid content provided to extract_plan_from_planfile")

        try:
            plan_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(";"):
                    continue
                plan_lines.append(stripped)
            return "\n".join(plan_lines)
        except Exception as e:
            print(f"Error extracting plan from plan file: {str(e)}")
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
