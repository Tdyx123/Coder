"""LLM Call Logger - Shared utility for logging LLM requests and responses."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


class LLMCallLogger:
    _instance: Optional['LLMCallLogger'] = None
    _log_file: Optional[Path] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        logs_dir = Path(__file__).parent.parent / 'logs'
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._log_file = logs_dir / f'llm_calls_{timestamp}.jsonl'

    def log(self, log_entry: Dict[str, Any]) -> None:
        if self._log_file is None:
            self._initialize()
        with open(self._log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

    @property
    def log_file(self) -> Path:
        if self._log_file is None:
            self._initialize()
        return self._log_file


def log_llm_call(
    model: str,
    provider: str,
    messages: List[Dict],
    params: Dict[str, Any],
    response_text: str,
    usage: Optional[Dict[str, int]] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None
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

    logger = LLMCallLogger()
    logger.log(log_entry)


def get_llm_logger() -> LLMCallLogger:
    return LLMCallLogger()
