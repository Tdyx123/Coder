"""Offline structural checks and feedback for one decomposition repair attempt."""

from collections import Counter
import re
from typing import Any, Dict, List, Optional

from parsing_utils import ParsingUtils


RETRY_PROMPT = """Your previous task decomposition failed validation.
Generate one complete replacement decomposition that resolves ALL
reported errors.

The original task, supplied PDDL domain, and object information remain
the source of truth. The previous response is a draft to repair.

VALIDATION ERRORS:
{validation_errors}

REQUIRED SUBTASKS:
{required_subtasks}

REGENERATION REQUIREMENTS:

1. Return the ENTIRE decomposition, including previously valid subtasks.
   Do not return only corrections, missing sections, or a continuation.

2. Include a concise overview followed by the complete action body for
   every subtask. Preserve the supplied subtask IDs and goals.
   Every overview ID must have exactly one corresponding body section.
   Do not remove a subtask merely to resolve a validation error.

3. Use this exact heading format for each body section:
   #SubTask <ID>: <subtask title>

   In the overview, use bullet entries:
   - SubTask <ID>: <subtask title>

4. Every subtask must contain at least one action.
   Every action must include these three explicitly labeled fields:
   Parameters:
   Preconditions:
   Effects:

   Each field must contain its complete value.
   A value may continue on subsequent lines.
   "Preconditions: None." is allowed when no precondition is required.

5. Use action names from the supplied PDDL domain.
   Complete every parameter list and predicate expression.
   Close all parentheses. Do not use placeholders, ellipses,
   "same as above", or "remaining actions omitted".

6. Repair all reported defects while preserving valid task content.
   Do not add redundant actions or filler to increase the output length.

7. Spend the output budget on the decomposition itself.
   Omit introductory explanations, robot-assignment analysis,
   repeated domain definitions, and closing commentary.
   Keep action descriptions brief without omitting required fields.

8. Before answering, check that:
   - every required subtask is present;
   - overview IDs and body IDs agree;
   - every action has all three complete fields;
   - the final action is complete.

Return only the overview and complete subtask bodies.
"""

# This scanner deliberately recognizes more headings than the execution parser.
# Summary headings and executable body headings must be classified separately.
HEADER_RE = re.compile(
    r"^[ \t]*(?P<hash>#{1,6}[ \t]*)?(?P<bullet>[-*][ \t]+)?"
    r"(?:\*\*)?Sub[ \t]*Task[ \t]*#?[ \t]*(?P<id>\d+)"
    r"(?P<separator>[ \t]*[:.]|[ \t]+)[ \t]*(?P<title>.*)$",
    re.I | re.M,
)
FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?"
    r"(?P<name>Parameters|Preconditions|Effects)(?:\*\*)?[ \t]*:"
    r"(?:\*\*)?[ \t]*(?P<value>[^\n]*)$", re.I | re.M,
)
ACTION_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+|\d+[.)][ \t]*)?(?:\*\*)?"
    r"(?:Action[ \t]+\d+[ \t]*:[ \t]*)?"
    r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:\*\*)?(?:[ \t]*:[^\n]*|[ \t]*)$",
    re.M,
)
BODY_MARKER_RE = re.compile(r"action descriptions? from domain", re.I)
PLACEHOLDER_RE = re.compile(r"\.\.\.|…|\b(?:TODO|TBD|placeholder|omitted)\b|same as above", re.I)
FIELD_NAMES = ("Parameters", "Preconditions", "Effects")


def _field_values(text: str) -> Dict[str, str]:
    matches = list(FIELD_RE.finditer(text))
    values = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lines = text[match.start("value"):end].splitlines()
        # Comments/closing prose are not a substitute for a field value.
        lines = [line.split(";", 1)[0].strip() for line in lines
                 if not re.match(r"^\s*(?:#|```|---|\*\*Task\b)", line)]
        values.setdefault(match.group("name").capitalize(), "\n".join(lines).strip(" \n*`"))
    return values


def _actions(text: str, known_actions: set) -> List[Dict[str, Any]]:
    candidates = [match for match in ACTION_RE.finditer(text)
                  if match.group("name").lower() not in {
                      "parameters", "preconditions", "effects", "note", "notes",
                      "actions", "action", "description", "goal", "overview",
                      "robots", "objects", "skills",
                  }
                  # Bare multiline values such as None are not action boundaries.
                  and (match.group("name").lower() in known_actions
                       or ":" in text[match.end("name"):match.end()]
                       or re.search(r"\d+[.)]|Action\s+\d+\s*:",
                                    text[match.start():match.start("name")], re.I))]
    actions = []
    for index, match in enumerate(candidates):
        end = candidates[index + 1].start() if index + 1 < len(candidates) else len(text)
        fields = _field_values(text[match.end():end])
        if fields or match.group("name").lower() in known_actions:
            counts = Counter(field.group("name").capitalize() for field in FIELD_RE.finditer(text[match.end():end]))
            actions.append({"name": match.group("name"), "fields": fields,
                            "duplicate_fields": [name for name, count in counts.items() if count > 1]})
    return actions


def _incomplete(value: str) -> bool:
    if PLACEHOLDER_RE.search(value):
        return True
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0


def _sections(text: str, known_actions: set) -> List[Dict[str, Any]]:
    headers = [match for match in HEADER_RE.finditer(text)
               if not re.match(r"(?:must|can|should|depends|is|and)\b", match.group("title"), re.I)]
    sections = []
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[match.end():end]
        sections.append({"id": int(match.group("id")),
                         "title": match.group("title").strip().rstrip("* "),
                         "start": match.start(), "header_end": match.end(), "end": end,
                         "bullet": bool(match.group("bullet")), "body": body,
                         "actions": _actions(body, known_actions)})
    return sections


def validate_decomposition(
    text: str, *, domain_content: str = "", finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None, max_completion_tokens: Optional[int] = None,
    required_subtasks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return generation defects separately from loss in the execution parser."""
    known = {name.lower() for name in re.findall(r"\(:action\s+([^\s()]+)", domain_content, re.I)}
    sections = _sections(text, known)
    marker = BODY_MARKER_RE.search(text)
    first_body = 0
    if marker:
        first_body = next((i for i, section in enumerate(sections) if section["start"] > marker.end()), len(sections))
    else:
        seen = set()
        for index, section in enumerate(sections):
            if section["actions"]:
                break
            if section["id"] in seen:
                break
            seen.add(section["id"])
            if section["bullet"] or "skills required" in section["title"].lower():
                first_body = index + 1
        # Hash/bold overview headings also repeat their IDs in the body.
        if not first_body:
            seen = set()
            for index, section in enumerate(sections):
                if section["id"] in seen:
                    first_body = index
                    break
                if section["actions"]:
                    break
                seen.add(section["id"])
    overview, bodies = sections[:first_body], sections[first_body:]
    counts = Counter(section["id"] for section in bodies)
    inventory = {}
    for section in (required_subtasks or []) + overview + bodies:
        title = re.sub(r"\s*\(Skills Required:.*", "", section["title"], flags=re.I).strip()
        sid = section["id"]
        if section in bodies and counts[sid] > 1:
            sid = None  # Preserve both goals without prescribing duplicate IDs.
        key = (sid, title) if sid is None else sid
        inventory.setdefault(key, {"id": sid, "title": title})
    errors = []

    def error(code, message, **details):
        errors.append({"code": code, "message": f"{code}: {message}", **details})

    if not text.strip():
        error("EMPTY_OUTPUT", "The previous response contained no usable decomposition. Return an actual overview and complete action bodies.")
    elif not sections:
        code = "PARSE_FAILED" if _actions(text, known) else "EMPTY_OUTPUT"
        error(code, "No identifiable subtask sections were found in the response.")
    for section in inventory.values():
        subtask_id = section["id"]
        if subtask_id is None:
            goal_key = lambda title: re.sub(r"[\W_]+", "", title).lower()
            if not any(goal_key(body["title"]) == goal_key(section["title"]) for body in bodies):
                error("MISSING_SUBTASK_BODY", f'Required goal ("{section["title"]}") from duplicate-ID sections is missing. Restore this goal using its original title and a unique body ID.')
        elif subtask_id not in counts:
            error("MISSING_SUBTASK_BODY", f'SubTask {subtask_id} ("{section["title"]}") is required, but its corresponding action body is missing. Restore this body and retain the other required subtasks.', subtask_id=subtask_id)
    overview_ids = {section["id"] for section in overview}
    if overview:
        for section in bodies:
            if section["id"] not in overview_ids:
                error("OVERVIEW_BODY_MISMATCH", f'SubTask {section["id"]} ("{section["title"]}") has an action body but no overview entry. Make the overview and body IDs agree.', subtask_id=section["id"])
    for subtask_id, count in counts.items():
        if count > 1:
            titles = [section["title"] for section in bodies if section["id"] == subtask_id]
            error("DUPLICATE_BODY_ID", f"SubTask {subtask_id} is used by multiple body sections: {titles}. Make body IDs unique and consistent with the overview while preserving all required goals.", subtask_id=subtask_id)
    for section in bodies:
        sid = section["id"]
        if not section["actions"]:
            error("NO_ACTIONS", f'SubTask {sid} ("{section["title"]}") contains no action body. Supply the actions needed to accomplish this subtask, each with Parameters, Preconditions, and Effects.', subtask_id=sid)
        for index, action in enumerate(section["actions"], 1):
            location = f'SubTask {sid}, action occurrence {index} ("{action["name"]}")'
            missing = [field for field in FIELD_NAMES if not action["fields"].get(field)]
            if missing:
                error("ACTION_FIELDS", f"{location} has missing or empty fields: {', '.join(missing)}. Rewrite this action with all three complete fields.", subtask_id=sid, action_index=index, fields=missing)
            if action["duplicate_fields"]:
                error("ACTION_FIELDS", f"{location} contains repeated fields: {', '.join(action['duplicate_fields'])}. Separate each action with its own action heading and exactly one set of complete fields.", subtask_id=sid, action_index=index)
            for field, value in action["fields"].items():
                if _incomplete(value):
                    error("INCOMPLETE_EXPRESSION", f"{location}, field {field}, contains an incomplete expression: {value[:240]}. Replace it with a complete expression; do not merely append closing characters.", subtask_id=sid, action_index=index, field=field)

    tokens = (usage or {}).get("completion_tokens")
    at_cap = isinstance(tokens, (int, float)) and isinstance(max_completion_tokens, (int, float)) and tokens >= max_completion_tokens
    tail_bad = False
    if bodies:
        last = bodies[-1]
        tail_bad = not last["actions"] or any(
            not last["actions"][-1]["fields"].get(field) or _incomplete(last["actions"][-1]["fields"].get(field, ""))
            for field in FIELD_NAMES
        )
    coverage_bad = any(e["code"] == "MISSING_SUBTASK_BODY" for e in errors)
    truncated = finish_reason == "length" or (at_cap and (tail_bad or coverage_bad))
    if truncated:
        evidence = "finish_reason=length" if finish_reason == "length" else f"completion_tokens={tokens} reaches max_completion_tokens={max_completion_tokens}, and action bodies are incomplete"
        error("TRUNCATED_OUTPUT", f"{evidence}. Regenerate the complete decomposition from the beginning. Shorten explanations, but preserve every required subtask and action field.")

    # Normalize only identified headings. Never manufacture action content.
    normalized = text
    for index in range(len(sections) - 1, -1, -1):
        section = sections[index]
        prefix = "- SubTask" if index < first_body else "#SubTask"
        heading = f'{prefix} {section["id"]}: {section["title"]}'
        normalized = normalized[:section["start"]] + heading + normalized[section["header_end"]:]
    normalized = normalized.strip()
    parsed = []
    if not errors:
        # Allocation/problem generation use 1-based list positions as IDs.
        # Sorting known IDs is a local representation fix, not a task-order rule.
        ordered_bodies = sorted(bodies, key=lambda section: section["id"])
        expected = [(s["id"], s["actions"]) for s in ordered_bodies]
        if [s["id"] for s in ordered_bodies] != list(range(1, len(bodies) + 1)):
            error("PARSE_FAILED", "Body IDs cannot be mapped losslessly to the execution layer's 1-based subtask positions.")
        if bodies != ordered_bodies:
            normalized_bodies = _sections(normalized, known)[first_body:]
            prefix = normalized[:normalized_bodies[0]["start"]]
            chunks = [normalized[s["start"]:s["end"]].strip()
                      for s in sorted(normalized_bodies, key=lambda section: section["id"])]
            normalized = prefix + "\n\n".join(chunks)

        def signature(parts):
            result = []
            for part in parts:
                found = _sections(part, known)
                if len(found) != 1:
                    return None
                result.append((found[0]["id"], found[0]["actions"]))
            return result

        parsed = ParsingUtils.extract_subtasks(text)
        if signature(parsed) != expected or normalized != text:
            parsed = ParsingUtils.extract_subtasks(normalized)
        if not bodies or signature(parsed) != expected:
            error("PARSE_FAILED", "Complete body sections were not preserved by the execution parser after local heading normalization.")
    status = "parse_failed" if any(e["code"] == "PARSE_FAILED" for e in errors) else "invalid" if errors else "passed"
    return {"status": status, "errors": errors, "truncated": truncated,
            "required_subtasks": list(inventory.values()), "normalized_text": normalized,
            "subtasks": parsed}


def build_retry_prompt(validation: Dict[str, Any]) -> str:
    inventory = validation["required_subtasks"]
    required = "\n".join(
        f'- SubTask {item["id"]}: {item["title"]}' if item["id"] is not None else
        f'- Required goal (ambiguous previous ID): {item["title"]}. Preserve this title and assign a unique ID.'
        for item in inventory
    )
    if not required:
        required = ("No reliable subtask inventory was recovered.\n"
                    "Derive the subtasks from the original task and make the overview and body consistent.")
    return RETRY_PROMPT.format(
        validation_errors="\n".join("- " + error["message"] for error in validation["errors"]),
        required_subtasks=required,
    )
