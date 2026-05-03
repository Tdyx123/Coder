import re
from pathlib import Path
from typing import List, Dict


class DataEngine:

    def extract_subtask_skill(self, folder: str) -> List[dict]:
        folder_path = Path(folder)
        if not folder_path.exists():
            raise FileNotFoundError(f"Folder not found: {folder}")

        results = []
        txt_files = list(folder_path.glob("*.txt"))

        for file_path in txt_files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            subtask_match = re.search(r"#Subtask\s+\d+(?:[:.]\s*)?(.+)", content)
            if not subtask_match:
                continue

            subtask_name = subtask_match.group(1).strip()
            skill_matches = re.findall(r"^([A-Za-z]+Object):", content, re.MULTILINE)
            skills = list(dict.fromkeys(skill_matches))

            results.append({"subtask": subtask_name, "skills": skills})

        return results
