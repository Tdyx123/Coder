"""LLM Call Logger - Shared utility for logging LLM requests and responses."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


class LLMCallLogger:
    _instance: Optional['LLMCallLogger'] = None
    _write_lock = threading.Lock()
    _thread_local = threading.local()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        return None

    def log(self, log_entry: Dict[str, Any]) -> None:
        entry = dict(log_entry)
        context = self.get_context()
        if context:
            entry["context"] = context
            for key in ("instance_id", "task_index", "task", "task_run_dir"):
                if key in context:
                    entry[key] = context[key]
        entry.setdefault("thread_name", threading.current_thread().name)

        task_log_file = self._resolve_task_log_file(context)
        if task_log_file is None:
            return
        serialized = json.dumps(entry, ensure_ascii=False) + '\n'

        with self._write_lock:
            task_log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(task_log_file, 'a', encoding='utf-8') as f:
                f.write(serialized)

    def set_context(self, **context: Any) -> None:
        current = getattr(self._thread_local, "context", {}).copy()
        current.update({key: value for key, value in context.items() if value is not None})
        self._thread_local.context = current

    def clear_context(self) -> None:
        self._thread_local.context = {}

    def get_context(self) -> Dict[str, Any]:
        return getattr(self._thread_local, "context", {}).copy()

    def _resolve_task_log_file(self, context: Dict[str, Any]) -> Optional[Path]:
        task_log_file = context.get("task_log_file")
        if task_log_file:
            return Path(task_log_file)
        task_run_dir = context.get("task_run_dir")
        if not task_run_dir:
            return None
        return Path(task_run_dir) / "00_llm" / "llm_calls.jsonl"

    @property
    def log_file(self) -> Path:
        context = self.get_context()
        log_file = self._resolve_task_log_file(context)
        if log_file is None:
            raise RuntimeError("LLM logger task context is not set")
        return log_file


def log_llm_call(
    model: str,
    provider: str,
    messages: List[Dict],
    params: Dict[str, Any],
    response_text: str,
    usage: Optional[Dict[str, int]] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None,
    key_index: Optional[int] = None,
) -> None:
    log_entry = {
        'timestamp': datetime.now().isoformat(),
        'model': model,
        'provider': provider,
        'messages': messages,
        'params': params,
        'response_text': response_text,
        'duration_ms': round(duration_ms, 2) if duration_ms is not None else None,
    }
    if usage is not None:
        log_entry['usage'] = usage
    if error is not None:
        log_entry['error'] = error
    if key_index is not None:
        log_entry['key_index'] = key_index

    logger = LLMCallLogger()
    logger.log(log_entry)


def get_llm_logger() -> LLMCallLogger:
    return LLMCallLogger()
