#!/usr/bin/env python3
"""Compress SFT user prompts into a stage-aware Qwen token budget."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DEFAULT_DATASET_DIR = Path("/data/dwb/datasets/0819_deepseek")
DEFAULT_TOKENIZER_PATH = Path("/data/dwb/models/Qwen3-8B/tokenizer.json")
DEFAULT_MAX_TOKENS = 1280
STAGE_INPUTS: Tuple[Tuple[str, str], ...] = (
    ("decompose", "01_decompose.jsonl"),
    ("allocate", "02_allocate.jsonl"),
    ("problem_generation", "05_problem_generation.jsonl"),
)


class CompressionError(ValueError):
    """Raised when one JSONL record cannot be compressed safely."""

    def __init__(self, message: str, token_count: Optional[int] = None):
        super().__init__(message)
        self.token_count = token_count


def _balanced_expression(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] != "(":
        raise CompressionError(f"expected '(' at character {start}")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise CompressionError(f"unbalanced parenthesized expression at character {start}")


def _minify_pddl(text: str) -> str:
    without_comments = re.sub(r";[^\n]*", "", text)
    return re.sub(r"\s+", " ", without_comments).strip()


def _pddl_field(action_block: str, field_name: str) -> str:
    field_match = re.search(re.escape(field_name), action_block, flags=re.IGNORECASE)
    if not field_match:
        return ""
    expression_start = action_block.find("(", field_match.end())
    if expression_start < 0:
        return ""
    return _minify_pddl(_balanced_expression(action_block, expression_start))


def _extract_domain(text: str, start: int = 0) -> str:
    match = re.search(r"\(define\s+\(domain\s+[^()\s]+\)", text[start:], flags=re.IGNORECASE)
    if not match:
        raise CompressionError("PDDL domain definition not found")
    domain_start = start + match.start()
    return _balanced_expression(text, domain_start)


def _domain_name(domain: str) -> str:
    match = re.search(r"\(domain\s+([^()\s]+)\)", domain, flags=re.IGNORECASE)
    if not match:
        raise CompressionError("PDDL domain name not found")
    return match.group(1)


def _action_blocks(domain: str) -> List[Tuple[str, str]]:
    actions: List[Tuple[str, str]] = []
    for match in re.finditer(r"\(:action\s+([^()\s]+)", domain, flags=re.IGNORECASE):
        actions.append((match.group(1), _balanced_expression(domain, match.start())))
    if not actions:
        raise CompressionError("PDDL domain contains no actions")
    return actions


def _action_signature(name: str, block: str) -> str:
    parameters = _pddl_field(block, ":parameters")
    return f"{name}{parameters or '()'}"


def _last_literal_assignment(text: str, name: str) -> Any:
    matches = list(re.finditer(rf"(?m)^\s*{re.escape(name)}\s*=\s*(.+)$", text))
    if not matches:
        raise CompressionError(f"{name} assignment not found")
    try:
        return ast.literal_eval(matches[-1].group(1))
    except (SyntaxError, ValueError) as exc:
        raise CompressionError(f"invalid {name} assignment: {exc}") from exc


def _unique_strings(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _clean_heading(text: str) -> str:
    return re.sub(r"[*_`]+", "", text).strip().strip("#").strip()


def _contains_name(text: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
    )


def compress_decompose_content(content: str) -> str:
    task_matches = list(
        re.finditer(r"(?mi)^#\s*Task Description\s*:\s*(.+)$", content)
    )
    if not task_matches:
        raise CompressionError("final task description not found")
    task = task_matches[-1].group(1).strip()

    objects = _last_literal_assignment(content, "objects")
    if not isinstance(objects, list):
        raise CompressionError("objects assignment must be a list")
    object_names = _unique_strings(objects)

    domain = _extract_domain(content)
    signatures = [_action_signature(name, block) for name, block in _action_blocks(domain)]

    return "\n".join(
        [
            (
                "Decompose the task into numbered subtasks. Use only available actions; "
                "list required skills, initial/dependency conditions, action parameters, "
                "preconditions and effects, and identify parallel subtasks. Follow the "
                "target format exactly."
            ),
            "Available actions: " + "; ".join(signatures),
            "Available objects: " + ", ".join(object_names),
            "Task: " + task,
        ]
    )


def _last_marker(text: str, pattern: str, label: str) -> re.Match:
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
    if not matches:
        raise CompressionError(f"{label} marker not found")
    return matches[-1]


def _allocation_summary_lines(body: str) -> List[str]:
    lines: List[str] = []
    seen = set()
    for raw_line in body.splitlines():
        line = _clean_heading(raw_line)
        if not line or re.fullmatch(r"-+", line):
            continue
        if re.fullmatch(r"Subtask\s*\d+\s*:\s*Robot\s*\d+\s*;?", line, re.IGNORECASE):
            continue
        if not re.search(
            r"(?:Task Description|SubTask\s*\d+\s*:|parallel|depend|sequential|independent)",
            line,
            flags=re.IGNORECASE,
        ):
            continue
        if line not in seen:
            seen.add(line)
            lines.append(line)
    if not lines:
        raise CompressionError("actual subtask summary not found")
    return lines


def _required_skills(body: str, robots: Sequence[Mapping[str, Any]]) -> List[str]:
    required: List[str] = []
    seen = set()
    for match in re.finditer(r"Skills Required\s*:\s*([^\)\]\n]+)", body, flags=re.IGNORECASE):
        for value in match.group(1).split(","):
            skill = value.strip(" .#*`_")
            if skill and skill not in seen:
                seen.add(skill)
                required.append(skill)

    known_skills = _unique_strings(
        skill
        for robot in robots
        for skill in robot.get("skills", [])
        if isinstance(skill, str)
    )
    for raw_line in body.splitlines():
        line = _clean_heading(raw_line)
        for skill in known_skills:
            if re.match(rf"{re.escape(skill)}\s*:", line, flags=re.IGNORECASE):
                if skill not in seen:
                    seen.add(skill)
                    required.append(skill)
    return required


def _compact_robots(
    robots: Sequence[Mapping[str, Any]], required_skills: Sequence[str]
) -> List[Dict[str, Any]]:
    required_lower = {skill.lower() for skill in required_skills}
    compact: List[Dict[str, Any]] = []
    for robot in robots:
        skills = robot.get("skills", [])
        if not isinstance(skills, list):
            raise CompressionError("robot skills must be a list")
        relevant_skills = [
            skill
            for skill in skills
            if isinstance(skill, str) and (not required_lower or skill.lower() in required_lower)
        ]
        compact.append(
            {
                "n": robot.get("name"),
                "s": relevant_skills,
                "c": robot.get("mass_capacity"),
            }
        )
    return compact


def _compact_allocation_objects(objects: Sequence[Mapping[str, Any]], body: str) -> List[Dict[str, Any]]:
    relevant: List[Dict[str, Any]] = []
    seen = set()
    for entry in objects:
        name = str(entry.get("name") or "")
        if not name or name in seen or not _contains_name(body, name):
            continue
        seen.add(name)
        relevant.append({"n": name, "m": entry.get("mass")})
    if relevant:
        return relevant
    return [
        {"n": entry.get("name"), "m": entry.get("mass")}
        for entry in objects
        if isinstance(entry, Mapping)
    ]


def compress_allocate_content(content: str) -> str:
    sequence_marker = _last_marker(
        content,
        r"^#\s*Sequence of Operations\s*:\s*$",
        "example sequence",
    )
    allocation_marker = _last_marker(
        content,
        r"^#\s*TASK ALLOCATION\s*$",
        "actual task allocation",
    )
    if allocation_marker.start() <= sequence_marker.end():
        raise CompressionError("actual allocation marker precedes the final example")
    body = content[sequence_marker.end() : allocation_marker.start()].strip()
    summary_lines = _allocation_summary_lines(body)

    robots = _last_literal_assignment(content, "robots")
    objects = _last_literal_assignment(content, "objects")
    if not isinstance(robots, list) or not all(isinstance(item, Mapping) for item in robots):
        raise CompressionError("robots assignment must be a list of objects")
    if not isinstance(objects, list) or not all(isinstance(item, Mapping) for item in objects):
        raise CompressionError("objects assignment must be a list of objects")

    required_skills = _required_skills(body, robots)
    compact_robots = _compact_robots(robots, required_skills)
    compact_objects = _compact_allocation_objects(objects, body)

    return "\n".join(
        [
            (
                "Allocate every subtask to a robot having all required skills. Check mass "
                "capacity only for picked-up objects; ties use the smallest robot id. Preserve "
                "dependencies; parallel assignments share a semicolon-separated line. End "
                "with exactly '# Sequence of Operations:' followed by numeric "
                "'Subtask N: Robot M;' entries and nothing after."
            ),
            "Subtasks and dependencies:",
            *summary_lines,
            "Robots: " + json.dumps(compact_robots, ensure_ascii=False, separators=(",", ":")),
            "Objects: " + json.dumps(compact_objects, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def _problem_subtask_summary(
    subtask_text: str,
    known_action_names: Sequence[str],
) -> Tuple[List[str], List[str]]:
    action_lookup = {name.lower(): name for name in known_action_names}
    normalized_lines = [_clean_heading(line) for line in subtask_text.splitlines()]
    action_indexes: List[int] = []
    actions: List[str] = []
    seen_actions = set()
    for index, line in enumerate(normalized_lines):
        prefix_match = re.match(r"([A-Za-z][A-Za-z0-9_]*)\s*:", line)
        if not prefix_match:
            continue
        action = action_lookup.get(prefix_match.group(1).lower())
        if not action:
            continue
        action_indexes.append(index)
        if action not in seen_actions:
            seen_actions.add(action)
            actions.append(action)

    keep: List[str] = []
    seen_lines = set()
    if action_indexes:
        first_action = action_indexes[0]
        candidates = normalized_lines[:first_action]
        candidates.extend(
            line
            for line in normalized_lines[first_action + 1 :]
            if re.search(r"(?:depend|previous|constraint|not possible|task status)", line, re.IGNORECASE)
            and not re.match(r"(?:Parameters|Preconditions|Effects)\s*:", line, re.IGNORECASE)
        )
    else:
        candidates = normalized_lines

    for line in candidates:
        if not line or re.fullmatch(r"-+", line) or line.lower() == "action sequence:":
            continue
        if line not in seen_lines:
            seen_lines.add(line)
            keep.append(line)
    if not keep:
        raise CompressionError("problem subtask description not found")
    return keep, actions


def _compact_action_schema(name: str, block: str) -> str:
    parameters = _pddl_field(block, ":parameters") or "()"
    precondition = _pddl_field(block, ":precondition") or "()"
    effect = _pddl_field(block, ":effect") or "()"
    return f"{name}{parameters} pre={precondition} effect={effect}"


def _parse_last_key_object_states(content: str) -> List[Mapping[str, Any]]:
    matches = list(re.finditer(r"(?m)^\s*key_object_pddl_states\s*=\s*", content))
    if not matches:
        return []
    remainder = content[matches[-1].end() :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(remainder)
    except json.JSONDecodeError as exc:
        raise CompressionError(f"invalid key_object_pddl_states JSON: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise CompressionError("key_object_pddl_states must be a list of objects")
    return value


def _compact_state_entry(entry: Mapping[str, Any]) -> str:
    name = str(entry.get("object") or "")
    object_type = str(entry.get("object_type") or "object")
    facts = entry.get("facts", [])
    if not isinstance(facts, list):
        raise CompressionError("state facts must be a list")
    result = f"{name}:{object_type}[" + "|".join(str(fact) for fact in facts) + "]"

    related = entry.get("related_objects", []) or []
    if not isinstance(related, list) or not all(isinstance(item, Mapping) for item in related):
        raise CompressionError("related_objects must be a list of objects")
    if related:
        result += "{" + ";".join(_compact_state_entry(item) for item in related) + "}"
    return result


def compress_problem_generation_content(content: str) -> str:
    finish_match = re.search(r"Finish the tasks like example", content, flags=re.IGNORECASE)
    if not finish_match:
        raise CompressionError("problem example terminator not found")
    domain_marker = _last_marker(
        content,
        r"Domain file content\s*[:,]?",
        "actual domain",
    )
    if domain_marker.start() <= finish_match.end():
        raise CompressionError("actual domain marker precedes the problem subtask")

    domain = _extract_domain(content, domain_marker.end())
    action_pairs = _action_blocks(domain)
    action_by_lower = {name.lower(): (name, block) for name, block in action_pairs}
    subtask_text = content[finish_match.end() : domain_marker.start()].strip()
    summary_lines, referenced_actions = _problem_subtask_summary(
        subtask_text,
        [name for name, _ in action_pairs],
    )

    schemas = [
        _compact_action_schema(*action_by_lower[action.lower()])
        for action in referenced_actions
        if action.lower() in action_by_lower
    ]
    states = _parse_last_key_object_states(content)
    compact_states = ";".join(_compact_state_entry(entry) for entry in states) or "none"

    return "\n".join(
        [
            (
                "Generate one valid PDDL problem. Declare every object in States; copy "
                "applicable facts to :init; use only the action schema below. The domain and "
                "robot token must use the real name. Output only the PDDL problem."
            ),
            "Subtask:",
            *summary_lines,
            "Domain: " + _domain_name(domain),
            "Actions: " + ("; ".join(schemas) if schemas else "none"),
            "States: " + compact_states,
        ]
    )


COMPRESSORS = {
    "decompose": compress_decompose_content,
    "allocate": compress_allocate_content,
    "problem_generation": compress_problem_generation_content,
}


def count_tokens(tokenizer: Any, content: str) -> int:
    return len(tokenizer.encode(content, add_special_tokens=False).ids)


def _user_message_index(record: Mapping[str, Any]) -> int:
    messages = record.get("messages")
    if not isinstance(messages, list):
        raise CompressionError("messages must be a list")
    indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, Mapping) and message.get("role") == "user"
    ]
    if len(indexes) != 1:
        raise CompressionError(f"expected exactly one user message, found {len(indexes)}")
    content = messages[indexes[0]].get("content")
    if not isinstance(content, str):
        raise CompressionError("user content must be a string")
    return indexes[0]


def compress_record(
    record: Mapping[str, Any],
    stage: str,
    tokenizer: Any,
    max_tokens: int,
) -> Tuple[Dict[str, Any], int]:
    if not isinstance(record, Mapping):
        raise CompressionError("JSONL record must be an object")
    if stage not in COMPRESSORS:
        raise CompressionError(f"unknown compression stage: {stage}")
    if max_tokens <= 0:
        raise CompressionError("max_tokens must be positive")

    user_index = _user_message_index(record)
    compressed_record = copy.deepcopy(dict(record))
    original_content = compressed_record["messages"][user_index]["content"]
    compressed_content = COMPRESSORS[stage](original_content)
    token_count = count_tokens(tokenizer, compressed_content)
    if token_count > max_tokens:
        raise CompressionError(
            f"compressed user content has {token_count} tokens and exceeds {max_tokens} tokens",
            token_count=token_count,
        )
    compressed_record["messages"][user_index]["content"] = compressed_content
    return compressed_record, token_count


def rejected_path_for(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.rejected.jsonl")


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp")


def _validate_output(path: Path, tokenizer: Any, max_tokens: int, expected_count: int) -> int:
    count = 0
    maximum = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CompressionError(f"output JSON error on line {line_number}: {exc}") from exc
            user_index = _user_message_index(record)
            token_count = count_tokens(tokenizer, record["messages"][user_index]["content"])
            if token_count > max_tokens:
                raise CompressionError(
                    f"output line {line_number} has {token_count} tokens; limit is {max_tokens}",
                    token_count=token_count,
                )
            count += 1
            maximum = max(maximum, token_count)
    if count != expected_count:
        raise CompressionError(f"output count {count} does not match expected count {expected_count}")
    return maximum


def process_file(
    input_path: Path,
    output_path: Path,
    stage: str,
    tokenizer: Any,
    max_tokens: int,
    overwrite: bool = False,
) -> Dict[str, Any]:
    input_path = Path(input_path)
    output_path = Path(output_path)
    rejected_path = rejected_path_for(output_path)
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if not overwrite:
        for candidate in (output_path, rejected_path):
            if candidate.exists():
                raise FileExistsError(candidate)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_temp = _temporary_path(output_path)
    rejected_temp = _temporary_path(rejected_path)
    input_count = 0
    output_count = 0
    rejected_count = 0
    observed_max_tokens = 0

    try:
        with input_path.open("r", encoding="utf-8") as source, output_temp.open(
            "w", encoding="utf-8"
        ) as output, rejected_temp.open("w", encoding="utf-8") as rejected:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                input_count += 1
                try:
                    record = json.loads(line)
                    compressed, token_count = compress_record(
                        record,
                        stage=stage,
                        tokenizer=tokenizer,
                        max_tokens=max_tokens,
                    )
                except (json.JSONDecodeError, CompressionError, TypeError, ValueError) as exc:
                    rejected_count += 1
                    reason_prefix = (
                        "JSON decode error"
                        if isinstance(exc, json.JSONDecodeError)
                        else "compression error"
                    )
                    reject_record: Dict[str, Any] = {
                        "source": str(input_path),
                        "line_number": line_number,
                        "reason": f"{reason_prefix}: {exc}",
                    }
                    token_count = getattr(exc, "token_count", None)
                    if token_count is not None:
                        reject_record["token_count"] = token_count
                    rejected.write(json.dumps(reject_record, ensure_ascii=False) + "\n")
                    continue

                output.write(json.dumps(compressed, ensure_ascii=False) + "\n")
                output_count += 1
                observed_max_tokens = max(observed_max_tokens, token_count)

        validated_max_tokens = _validate_output(
            output_temp,
            tokenizer=tokenizer,
            max_tokens=max_tokens,
            expected_count=output_count,
        )
        if input_count != output_count + rejected_count:
            raise CompressionError(
                "input count does not equal output count plus rejected count"
            )
        os.replace(output_temp, output_path)
        os.replace(rejected_temp, rejected_path)
    except BaseException:
        for temporary in (output_temp, rejected_temp):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise

    return {
        "stage": stage,
        "input_path": input_path,
        "output_path": output_path,
        "rejected_path": rejected_path,
        "input_count": input_count,
        "output_count": output_count,
        "rejected_count": rejected_count,
        "max_tokens": max(observed_max_tokens, validated_max_tokens),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compress three SFT JSONL user prompts to a Qwen token budget."
    )
    parser.add_argument("--dataset-dir", default=str(DEFAULT_DATASET_DIR))
    parser.add_argument("--tokenizer-path", default=str(DEFAULT_TOKENIZER_PATH))
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing *_1280.jsonl and rejection reports.",
    )
    return parser.parse_args(argv)


def _output_path(input_path: Path, max_tokens: int) -> Path:
    return input_path.with_name(f"{input_path.stem}_{max_tokens}.jsonl")


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.max_tokens <= 0:
        raise ValueError("--max-tokens must be positive")

    dataset_dir = Path(args.dataset_dir).expanduser().resolve()
    tokenizer_path = Path(args.tokenizer_path).expanduser().resolve()
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    jobs = [
        (
            stage,
            dataset_dir / filename,
            _output_path(dataset_dir / filename, args.max_tokens),
        )
        for stage, filename in STAGE_INPUTS
    ]
    if not args.overwrite:
        for _, _, output_path in jobs:
            for candidate in (output_path, rejected_path_for(output_path)):
                if candidate.exists():
                    raise FileExistsError(candidate)

    summaries = []
    for stage, input_path, output_path in jobs:
        summary = process_file(
            input_path=input_path,
            output_path=output_path,
            stage=stage,
            tokenizer=tokenizer,
            max_tokens=args.max_tokens,
            overwrite=args.overwrite,
        )
        summaries.append(summary)
        print(
            json.dumps(
                {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in summary.items()
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )

    return 2 if any(summary["rejected_count"] for summary in summaries) else 0


def main() -> None:
    try:
        exit_code = run()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
