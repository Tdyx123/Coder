"""Pure, deterministic repairs for generated PDDL problem text.

This module deliberately performs no planner, VAL, LLM, subprocess, or network
calls.  It only inspects the generated problem and its already-loaded domain
text.
"""

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple


WORD_CHARS = "A-Za-z0-9_\\-"
LEAK_MARKERS = (
    "wait no",
    "let me",
    "thought process",
    "copy paste error",
    "correct line should",
    "original problem",
    "provided problem description",
    "validation of the provided",
    "here is",
)


@dataclass
class ProblemRepairResult:
    """Result of one local PDDL problem repair pass."""

    problem: str
    status: str
    changed: bool
    detected_categories: List[str]
    changes: List[str]
    unresolved: List[str]

    def to_manifest_record(self, subtask_index: int) -> Dict[str, Any]:
        """Return the stable, content-free representation stored in run artifacts."""
        return {
            "index": subtask_index,
            "status": self.status,
            "changed": self.changed,
            "detected_categories": list(self.detected_categories),
            "changes": list(self.changes),
            "unresolved": list(self.unresolved),
        }


def _append_unique(values: List[str], value: str) -> None:
    if value not in values:
        values.append(value)


def strip_comments(text: str) -> str:
    return re.sub(r";[^\n\r]*", "", text)


def tokenize_pddl(text: str) -> List[str]:
    return re.findall(r"\(|\)|-|[^\s()]+", strip_comments(text))


def find_balanced_end(text: str, start: int) -> Optional[int]:
    """Find an s-expression end while ignoring PDDL line comments."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == ";":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
        index += 1
    return None


def extract_problem_define_block(text: str) -> Tuple[Optional[str], str]:
    """Extract the first complete ``(define (problem ...))`` block."""
    clean = str(text).replace("\x00", "")
    match = re.search(r"\(\s*define\s*\(\s*problem\b", clean, flags=re.IGNORECASE)
    if not match:
        return None, "no_problem_define_block"
    end = find_balanced_end(clean, match.start())
    if end is None:
        return None, "unbalanced_problem_define_block"
    return clean[match.start() : end].strip() + "\n", "extracted_problem_define_block"


def find_section_span(pddl: str, section_name: str) -> Optional[Tuple[int, int]]:
    pattern = re.compile(r"\(\s*:" + re.escape(section_name) + r"\b", flags=re.IGNORECASE)
    match = pattern.search(pddl)
    if not match:
        return None
    end = find_balanced_end(pddl, match.start())
    if end is None:
        return None
    return match.start(), end


def replace_span(text: str, span: Tuple[int, int], replacement: str) -> str:
    return text[: span[0]] + replacement + text[span[1] :]


def parse_typed_list(section_text: str, section_name: str) -> List[Tuple[List[str], str]]:
    tokens = tokenize_pddl(section_text)
    body: List[str] = []
    found_section = False
    for token in tokens:
        if token in {"(", ")"}:
            continue
        if not found_section and token.lower() == f":{section_name.lower()}":
            found_section = True
            continue
        if found_section:
            body.append(token)

    groups: List[Tuple[List[str], str]] = []
    names: List[str] = []
    index = 0
    while index < len(body):
        token = body[index]
        if token == "-":
            if index + 1 < len(body) and names:
                groups.append((names, body[index + 1]))
                names = []
                index += 2
                continue
            index += 1
            continue
        names.append(token)
        index += 1
    if names:
        groups.append((names, "object"))
    return groups


def render_objects_section(groups: Sequence[Tuple[List[str], str]]) -> str:
    lines = ["  (:objects"]
    for names, type_name in groups:
        if names:
            lines.append(f"    {' '.join(names)} - {type_name}")
    lines.append("  )")
    return "\n".join(lines)


def replace_symbol(text: str, old: str, new: str) -> str:
    pattern = re.compile(
        rf"(?<![{WORD_CHARS}]){re.escape(old)}(?![{WORD_CHARS}])",
        flags=re.IGNORECASE,
    )
    return pattern.sub(new, text)


def domain_types(domain_text: str) -> List[str]:
    span = find_section_span(domain_text, "types")
    if not span:
        return ["object"]
    groups = parse_typed_list(domain_text[span[0] : span[1]], "types")
    found: List[str] = []
    seen = set()
    for names, parent in groups:
        for name in [*names, parent]:
            key = name.lower()
            if key not in seen:
                found.append(name)
                seen.add(key)
    if "object" not in seen:
        found.append("object")
    return found


def normal_type_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def canonical_type(type_name: str, known_types: Sequence[str]) -> Tuple[str, bool]:
    by_lower = {item.lower(): item for item in known_types}
    by_normal = {normal_type_key(item): item for item in known_types}
    if type_name.lower() in by_lower:
        return type_name, False
    normalized = normal_type_key(type_name)
    if normalized in by_normal:
        return by_normal[normalized], True
    return by_lower.get("object", "object"), True


def repair_duplicate_objects(pddl: str) -> Tuple[str, List[str], bool]:
    span = find_section_span(pddl, "objects")
    if not span:
        return pddl, [], False
    groups = parse_typed_list(pddl[span[0] : span[1]], "objects")
    seen: Dict[str, str] = {}
    aliases: List[Tuple[str, str]] = []
    new_groups: List[Tuple[List[str], str]] = []

    for names, type_name in groups:
        kept_names: List[str] = []
        for name in names:
            key = name.lower()
            if key in seen:
                aliases.append((name, seen[key]))
            else:
                seen[key] = name
                kept_names.append(name)
        if kept_names:
            new_groups.append((kept_names, type_name))

    if not aliases:
        return pddl, [], False

    repaired = replace_span(pddl, span, render_objects_section(new_groups))
    for old, new in aliases:
        if old != new:
            repaired = replace_symbol(repaired, old, new)
    changes = [f"merged_object:{old}->{new}" for old, new in aliases]
    return repaired, changes, True


def repair_unknown_object_types(pddl: str, domain_content: str) -> Tuple[str, List[str], bool]:
    span = find_section_span(pddl, "objects")
    if not span:
        return pddl, [], False
    known_types = domain_types(domain_content)
    groups = parse_typed_list(pddl[span[0] : span[1]], "objects")
    changes: List[str] = []
    repaired_groups: List[Tuple[List[str], str]] = []
    for names, type_name in groups:
        fixed_type, changed = canonical_type(type_name, known_types)
        if changed:
            changes.append(f"type:{type_name}->{fixed_type}")
        repaired_groups.append((names, fixed_type))
    if not changes:
        return pddl, [], False
    repaired = replace_span(pddl, span, render_objects_section(repaired_groups))
    return repaired, changes, True


def immediate_sexpr_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == ";":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline + 1
            continue
        if char != "(":
            index += 1
            continue
        end = find_balanced_end(text, index)
        if end is None:
            break
        spans.append((index, end))
        index = end
    return spans


def atom_key(expr: str) -> str:
    tokens = [token.lower() for token in tokenize_pddl(expr) if token not in {"(", ")"}]
    return " ".join(tokens)


def negative_inner(expr: str) -> Optional[str]:
    stripped = strip_comments(expr).strip()
    match = re.match(r"^\(\s*not\b", stripped, flags=re.IGNORECASE)
    if not match:
        return None
    start = stripped.find("(", match.end())
    if start == -1:
        return None
    end = find_balanced_end(stripped, start)
    if end is None:
        return None
    return stripped[start:end]


def repair_initial_state_contradictions(pddl: str) -> Tuple[str, List[str], bool]:
    span = find_section_span(pddl, "init")
    if not span:
        return pddl, [], False
    section = pddl[span[0] : span[1]]
    header = re.search(r"\(\s*:init\b", section, flags=re.IGNORECASE)
    if not header:
        return pddl, [], False
    content_start = header.end()
    content_end = len(section) - 1
    content = section[content_start:content_end]

    positives = set()
    negatives: List[Tuple[Tuple[int, int], str, str]] = []
    for child_span in immediate_sexpr_spans(content):
        expr = content[child_span[0] : child_span[1]]
        inner = negative_inner(expr)
        if inner is None:
            positives.add(atom_key(expr))
        else:
            negatives.append((child_span, atom_key(inner), inner))

    remove_spans: List[Tuple[int, int]] = []
    changes: List[str] = []
    for child_span, key, inner in negatives:
        if key in positives:
            remove_spans.append(child_span)
            changes.append(f"removed_conflicting_negative:{' '.join(tokenize_pddl(inner))}")
    if not remove_spans:
        return pddl, [], False

    repaired_parts: List[str] = []
    cursor = 0
    for start, end in remove_spans:
        repaired_parts.append(content[cursor:start])
        cursor = end
    repaired_parts.append(content[cursor:])
    repaired_section = section[:content_start] + "".join(repaired_parts) + section[content_end:]
    return replace_span(pddl, span, repaired_section), changes, True


def _outside_define_text(text: str, define_block: str) -> str:
    stripped_block = define_block.strip()
    start = text.find(stripped_block)
    if start == -1:
        return ""
    return text[:start] + text[start + len(stripped_block) :]


def repair_problem_pddl(
    problem: str,
    domain_content: str,
    raw_output: Optional[str] = None,
) -> ProblemRepairResult:
    """Inspect and repair one generated problem with deterministic local rules."""
    original = str(problem)
    repaired = original
    detected: List[str] = []
    changes: List[str] = []
    unresolved: List[str] = []

    extracted, extraction_reason = extract_problem_define_block(repaired)
    if extracted is None:
        _append_unique(detected, "malformed_pddl_syntax")
        unresolved.append(extraction_reason)
        return ProblemRepairResult(
            problem=original,
            status="unresolved",
            changed=False,
            detected_categories=detected,
            changes=changes,
            unresolved=unresolved,
        )

    outside = _outside_define_text(repaired, extracted)
    if outside.strip():
        if "```" in outside:
            _append_unique(detected, "markdown_fence_in_pddl")
        else:
            _append_unique(detected, "natural_language_leak")
        changes.append("removed_markdown_or_surrounding_text")
        repaired = extracted

    if raw_output is not None:
        raw_extracted, _ = extract_problem_define_block(raw_output)
        if raw_extracted is not None:
            raw_outside = _outside_define_text(str(raw_output), raw_extracted)
            if raw_outside.strip():
                if "```" in raw_outside:
                    _append_unique(detected, "markdown_fence_in_pddl")
                else:
                    _append_unique(detected, "natural_language_leak")
                _append_unique(changes, "removed_markdown_or_surrounding_text")

    repaired, duplicate_changes, duplicate_found = repair_duplicate_objects(repaired)
    if duplicate_found:
        _append_unique(detected, "duplicate_object_definition")
        changes.extend(duplicate_changes)

    repaired, type_changes, unknown_type_found = repair_unknown_object_types(repaired, domain_content)
    if unknown_type_found:
        _append_unique(detected, "pddl_unknown_type")
        changes.extend(type_changes)

    repaired, contradiction_changes, contradiction_found = repair_initial_state_contradictions(repaired)
    if contradiction_found:
        _append_unique(detected, "initial_state_contradiction")
        changes.extend(contradiction_changes)

    lowered_define = repaired.lower()
    if "```" in repaired:
        _append_unique(detected, "markdown_fence_in_pddl")
        unresolved.append("define_block_contains_markdown_fence")
    if any(marker in lowered_define for marker in LEAK_MARKERS):
        _append_unique(detected, "natural_language_leak")
        unresolved.append("define_block_contains_natural_language")

    changed = repaired != original
    status = "unresolved" if unresolved else ("repaired" if changes else "unchanged")
    return ProblemRepairResult(
        problem=repaired,
        status=status,
        changed=changed,
        detected_categories=detected,
        changes=changes,
        unresolved=unresolved,
    )
