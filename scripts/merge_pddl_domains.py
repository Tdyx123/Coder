#!/usr/bin/env python3
"""Merge a base PDDL domain with one or more device domains."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union


PddlExpr = Union[str, List["PddlExpr"]]
PathLike = Union[str, Path]


class PddlDomainMergeError(ValueError):
    """Raised when PDDL domains cannot be merged safely."""


@dataclass
class PddlTypeDecl:
    name: str
    parent: Optional[str]


@dataclass
class ParsedPddlDomain:
    path: Path
    name: str
    requirements: List[str] = field(default_factory=list)
    types: List[PddlTypeDecl] = field(default_factory=list)
    predicates: List[List[PddlExpr]] = field(default_factory=list)
    actions: List[List[PddlExpr]] = field(default_factory=list)


@dataclass
class _NamedItem:
    name: str
    expr: PddlExpr
    source: Path


class PddlDomainMergeHelper:
    """Merge PDDL domain files and return the merged domain text."""

    def __init__(self, repo_root: Optional[PathLike] = None, domain_dir: Optional[PathLike] = None):
        self.repo_root = Path(repo_root).resolve() if repo_root else Path(__file__).resolve().parents[1]
        self.domain_dir = Path(domain_dir).resolve() if domain_dir else self.repo_root / "resources" / "v2"

    def merge_domains(
        self,
        base_path: PathLike,
        domain_paths: Sequence[PathLike],
        domain_name: str = "allactionrobot",
        include_inaction: bool = True,
    ) -> str:
        """Return a merged PDDL domain as text.

        ``base_path`` and each item in ``domain_paths`` may be absolute paths,
        paths relative to the repository root, or bare file names inside
        ``resources/v2``.
        """
        parsed_domains = [self._parse_domain(self.resolve_domain_path(base_path))]
        parsed_domains.extend(self._parse_domain(self.resolve_domain_path(path)) for path in domain_paths)

        requirements: List[str] = []
        requirement_keys: set[str] = set()
        types: List[PddlTypeDecl] = []
        type_map: Dict[str, Tuple[PddlTypeDecl, Path]] = {}
        predicates: List[List[PddlExpr]] = []
        predicate_map: Dict[str, _NamedItem] = {}
        actions: List[List[PddlExpr]] = []
        action_map: Dict[str, _NamedItem] = {}

        for domain in parsed_domains:
            for requirement in domain.requirements:
                key = self._atom_key(requirement)
                if key not in requirement_keys:
                    requirement_keys.add(key)
                    requirements.append(requirement)

            for type_decl in domain.types:
                self._merge_type(type_decl, domain.path, types, type_map)

            for predicate in domain.predicates:
                self._merge_named_expr(
                    item_kind="predicate",
                    expr=predicate,
                    source=domain.path,
                    output=predicates,
                    seen=predicate_map,
                )

            for action in domain.actions:
                self._merge_named_expr(
                    item_kind="action",
                    expr=action,
                    source=domain.path,
                    output=actions,
                    seen=action_map,
                )

        if include_inaction:
            self._merge_named_expr(
                item_kind="predicate",
                expr=["inaction", "?robot", "-", "robot"],
                source=Path("<generated:inaction>"),
                output=predicates,
                seen=predicate_map,
            )

        return self._render_domain(domain_name, requirements, types, predicates, actions)

    def resolve_domain_path(self, path_value: PathLike) -> Path:
        """Resolve a path according to the repository's PDDL domain conventions."""
        path = Path(path_value)
        if path.is_absolute():
            return path
        if len(path.parts) == 1:
            return self.domain_dir / path
        return self.repo_root / path

    def _merge_type(
        self,
        type_decl: PddlTypeDecl,
        source: Path,
        output: List[PddlTypeDecl],
        seen: Dict[str, Tuple[PddlTypeDecl, Path]],
    ) -> None:
        key = self._atom_key(type_decl.name)
        existing = seen.get(key)
        if existing is None:
            seen[key] = (type_decl, source)
            output.append(type_decl)
            return

        existing_decl, existing_source = existing
        if self._optional_atom_key(existing_decl.parent) != self._optional_atom_key(type_decl.parent):
            raise PddlDomainMergeError(
                f"Conflicting type '{type_decl.name}' in {source}: "
                f"already defined in {existing_source}"
            )

    def _merge_named_expr(
        self,
        item_kind: str,
        expr: List[PddlExpr],
        source: Path,
        output: List[List[PddlExpr]],
        seen: Dict[str, _NamedItem],
    ) -> None:
        name = self._named_expr_name(item_kind, expr, source)
        key = self._atom_key(name)
        canonical = self._canonical_expr(expr)
        existing = seen.get(key)
        if existing is None:
            seen[key] = _NamedItem(name=name, expr=expr, source=source)
            output.append(expr)
            return

        if self._canonical_expr(existing.expr) != canonical:
            raise PddlDomainMergeError(
                f"Conflicting {item_kind} '{name}' in {source}: "
                f"already defined in {existing.source}"
            )

    def _named_expr_name(self, item_kind: str, expr: List[PddlExpr], source: Path) -> str:
        if item_kind == "action":
            if len(expr) < 2 or not isinstance(expr[1], str):
                raise PddlDomainMergeError(f"Invalid action form in {source}: {self._format_inline(expr)}")
            return expr[1]

        if item_kind == "predicate":
            if not expr or not isinstance(expr[0], str):
                raise PddlDomainMergeError(f"Invalid predicate form in {source}: {self._format_inline(expr)}")
            return expr[0]

        raise PddlDomainMergeError(f"Unknown merge item kind: {item_kind}")

    def _parse_domain(self, path: Path) -> ParsedPddlDomain:
        if not path.is_file():
            raise PddlDomainMergeError(f"PDDL domain file does not exist: {path}")

        text = path.read_text(encoding="utf-8")
        expr = self._parse_pddl_text(text, path)
        if not isinstance(expr, list) or len(expr) < 2 or self._atom_key(expr[0]) != "define":
            raise PddlDomainMergeError(f"Expected top-level (define ...) form in {path}")

        domain_decl = expr[1]
        if (
            not isinstance(domain_decl, list)
            or len(domain_decl) != 2
            or not isinstance(domain_decl[0], str)
            or self._atom_key(domain_decl[0]) != "domain"
            or not isinstance(domain_decl[1], str)
        ):
            raise PddlDomainMergeError(f"Expected (domain <name>) declaration in {path}")

        parsed = ParsedPddlDomain(path=path, name=domain_decl[1])
        raw_types = self._parse_types_from_text(text, path)
        for section in expr[2:]:
            if not isinstance(section, list) or not section or not isinstance(section[0], str):
                raise PddlDomainMergeError(f"Invalid domain section in {path}: {self._format_inline(section)}")
            tag = self._atom_key(section[0])
            if tag == ":requirements":
                parsed.requirements.extend(self._expect_atoms(section[1:], path, ":requirements"))
            elif tag == ":types":
                parsed.types.extend(raw_types if raw_types is not None else self._parse_types(section[1:], path))
            elif tag == ":predicates":
                parsed.predicates.extend(self._parse_predicates(section[1:], path))
            elif tag == ":action":
                parsed.actions.append(section)
            else:
                raise PddlDomainMergeError(f"Unsupported domain section '{section[0]}' in {path}")

        return parsed

    def _parse_pddl_text(self, text: str, path: Path) -> PddlExpr:
        tokens = self._tokenize(text)
        if not tokens:
            raise PddlDomainMergeError(f"Empty PDDL file: {path}")
        expr, next_index = self._parse_tokens(tokens, 0, path)
        if next_index != len(tokens):
            raise PddlDomainMergeError(f"Unexpected tokens after top-level form in {path}")
        return expr

    def _strip_comments(self, text: str) -> str:
        return "\n".join(line.split(";", 1)[0] for line in text.splitlines())

    def _tokenize(self, text: str) -> List[str]:
        without_comments = self._strip_comments(text)
        tokens: List[str] = []
        current: List[str] = []

        def flush_current() -> None:
            if current:
                tokens.append("".join(current))
                current.clear()

        for char in without_comments:
            if char in "()":
                flush_current()
                tokens.append(char)
            elif char.isspace():
                flush_current()
            else:
                current.append(char)
        flush_current()
        return tokens

    def _parse_tokens(self, tokens: Sequence[str], index: int, path: Path) -> Tuple[PddlExpr, int]:
        if index >= len(tokens):
            raise PddlDomainMergeError(f"Unexpected end of file while parsing {path}")

        token = tokens[index]
        if token == "(":
            items: List[PddlExpr] = []
            index += 1
            while index < len(tokens) and tokens[index] != ")":
                item, index = self._parse_tokens(tokens, index, path)
                items.append(item)
            if index >= len(tokens):
                raise PddlDomainMergeError(f"Unclosed '(' while parsing {path}")
            return items, index + 1

        if token == ")":
            raise PddlDomainMergeError(f"Unexpected ')' while parsing {path}")

        return token, index + 1

    def _parse_types_from_text(self, text: str, path: Path) -> Optional[List[PddlTypeDecl]]:
        section_text = self._extract_section_text(self._strip_comments(text), ":types")
        if section_text is None:
            return None

        match = re.match(r"\(\s*:types\b", section_text, flags=re.IGNORECASE)
        if not match:
            raise PddlDomainMergeError(f"Invalid :types section in {path}")

        declarations: List[PddlTypeDecl] = []
        inner = section_text[match.end():-1]
        for line in inner.splitlines():
            atoms = self._tokenize_type_line(line)
            if atoms:
                declarations.extend(self._parse_type_atoms(atoms, path))
        return declarations

    def _extract_section_text(self, text: str, section_name: str) -> Optional[str]:
        pattern = re.compile(r"\(\s*" + re.escape(section_name) + r"\b", flags=re.IGNORECASE)
        match = pattern.search(text)
        if not match:
            return None

        balance = 0
        for index in range(match.start(), len(text)):
            char = text[index]
            if char == "(":
                balance += 1
            elif char == ")":
                balance -= 1
                if balance == 0:
                    return text[match.start():index + 1]
        raise PddlDomainMergeError(f"Unclosed {section_name} section")

    def _tokenize_type_line(self, line: str) -> List[str]:
        return [token for token in line.replace("(", " ").replace(")", " ").split() if token]

    def _parse_type_atoms(self, atoms: Sequence[str], path: Path) -> List[PddlTypeDecl]:
        declarations: List[PddlTypeDecl] = []
        pending: List[str] = []
        index = 0
        while index < len(atoms):
            token = atoms[index]
            if token == "-":
                if not pending:
                    raise PddlDomainMergeError(f"Dangling '-' in :types section of {path}")
                if index + 1 >= len(atoms):
                    raise PddlDomainMergeError(f"Missing parent type after '-' in :types section of {path}")
                parent = atoms[index + 1]
                declarations.extend(PddlTypeDecl(name=name, parent=parent) for name in pending)
                pending = []
                index += 2
            else:
                pending.append(token)
                index += 1

        declarations.extend(PddlTypeDecl(name=name, parent=None) for name in pending)
        return declarations

    def _parse_types(self, tokens: Sequence[PddlExpr], path: Path) -> List[PddlTypeDecl]:
        return self._parse_type_atoms(self._expect_atoms(tokens, path, ":types"), path)

    def _parse_predicates(self, predicates: Sequence[PddlExpr], path: Path) -> List[List[PddlExpr]]:
        parsed: List[List[PddlExpr]] = []
        for predicate in predicates:
            if not isinstance(predicate, list):
                raise PddlDomainMergeError(f"Invalid predicate in {path}: {predicate}")
            if not predicate or not isinstance(predicate[0], str):
                raise PddlDomainMergeError(f"Invalid predicate in {path}: {self._format_inline(predicate)}")
            parsed.append(predicate)
        return parsed

    def _expect_atoms(self, values: Sequence[PddlExpr], path: Path, section_name: str) -> List[str]:
        atoms: List[str] = []
        for value in values:
            if not isinstance(value, str):
                raise PddlDomainMergeError(f"Expected atom in {section_name} section of {path}")
            atoms.append(value)
        return atoms

    def _render_domain(
        self,
        domain_name: str,
        requirements: Sequence[str],
        types: Sequence[PddlTypeDecl],
        predicates: Sequence[List[PddlExpr]],
        actions: Sequence[List[PddlExpr]],
    ) -> str:
        lines: List[str] = [f"(define (domain {domain_name})"]
        lines.extend(self._render_requirements(requirements))
        lines.append("")
        lines.extend(self._render_types(types))
        lines.append("")
        lines.extend(self._render_predicates(predicates))
        lines.append("")

        for action in actions:
            lines.extend(self._render_action(action))
            lines.append("")

        if lines[-1] == "":
            lines.pop()
        lines.append(")")
        return "\n".join(lines) + "\n"

    def _render_requirements(self, requirements: Sequence[str]) -> List[str]:
        lines = ["  (:requirements"]
        lines.extend(f"    {requirement}" for requirement in requirements)
        lines.append("  )")
        return lines

    def _render_types(self, types: Sequence[PddlTypeDecl]) -> List[str]:
        lines = ["  (:types"]
        for type_decl in types:
            if type_decl.parent:
                lines.append(f"    {type_decl.name} - {type_decl.parent}")
            else:
                lines.append(f"    {type_decl.name}")
        lines.append("  )")
        return lines

    def _render_predicates(self, predicates: Sequence[List[PddlExpr]]) -> List[str]:
        lines = ["  (:predicates"]
        lines.extend(f"    {self._format_inline(predicate)}" for predicate in predicates)
        lines.append("  )")
        return lines

    def _render_action(self, action: Sequence[PddlExpr]) -> List[str]:
        if len(action) < 2 or not isinstance(action[1], str):
            raise PddlDomainMergeError(f"Invalid action form while rendering: {self._format_inline(action)}")

        lines = [f"  (:action {action[1]}"]
        index = 2
        while index < len(action):
            key = action[index]
            if not isinstance(key, str):
                raise PddlDomainMergeError(f"Invalid action field in {action[1]}: {self._format_inline(action)}")
            if index + 1 >= len(action):
                raise PddlDomainMergeError(f"Missing value for action field {key} in {action[1]}")
            value = action[index + 1]
            self._append_labeled_expr(lines, key, value)
            index += 2
        lines.append("  )")
        return lines

    def _append_labeled_expr(self, lines: List[str], label: str, expr: PddlExpr) -> None:
        if self._is_inline_expr(expr):
            lines.append(f"    {label} {self._format_inline(expr)}")
            return

        rendered = self._format_expr(expr, indent=6).splitlines()
        lines.append(f"    {label} {rendered[0].lstrip()}")
        lines.extend(rendered[1:])

    def _format_expr(self, expr: PddlExpr, indent: int = 0) -> str:
        prefix = " " * indent
        if isinstance(expr, str):
            return prefix + expr
        if self._is_inline_expr(expr):
            return prefix + self._format_inline(expr)
        if not expr:
            return prefix + "()"

        head = expr[0]
        head_text = head if isinstance(head, str) else self._format_inline(head)
        lines = [prefix + f"({head_text}"]
        for item in expr[1:]:
            lines.append(self._format_expr(item, indent + 2))
        lines.append(prefix + ")")
        return "\n".join(lines)

    def _format_inline(self, expr: PddlExpr) -> str:
        if isinstance(expr, str):
            return expr
        return "(" + " ".join(self._format_inline(item) for item in expr) + ")"

    def _is_inline_expr(self, expr: PddlExpr) -> bool:
        return isinstance(expr, str) or all(isinstance(item, str) for item in expr)

    def _canonical_expr(self, expr: PddlExpr) -> Tuple:
        if isinstance(expr, str):
            return ("atom", self._atom_key(expr))
        return ("list", tuple(self._canonical_expr(item) for item in expr))

    def _atom_key(self, atom: str) -> str:
        return atom.lower()

    def _optional_atom_key(self, atom: Optional[str]) -> Optional[str]:
        return atom.lower() if atom is not None else None


def build_arg_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    default_base = repo_root / "resources" / "v2" / "allactionrobot_remaining.pddl"
    parser = argparse.ArgumentParser(description="Merge PDDL domain files into one all-action domain.")
    parser.add_argument(
        "domains",
        nargs="*",
        help="Domain files to merge with the base domain. Bare names are resolved in resources/v2/.",
    )
    parser.add_argument(
        "--base",
        default=str(default_base),
        help="Base domain file. Defaults to resources/v2/allactionrobot_remaining.pddl.",
    )
    parser.add_argument("--output", help="Output file. If omitted, merged PDDL is printed to stdout.")
    parser.add_argument("--domain-name", default="allactionrobot", help="Domain name to render in the merged output.")
    parser.add_argument(
        "--no-include-inaction",
        action="store_false",
        dest="include_inaction",
        help="Do not inject the legacy-compatible (inaction ?robot - robot) predicate.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    helper = PddlDomainMergeHelper()

    try:
        merged_text = helper.merge_domains(
            args.base,
            args.domains,
            domain_name=args.domain_name,
            include_inaction=args.include_inaction,
        )
    except PddlDomainMergeError as exc:
        print(f"merge_pddl_domains.py: error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(merged_text, encoding="utf-8")
    else:
        print(merged_text, end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
