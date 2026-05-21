import re
from typing import Dict, List, Optional, Tuple


class ParsingUtils:
    """Shared parsing helpers for decomposition subtasks and robot assignments."""

    MARKDOWN_SUBTASK_HEADER_RE = re.compile(
        r'^(?:#\s*)?\*\*\s*Sub\s*Task\s*(?P<number>\d+)\s*(?P<separator>:)\s*(?P<title>.*?)\s*\*\*\s*$',
        re.IGNORECASE,
    )
    HASH_SUBTASK_HEADER_RE = re.compile(
        r'^#\s*Sub\s*Task\s*(?P<number>\d+)\s*(?P<separator>[:.\s])(?P<title>.*?)\s*$',
        re.IGNORECASE,
    )
    BARE_SUBTASK_HEADER_RE = re.compile(
        r'^Sub\s*Task\s*(?P<number>\d+)\s*:\s*(?P<title>.*?)\s*$',
        re.IGNORECASE,
    )
    MARKDOWN_TASK_DONE_FOOTER_RE = re.compile(
        r'^\*\*Task\b.*\bis\s+done\.?\*\*\s*$',
        re.IGNORECASE,
    )

    @classmethod
    def match_subtask_header_line(
        cls,
        line: str,
        allow_bare: bool = False,
        allow_hash_space_separator: bool = True,
    ) -> Optional[re.Match]:
        stripped = line.strip()
        if not stripped:
            return None

        match = cls.MARKDOWN_SUBTASK_HEADER_RE.match(stripped)
        if match:
            return match

        match = cls.HASH_SUBTASK_HEADER_RE.match(stripped)
        if match and (allow_hash_space_separator or match.group("separator") == ":"):
            return match

        if allow_bare:
            return cls.BARE_SUBTASK_HEADER_RE.match(stripped)

        return None

    @classmethod
    def is_subtask_header_line(
        cls,
        line: str,
        allow_bare: bool = False,
        allow_hash_space_separator: bool = True,
    ) -> bool:
        return cls.match_subtask_header_line(
            line,
            allow_bare=allow_bare,
            allow_hash_space_separator=allow_hash_space_separator,
        ) is not None

    @classmethod
    def normalize_subtask_header_line(
        cls,
        line: str,
        allow_bare: bool = False,
        allow_hash_space_separator: bool = True,
    ) -> str:
        match = cls.match_subtask_header_line(
            line,
            allow_bare=allow_bare,
            allow_hash_space_separator=allow_hash_space_separator,
        )
        if not match:
            return line.rstrip()

        stripped = line.strip()
        is_markdown_header = stripped.startswith("**") or bool(re.match(r'^#\s*\*\*', stripped))
        if not is_markdown_header:
            return line.rstrip()

        title = match.group("title").strip()
        if title:
            return f"#SubTask {match.group('number')}: {title}"
        return f"#SubTask {match.group('number')}:"

    @classmethod
    def extract_subtask_blocks(
        cls,
        text: str,
        allow_bare: bool = False,
        allow_hash_space_separator: bool = True,
        normalize_headers: bool = False,
    ) -> List[str]:
        lines = text.splitlines()
        header_indices = [
            idx
            for idx, line in enumerate(lines)
            if cls.is_subtask_header_line(
                line,
                allow_bare=allow_bare,
                allow_hash_space_separator=allow_hash_space_separator,
            )
        ]

        if not header_indices:
            stripped = text.strip()
            return [stripped] if stripped else []

        blocks = []
        for header_pos, start_idx in enumerate(header_indices):
            end_idx = header_indices[header_pos + 1] if header_pos + 1 < len(header_indices) else len(lines)
            block_lines = lines[start_idx:end_idx]
            if normalize_headers and block_lines:
                block_lines[0] = cls.normalize_subtask_header_line(
                    block_lines[0],
                    allow_bare=allow_bare,
                    allow_hash_space_separator=allow_hash_space_separator,
                )
            block = "\n".join(block_lines).strip()
            if block:
                blocks.append(block)

        return blocks

    @classmethod
    def is_subtask_trailing_line(cls, line: str) -> bool:
        stripped = line.strip()
        return (
            not stripped
            or stripped.startswith("```")
            or stripped.startswith("#")
            or cls.MARKDOWN_TASK_DONE_FOOTER_RE.match(stripped) is not None
        )

    @classmethod
    def extract_subtasks(cls, decomposed_plan: str) -> List[str]:
        """Extract executable subtask sections from a decomposed plan."""
        if not any(cls.is_subtask_header_line(line) for line in decomposed_plan.splitlines()):
            return [decomposed_plan.strip()] if decomposed_plan.strip() else []

        subtasks = cls.extract_subtask_blocks(decomposed_plan, normalize_headers=True)
        filtered_subtasks = []

        for subtask in subtasks:
            lines = subtask.splitlines()

            while lines and cls.is_subtask_trailing_line(lines[-1]):
                lines.pop()

            if len(lines) < 10:
                continue

            filtered_subtasks.append('\n'.join(lines))

        return filtered_subtasks

    @staticmethod
    def sequence_assignment_re() -> re.Pattern:
        return re.compile(
            r'\bSub\s*Task\s*#?\s*(\d+)\b'
            r'(?:\s*\([^)]*\))?'
            r'\s*(?::|[-–—])\s*'
            r'Robot\s*[#_\-\s]*(\d+)\b',
            re.IGNORECASE,
        )

    @staticmethod
    def is_sequence_header(line: str) -> bool:
        normalized = line.strip().strip('*').strip('"').strip("'")
        return bool(re.search(r'\bSequence\s+of\s+Operations?\b\s*:?', normalized, re.IGNORECASE))

    @staticmethod
    def is_sequence_boundary(line: str) -> bool:
        return bool(
            re.match(
                r'(?i)^(```|robots\s*=|objects\s*=|#?\s*'
                r'(task allocation|task description|general task|example|solution|'
                r'problem content|additional output rules)\b)',
                line.strip(),
            )
        )

    @staticmethod
    def merge_sequence_lines(lines: List[str]) -> List[str]:
        merged: List[str] = []
        idx = 0
        subtask_only_re = re.compile(r'^\s*Sub\s*Task\s*#?\s*\d+\s*$', re.IGNORECASE)
        robot_bullet_re = re.compile(r'^\s*[-–—]\s*Robot\s*[#_\-\s]*\d+\b', re.IGNORECASE)

        while idx < len(lines):
            line = lines[idx]
            if idx + 1 < len(lines) and subtask_only_re.match(line) and robot_bullet_re.match(lines[idx + 1]):
                merged.append(f"{line} {lines[idx + 1]}")
                idx += 2
            else:
                merged.append(line)
                idx += 1

        return merged

    @classmethod
    def extract_sequence_sections(cls, allocated_plan: str) -> List[List[str]]:
        sections: List[List[str]] = []
        current: Optional[List[str]] = None
        assignment_re = cls.sequence_assignment_re()

        for raw_line in allocated_plan.strip().split('\n'):
            line = raw_line.strip()

            if cls.is_sequence_header(line):
                if current is not None:
                    sections.append(current)
                current = []
                continue

            if current is None:
                continue
            if not line:
                continue
            if cls.is_sequence_boundary(line) and not assignment_re.search(line):
                sections.append(current)
                current = None
                continue

            current.append(line)

        if current is not None:
            sections.append(current)

        return sections

    @classmethod
    def parse_sequence_section(
        cls,
        lines: List[str],
    ) -> Tuple[List[str], Dict[int, int]]:
        assignment_re = cls.sequence_assignment_re()
        assignments: Dict[int, int] = {}
        line_pairs_by_line: List[List[Tuple[int, int]]] = []
        last_occurrence: Dict[int, Tuple[int, int]] = {}

        for line in cls.merge_sequence_lines(lines):
            line_pairs: List[Tuple[int, int]] = []
            for match in assignment_re.finditer(line):
                subtask_num = int(match.group(1))
                robot_num = int(match.group(2))
                assignments[subtask_num] = robot_num
                line_pairs.append((subtask_num, robot_num))

            if line_pairs:
                line_idx = len(line_pairs_by_line)
                for pair_idx, (subtask_num, _) in enumerate(line_pairs):
                    last_occurrence[subtask_num] = (line_idx, pair_idx)
                line_pairs_by_line.append(line_pairs)

        normalized_lines: List[str] = []
        for line_idx, line_pairs in enumerate(line_pairs_by_line):
            final_pairs = [
                (subtask_num, robot_num)
                for pair_idx, (subtask_num, robot_num) in enumerate(line_pairs)
                if last_occurrence[subtask_num] == (line_idx, pair_idx)
            ]
            if final_pairs:
                normalized_lines.append(
                    ''.join(
                        f"Subtask {subtask_num}: Robot {robot_num};"
                        for subtask_num, robot_num in final_pairs
                    )
                )

        return normalized_lines, assignments

    @classmethod
    def extract_sequence_operations(cls, allocated_plan: str) -> List[str]:
        sections = cls.extract_sequence_sections(allocated_plan)
        if not sections:
            sections = [allocated_plan.strip().split('\n')]

        candidates: List[Tuple[int, int, List[str], Dict[int, int]]] = []
        for idx, section_lines in enumerate(sections):
            normalized_lines, assignments = cls.parse_sequence_section(section_lines)
            coverage = len(assignments)
            candidates.append((coverage, idx, normalized_lines, assignments))

        if not candidates:
            return []

        nonempty_candidates = [candidate for candidate in candidates if candidate[0] > 0]
        if not nonempty_candidates:
            return []
        _, _, selected_lines, _ = max(
            nonempty_candidates,
            key=lambda item: (item[0], item[1]),
        )

        return selected_lines

    @classmethod
    def extract_robot_assignments(
        cls,
        sequence_operations: List[str],
    ) -> Dict[int, int]:
        assignments: Dict[int, int] = {}
        assignment_re = cls.sequence_assignment_re()

        for line in sequence_operations:
            for match in assignment_re.finditer(line):
                subtask_num = int(match.group(1))
                robot_num = int(match.group(2))
                assignments[subtask_num] = robot_num

        return assignments
