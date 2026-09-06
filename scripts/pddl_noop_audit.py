"""Deterministic auditing for zero-action PDDL plans.

The helpers in this module deliberately avoid planner, VAL, LLM, subprocess,
or filesystem calls.  Callers supply the exact problem, plan and scene-state
evidence used for one subtask.
"""

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Sequence

from pddl_problem_repair import (
    immediate_sexpr_spans,
    replace_span,
    strip_comments,
    tokenize_pddl,
)


@dataclass
class ProblemInitialStateAudit:
    problem: str
    goal_literals: List[str]
    init_literals: List[str]
    repairs: List[Dict[str, str]]
    failure_reasons: List[str]


def _parse_sexpr(text: str) -> Any:
    tokens = tokenize_pddl(text)
    if not tokens:
        raise ValueError("empty_expression")

    def parse_at(index: int) -> tuple[Any, int]:
        if index >= len(tokens):
            raise ValueError("unexpected_end")
        token = tokens[index]
        if token != "(":
            if token == ")":
                raise ValueError("unexpected_close")
            return token, index + 1
        values: List[Any] = []
        index += 1
        while index < len(tokens) and tokens[index] != ")":
            value, index = parse_at(index)
            values.append(value)
        if index >= len(tokens):
            raise ValueError("unbalanced_expression")
        return values, index + 1

    expression, next_index = parse_at(0)
    if next_index != len(tokens):
        raise ValueError("multiple_expressions")
    return expression


def _canonical_atom(expression: Any) -> Optional[str]:
    if not isinstance(expression, list) or not expression:
        return None
    if not isinstance(expression[0], str):
        return None
    predicate = expression[0].casefold()
    if predicate.startswith(("?", ":")) or predicate in {
        "and", "or", "not", "forall", "exists", "imply", "when"
    }:
        return None
    arguments: List[str] = []
    for argument in expression[1:]:
        if not isinstance(argument, str) or argument.startswith("?"):
            return None
        arguments.append(argument.casefold())
    suffix = f" {' '.join(arguments)}" if arguments else ""
    return f"({predicate}{suffix})"


def normalize_literal(expression: str) -> Optional[str]:
    """Return a canonical signed grounded literal, or ``None`` if unsupported."""
    try:
        parsed = _parse_sexpr(str(expression))
    except ValueError:
        return None
    if (
        isinstance(parsed, list)
        and len(parsed) == 2
        and isinstance(parsed[0], str)
        and parsed[0].casefold() == "not"
    ):
        inner = _canonical_atom(parsed[1])
        return f"(not {inner})" if inner else None
    return _canonical_atom(parsed)


def _mask_comments(problem: str) -> str:
    """Hide comments while retaining every source offset and line ending."""
    return re.sub(r";[^\r\n]*", lambda match: " " * len(match.group()), problem)


def _problem_sections(problem: str) -> tuple[Dict[str, tuple[int, int]], List[str]]:
    """Validate the full problem and locate only its immediate init/goal sections."""
    masked = _mask_comments(problem)
    try:
        root = _parse_sexpr(masked)
    except ValueError as exc:
        return {}, [f"malformed_problem:{exc}"]
    if not (
        isinstance(root, list)
        and len(root) >= 2
        and isinstance(root[0], str)
        and root[0].casefold() == "define"
        and isinstance(root[1], list)
        and len(root[1]) == 2
        and isinstance(root[1][0], str)
        and root[1][0].casefold() == "problem"
        and isinstance(root[1][1], str)
    ):
        return {}, ["malformed_problem:expected_problem_root"]
    if any(not isinstance(child, list) or not child for child in root[2:]):
        return {}, ["malformed_problem:expected_section"]

    root_start = masked.index("(") + 1
    root_end = masked.rfind(")")
    sections: Dict[str, List[tuple[int, int]]] = {"init": [], "goal": []}
    for start, end in immediate_sexpr_spans(masked[root_start:root_end]):
        start, end = root_start + start, root_start + end
        header = re.match(r"\(\s*:(init|goal)(?=\s|[()])", masked[start:end], re.IGNORECASE)
        if header:
            sections[header.group(1).casefold()].append((start, end))
    failures = []
    for name, spans in sections.items():
        if not spans:
            failures.append(f"missing_{name}_section")
        elif len(spans) != 1:
            failures.append(f"duplicate_{name}_section")
    return {name: spans[0] for name, spans in sections.items() if len(spans) == 1}, failures


def _goal_literals(problem: str, span: tuple[int, int]) -> tuple[List[str], List[str]]:
    section = _parse_sexpr(problem[span[0] : span[1]])
    if not isinstance(section, list) or len(section) != 2:
        return [], ["malformed_goal:expected_single_expression"]
    literals: List[str] = []
    failures: List[str] = []

    def visit(expression: Any) -> None:
        head = (
            str(expression[0]).casefold()
            if isinstance(expression, list) and expression
            else "expression"
        )
        if head == "and":
            if len(expression) == 1:
                failures.append("empty_goal")
            for child in expression[1:]:
                visit(child)
            return
        literal = normalize_literal(_render_sexpr(expression))
        if literal is None:
            failures.append(f"unsupported_goal:{head}")
        elif literal not in literals:
            literals.append(literal)

    visit(section[1])
    return ([], list(dict.fromkeys(failures))) if failures else (literals, [])


def _render_sexpr(expression: Any) -> str:
    if isinstance(expression, list):
        return "(" + " ".join(_render_sexpr(item) for item in expression) + ")"
    return str(expression)


def _evidence_by_literal(
    evidence: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        literal = normalize_literal(str(entry.get("literal", "")))
        if literal is None or literal in indexed:
            continue
        expected_polarity = "negative" if literal.startswith("(not ") else "positive"
        declared_polarity = entry.get("polarity")
        if (
            declared_polarity is not None
            and str(declared_polarity).casefold() != expected_polarity
        ):
            continue
        normalized = dict(entry)
        normalized["literal"] = literal
        normalized.setdefault("polarity", expected_polarity)
        indexed[literal] = normalized
    return indexed


def build_key_object_evidence(
    item: Dict[str, Any],
    object_token: str,
    parent_token: Optional[str],
    facts: Sequence[str],
    supported_predicates: Sequence[str],
) -> List[Dict[str, Any]]:
    """Build signed, provenance-bearing evidence from one AI2-THOR object."""
    supported = {str(predicate).casefold() for predicate in supported_predicates}
    source_by_predicate = {
        "at-location": "parentReceptacles",
        "object-open": "isOpen",
        "switch-on": "isToggled",
    }
    evidence: List[Dict[str, Any]] = []

    def add(literal: str, source_field: str, observed_value: Any, polarity: str) -> None:
        evidence.append(
            {
                "literal": literal,
                "polarity": polarity,
                "object": object_token,
                "object_id": item.get("objectId"),
                "source_field": source_field,
                "observed_value": observed_value,
            }
        )

    for fact in facts:
        literal = str(fact).strip()
        normalized = normalize_literal(literal)
        if normalized is None:
            continue
        predicate = normalized[1:].split(None, 1)[0].rstrip(")")
        source_field = source_by_predicate.get(predicate, "objectType")
        if source_field == "parentReceptacles":
            observed_value = parent_token
        elif source_field in item:
            observed_value = item.get(source_field)
        else:
            observed_value = item.get("objectType")
        add(literal, source_field, observed_value, "positive")

    for predicate, source_field in (
        ("object-open", "isOpen"),
        ("switch-on", "isToggled"),
    ):
        if (
            predicate in supported
            and source_field in item
            and item.get(source_field) is False
        ):
            add(
                f"(not ({predicate} {object_token}))",
                source_field,
                False,
                "negative",
            )
    return evidence


def _init_children(problem: str, span: tuple[int, int]) -> tuple[str, int, List[tuple[tuple[int, int], str]]]:
    section = problem[span[0] : span[1]]
    masked = _mask_comments(section)
    header = re.match(r"\(\s*:init\b", masked, flags=re.IGNORECASE)
    content_start = header.end()
    content = masked[content_start : len(section) - 1]
    children: List[tuple[tuple[int, int], str]] = []
    for child_span in immediate_sexpr_spans(content):
        expression = content[child_span[0] : child_span[1]]
        literal = normalize_literal(expression)
        if literal is not None:
            children.append((child_span, literal))
    return section, content_start, children


def audit_problem_initial_state(
    problem: str,
    evidence: Sequence[Dict[str, Any]],
) -> ProblemInitialStateAudit:
    source = str(problem)
    sections, failures = _problem_sections(source)
    if failures:
        return ProblemInitialStateAudit(source, [], [], [], failures)
    goals, failures = _goal_literals(source, sections["goal"])
    if failures:
        return ProblemInitialStateAudit(source, goals, [], [], failures)
    init_span = sections["init"]
    init_section, content_start, children = _init_children(source, init_span)

    evidence_index = _evidence_by_literal(evidence)
    goal_set = set(goals)
    remove_spans: List[tuple[int, int]] = []
    repairs: List[Dict[str, str]] = []
    for child_span, literal in children:
        if literal in goal_set and literal not in evidence_index:
            remove_spans.append(child_span)
            repairs.append(
                {
                    "type": "untrusted_goal_in_initial_state",
                    "literal": literal,
                    "before": "present_in_init",
                    "after": "removed_from_init",
                }
            )

    repaired = source
    if remove_spans:
        content_end = len(init_section) - 1
        content = init_section[content_start:content_end]
        parts: List[str] = []
        cursor = 0
        for start, end in remove_spans:
            parts.append(content[cursor:start])
            removed = content[start:end]
            if ";" in removed:
                # Erase syntax without losing comments or their whitespace/line endings.
                parts.append(re.sub(
                    r";[^\r\n]*|[^\s;]+",
                    lambda match: (
                        match.group() if match.group().startswith(";")
                        else " " * len(match.group())
                    ),
                    removed,
                ))
            cursor = end
        parts.append(content[cursor:])
        repaired_section = (
            init_section[:content_start] + "".join(parts) + init_section[content_end:]
        )
        repaired = replace_span(source, init_span, repaired_section)

    init_literals = list(dict.fromkeys(
        literal for child_span, literal in children if child_span not in remove_spans
    ))
    return ProblemInitialStateAudit(
        problem=repaired,
        goal_literals=goals,
        init_literals=init_literals,
        repairs=repairs,
        failure_reasons=failures,
    )


def _positive_literal(negative_literal: str) -> Optional[str]:
    try:
        parsed = _parse_sexpr(negative_literal)
    except ValueError:
        return None
    if (
        isinstance(parsed, list)
        and len(parsed) == 2
        and isinstance(parsed[0], str)
        and parsed[0].casefold() == "not"
    ):
        return _canonical_atom(parsed[1])
    return None


def plan_has_actions(plan_text: str) -> bool:
    for raw_line in strip_comments(str(plan_text)).splitlines():
        if re.match(r"^\s*(?:\d+(?:\.\d+)?\s*:\s*)?\(", raw_line):
            return True
    return False


def verify_zero_action_plan(
    *,
    subtask_id: int,
    problem: str,
    plan_text: str,
    evidence: Sequence[Dict[str, Any]],
    planner_record: Dict[str, Any],
    val_enabled: bool = False,
    val_record: Optional[Dict[str, Any]] = None,
    repairs: Optional[Sequence[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    audited = audit_problem_initial_state(problem, evidence)
    evidence_index = _evidence_by_literal(evidence)
    failures = list(audited.failure_reasons)

    raw_planner_status = planner_record.get("status")
    legacy_record_identified = bool(
        planner_record.get("problem_file")
        and planner_record.get("compatibility_output")
    )
    planner_status = str(
        raw_planner_status
        or ("legacy_return_code_0" if legacy_record_identified else "unknown")
    )
    plan_generated = planner_record.get("plan_generated") is True or (
        "plan_generated" not in planner_record and bool(str(plan_text).strip())
    )
    status_succeeded = (
        str(raw_planner_status).casefold() in {"completed", "ok", "success"}
        if raw_planner_status is not None
        else legacy_record_identified
    )
    planner_succeeded = (
        bool(planner_record)
        and status_succeeded
        and planner_record.get("return_code") == 0
        and planner_record.get("has_planner_error") is not True
        and plan_generated
    )
    if not planner_succeeded:
        failures.append("planner_not_successful")
    if plan_has_actions(plan_text):
        failures.append("plan_has_actions")
    if not re.search(
        r"^\s*;\s*cost\s*=\s*0(?:\s|\(|$)",
        str(plan_text),
        flags=re.IGNORECASE | re.MULTILINE,
    ):
        failures.append("missing_zero_cost_marker")

    if val_enabled:
        val_payload = val_record if isinstance(val_record, dict) else {}
        val_status = str(val_payload.get("status") or "missing")
        normalized_val_status = val_status.casefold()
        if "valid" in val_payload:
            val_valid = (
                val_payload.get("valid") is True
                and normalized_val_status
                not in {"invalid", "error", "failed", "infrastructure_error"}
            )
        else:
            val_valid = normalized_val_status == "valid"
        if not val_valid:
            failures.append("val_not_valid")
    else:
        val_status = "not_run"

    init_set = set(audited.init_literals)
    matched_evidence: List[Dict[str, Any]] = []
    for literal in audited.goal_literals:
        proof = evidence_index.get(literal)
        if proof is None:
            failures.append(f"missing_goal_evidence:{literal}")
        else:
            matched_evidence.append(proof)
        positive = _positive_literal(literal)
        if positive is None:
            if literal not in init_set:
                failures.append(f"goal_not_in_initial_state:{literal}")
        elif positive in init_set:
            failures.append(f"negative_goal_conflicts_with_initial_state:{literal}")

    combined_repairs: List[Dict[str, str]] = []
    for repair in [*(repairs or []), *audited.repairs]:
        if repair not in combined_repairs:
            combined_repairs.append(dict(repair))

    unique_failures: List[str] = []
    for failure in failures:
        if failure not in unique_failures:
            unique_failures.append(failure)

    return {
        "subtask_id": subtask_id,
        "goal_literals": list(audited.goal_literals),
        "evidence": matched_evidence,
        "repairs": combined_repairs,
        "planner_status": planner_status,
        "val_status": val_status,
        "verified": not unique_failures,
        "failure_reasons": unique_failures,
    }
