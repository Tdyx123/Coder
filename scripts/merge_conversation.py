import json
import os
import os.path

from typing import Dict, List

from parsing_utils import ParsingUtils

def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def get_all_task_run_dirs(summary_path: str) -> List[str]:
    with open(summary_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    result = []
    for summary in data.get("summaries", []):
        for r in summary.get("results", []):
            if "task_run_dir" in r:
                result.append(r["task_run_dir"])
    return result

def append_conversation(file_path0: str, file_path1: str, output_path:str, system_message: str = None):
    content0 = read_file(file_path0)
    content1 = read_file(file_path1)

    if system_message is None:
        data = {"messages": [
            {"role": "user", "content": content0},
            {"role": "assistant", "content": content1}
        ]}
    else:
        data = {"messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": content0},
            {"role": "assistant", "content": content1}
        ]}

    with open(output_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")

def _normalize_floor_plan(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]
    return text

def _extract_decomposition_subtasks(decomposed_plan: str) -> List[str]:
    if not any(ParsingUtils.is_subtask_header_line(line) for line in decomposed_plan.splitlines()):
        return [decomposed_plan.strip()] if decomposed_plan.strip() else []

    subtasks = ParsingUtils.extract_subtask_blocks(decomposed_plan, normalize_headers=True)
    filtered_subtasks = []

    for subtask in subtasks:
        lines = subtask.splitlines()

        while lines and ParsingUtils.is_subtask_trailing_line(lines[-1]):
            lines.pop()

        if len(lines) < 10:
            continue

        filtered_subtasks.append("\n".join(lines))

    return filtered_subtasks

def _flatten_summary_results(summary_data: Dict) -> List[Dict]:
    flat_results = []
    for floor_summary in summary_data.get("summaries", []):
        for result in floor_summary.get("results", []):
            flat_results.append(result)

    if not flat_results:
        flat_results.extend(summary_data.get("results", []))

    return flat_results

def _resolve_data_root(summary_data: Dict, data_root: str) -> str:
    if os.path.isabs(data_root):
        return data_root

    repo_root = summary_data.get("repo_root")
    if repo_root:
        return os.path.join(repo_root, data_root)

    return os.path.abspath(data_root)

def _load_jsonl_record(jsonl_path: str, task_index: int) -> Dict:
    if not os.path.exists(jsonl_path):
        raise FileNotFoundError(f"Dataset JSONL not found: {jsonl_path}")

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for idx, raw_line in enumerate(f):
            line = raw_line.strip()
            if not line:
                continue
            if idx == task_index:
                return json.loads(line)

    raise IndexError(f"Task index {task_index} is out of range for {jsonl_path}")

def check_decompose_subtask_count(summary_path: str, index: int, data_root: str = "data") -> Dict:
    """Check one flattened summary result against its dataset JSONL subtask count."""
    if index < 0:
        raise IndexError(f"Flat index must be non-negative, got {index}")

    with open(summary_path, "r", encoding="utf-8") as f:
        summary_data = json.load(f)

    flat_results = _flatten_summary_results(summary_data)
    if index >= len(flat_results):
        raise IndexError(f"Flat index {index} is out of range for {len(flat_results)} result(s)")

    result = flat_results[index]
    task_run_dir = result.get("task_run_dir")
    if not task_run_dir:
        raise ValueError(f"Missing task_run_dir for flat index {index}")

    decompose_output_path = os.path.join(task_run_dir, "01_decompose", "02_decompose_output.txt")
    if not os.path.exists(decompose_output_path):
        raise FileNotFoundError(f"Decompose output not found: {decompose_output_path}")

    floor_plan = _normalize_floor_plan(result.get("floor_plan"))
    if not floor_plan:
        raise ValueError(f"Missing floor_plan for flat index {index}")

    if "task_index" not in result:
        raise ValueError(f"Missing task_index for flat index {index}")
    task_index = int(result["task_index"])

    test_set = summary_data.get("test_set")
    if not test_set:
        raise ValueError(f"Missing test_set in summary: {summary_path}")

    jsonl_path = os.path.join(_resolve_data_root(summary_data, data_root), test_set, f"FloorPlan{floor_plan}.jsonl")
    record = _load_jsonl_record(jsonl_path, task_index)

    decompose_count = len(_extract_decomposition_subtasks(read_file(decompose_output_path)))
    jsonl_count = len(record.get("subtasks", []))

    return {
        "matched": decompose_count == jsonl_count,
        "decompose_count": decompose_count,
        "jsonl_count": jsonl_count,
        "floor_plan": floor_plan,
        "task_index": task_index,
        "flat_index": index,
        "task_run_dir": task_run_dir,
        "decompose_output_path": decompose_output_path,
        "jsonl_path": jsonl_path,
    }

def check_allocate_assignment_count(summary_path: str, index: int, data_root: str = "data") -> Dict:
    """Check one flattened summary result against its dataset JSONL assignment count."""
    if index < 0:
        raise IndexError(f"Flat index must be non-negative, got {index}")

    with open(summary_path, "r", encoding="utf-8") as f:
        summary_data = json.load(f)

    flat_results = _flatten_summary_results(summary_data)
    if index >= len(flat_results):
        raise IndexError(f"Flat index {index} is out of range for {len(flat_results)} result(s)")

    result = flat_results[index]
    task_run_dir = result.get("task_run_dir")
    if not task_run_dir:
        raise ValueError(f"Missing task_run_dir for flat index {index}")

    allocate_output_path = os.path.join(task_run_dir, "02_allocate", "02_allocate_output.txt")
    if not os.path.exists(allocate_output_path):
        raise FileNotFoundError(f"Allocate output not found: {allocate_output_path}")

    floor_plan = _normalize_floor_plan(result.get("floor_plan"))
    if not floor_plan:
        raise ValueError(f"Missing floor_plan for flat index {index}")

    if "task_index" not in result:
        raise ValueError(f"Missing task_index for flat index {index}")
    task_index = int(result["task_index"])

    test_set = summary_data.get("test_set")
    if not test_set:
        raise ValueError(f"Missing test_set in summary: {summary_path}")

    jsonl_path = os.path.join(_resolve_data_root(summary_data, data_root), test_set, f"FloorPlan{floor_plan}.jsonl")
    record = _load_jsonl_record(jsonl_path, task_index)

    sequence_operations = ParsingUtils.extract_sequence_operations(read_file(allocate_output_path))
    assignments = ParsingUtils.extract_robot_assignments(sequence_operations)
    allocate_count = len(assignments)
    jsonl_count = len(record.get("assigned_robots", []))

    return {
        "matched": allocate_count == jsonl_count,
        "allocate_count": allocate_count,
        "jsonl_count": jsonl_count,
        "assignments": assignments,
        "sequence_operations": sequence_operations,
        "floor_plan": floor_plan,
        "task_index": task_index,
        "flat_index": index,
        "task_run_dir": task_run_dir,
        "allocate_output_path": allocate_output_path,
        "jsonl_path": jsonl_path,
    }

def check_validate_output_count(summary_path: str, index: int, data_root: str = "data") -> Dict:
    """Check one flattened summary result against its dataset JSONL validated PDDL count."""
    if index < 0:
        raise IndexError(f"Flat index must be non-negative, got {index}")

    with open(summary_path, "r", encoding="utf-8") as f:
        summary_data = json.load(f)

    flat_results = _flatten_summary_results(summary_data)
    if index >= len(flat_results):
        raise IndexError(f"Flat index {index} is out of range for {len(flat_results)} result(s)")

    result = flat_results[index]
    task_run_dir = result.get("task_run_dir")
    if not task_run_dir:
        raise ValueError(f"Missing task_run_dir for flat index {index}")

    validate_outputs_dir = os.path.join(task_run_dir, "07_validate", "outputs")
    if not os.path.isdir(validate_outputs_dir):
        raise FileNotFoundError(f"Validate outputs directory not found: {validate_outputs_dir}")

    floor_plan = _normalize_floor_plan(result.get("floor_plan"))
    if not floor_plan:
        raise ValueError(f"Missing floor_plan for flat index {index}")

    if "task_index" not in result:
        raise ValueError(f"Missing task_index for flat index {index}")
    task_index = int(result["task_index"])

    test_set = summary_data.get("test_set")
    if not test_set:
        raise ValueError(f"Missing test_set in summary: {summary_path}")

    jsonl_path = os.path.join(_resolve_data_root(summary_data, data_root), test_set, f"FloorPlan{floor_plan}.jsonl")
    record = _load_jsonl_record(jsonl_path, task_index)

    validated_paths = sorted(
        os.path.join(validate_outputs_dir, f)
        for f in os.listdir(validate_outputs_dir)
        if f.endswith("_validated.pddl") and os.path.isfile(os.path.join(validate_outputs_dir, f))
    )
    validated_count = len(validated_paths)
    jsonl_count = len(record.get("subtasks", []))

    return {
        "matched": validated_count == jsonl_count,
        "validated_count": validated_count,
        "jsonl_count": jsonl_count,
        "validated_paths": validated_paths,
        "floor_plan": floor_plan,
        "task_index": task_index,
        "flat_index": index,
        "task_run_dir": task_run_dir,
        "validate_outputs_dir": validate_outputs_dir,
        "jsonl_path": jsonl_path,
    }

def check_planner_plan_count(summary_path: str, index: int, data_root: str = "data") -> Dict:
    """Check one flattened summary result against its dataset JSONL planner plan count."""
    if index < 0:
        raise IndexError(f"Flat index must be non-negative, got {index}")

    with open(summary_path, "r", encoding="utf-8") as f:
        summary_data = json.load(f)

    flat_results = _flatten_summary_results(summary_data)
    if index >= len(flat_results):
        raise IndexError(f"Flat index {index} is out of range for {len(flat_results)} result(s)")

    result = flat_results[index]
    task_run_dir = result.get("task_run_dir")
    if not task_run_dir:
        raise ValueError(f"Missing task_run_dir for flat index {index}")

    planner_outputs_dir = os.path.join(task_run_dir, "08_planner", "outputs")
    if not os.path.isdir(planner_outputs_dir):
        raise FileNotFoundError(f"Planner outputs directory not found: {planner_outputs_dir}")

    floor_plan = _normalize_floor_plan(result.get("floor_plan"))
    if not floor_plan:
        raise ValueError(f"Missing floor_plan for flat index {index}")

    if "task_index" not in result:
        raise ValueError(f"Missing task_index for flat index {index}")
    task_index = int(result["task_index"])

    test_set = summary_data.get("test_set")
    if not test_set:
        raise ValueError(f"Missing test_set in summary: {summary_path}")

    jsonl_path = os.path.join(_resolve_data_root(summary_data, data_root), test_set, f"FloorPlan{floor_plan}.jsonl")
    record = _load_jsonl_record(jsonl_path, task_index)

    plan_paths = sorted(
        os.path.join(planner_outputs_dir, f)
        for f in os.listdir(planner_outputs_dir)
        if f.endswith("_plan.txt") and os.path.isfile(os.path.join(planner_outputs_dir, f))
    )
    planner_count = len(plan_paths)
    jsonl_count = len(record.get("subtasks", []))

    return {
        "matched": planner_count == jsonl_count,
        "planner_count": planner_count,
        "jsonl_count": jsonl_count,
        "plan_paths": plan_paths,
        "floor_plan": floor_plan,
        "task_index": task_index,
        "flat_index": index,
        "task_run_dir": task_run_dir,
        "planner_outputs_dir": planner_outputs_dir,
        "jsonl_path": jsonl_path,
    }

if __name__ == "__main__":
    output_path_base = "/data/dwb/datasets/0518"
    summary_path = os.path.join("/home/dwb/thor/LaMMA-P/parallel_runs",                    
        "pddlrun_llmseparate_20260506_180812",
        "summary.json")


    for index, task_run_dir in enumerate(get_all_task_run_dirs(summary_path)):
        if not (check_decompose_subtask_count(summary_path, index))["matched"]:
            continue
        if not (check_allocate_assignment_count(summary_path, index))["matched"]:
            continue
        if not (check_validate_output_count(summary_path, index))["matched"]:
            continue
        if not (check_planner_plan_count(summary_path, index))["matched"]:
            continue


        file_path0 = os.path.join(task_run_dir, "01_decompose",  "01_decompose_prompt.txt")
        file_path1 = os.path.join(task_run_dir, "01_decompose",  "02_decompose_output.txt")
        append_conversation(file_path0, file_path1, os.path.join(output_path_base,"01_decompose.jsonl"))

        file_path0 = os.path.join(task_run_dir, "02_allocate",  "01_allocate_prompt.txt")
        file_path1 = os.path.join(task_run_dir, "02_allocate",  "02_allocate_output.txt")
        append_conversation(file_path0, file_path1, os.path.join(output_path_base,"02_allocate.jsonl"))

        problem_generation_path = os.path.join(task_run_dir, "05_problem_generation")
        for f in os.listdir(os.path.join(problem_generation_path, "prompts")):
            problem_path = os.path.join(problem_generation_path, "outputs", f.replace("_prompt.txt", "_problem.pddl"))
            if os.path.exists(problem_path):
                append_conversation(os.path.join(problem_generation_path, "prompts", f), problem_path, os.path.join(output_path_base,"05_problem_generation.jsonl"))
        
        validate_path = os.path.join(task_run_dir, "07_validate")
        for f in os.listdir(os.path.join(validate_path, "prompts")):
            problem_path = os.path.join(validate_path, "outputs", f.replace("_prompt.txt", "_validated.pddl"))
            if os.path.exists(problem_path):
                append_conversation(os.path.join(validate_path, "prompts", f), problem_path, os.path.join(output_path_base,"07_validate.jsonl"))




        
