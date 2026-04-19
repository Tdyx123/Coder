from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml
from litellm import completion
from litellm.exceptions import APIError, RateLimitError, Timeout


MessageInput = Union[str, List[Dict[str, Any]]]


def _sanitize_litellm_error(exc: Exception) -> Exception:
    """Strip noisy LiteLLM help text from exception messages."""
    message = str(exc)
    filtered_lines = []

    for line in message.splitlines():
        if "Provider List:" in line:
            continue
        filtered_lines.append(line)

    cleaned = "\n".join(filtered_lines).strip() or message
    if cleaned != message:
        exc.args = (cleaned,)
    return exc


def load_providers(providers_file: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    if providers_file is None:
        providers_file = Path(__file__).parent / "providers.yaml"

    with open(providers_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["providers"]


def get_available_models(providers_file: Optional[Union[str, Path]] = None) -> List[str]:
    return [model for provider in load_providers(providers_file) for model in provider["models"]]


def get_provider_for_model(
    model: str, providers: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    providers = providers or load_providers()
    for provider in providers:
        if model in provider["models"]:
            return provider
    raise ValueError(f"Model {model} not found in any provider")


def _normalize_messages(prompt: MessageInput) -> List[Dict[str, Any]]:
    if isinstance(prompt, list):
        return prompt
    return [{"role": "user", "content": prompt}]


def complete_with_provider(
    model: str,
    prompt: MessageInput,
    provider: Dict[str, Any],
    max_tokens: int,
    temperature: float,
    stop: Optional[List[str]] = None,
    frequency_penalty: float = 0,
) -> Any:
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(prompt),
        "api_key": provider["api_key"],
        "api_base": provider["base_url"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "frequency_penalty": frequency_penalty,
        "custom_llm_provider": "openai"
    }
    if stop:
        kwargs["stop"] = stop
    try:
        return completion(**kwargs)
    except Exception as exc:
        raise _sanitize_litellm_error(exc)


def extract_text(response: Any) -> str:
    choice = response.choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message", {})

    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                parts.append(item.text)
        return "".join(parts).strip()

    return (content or "").strip()


def extract_usage(response: Any) -> Optional[Dict[str, Any]]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    return {
        "prompt_tokens": _get(usage, "prompt_tokens"),
        "completion_tokens": _get(usage, "completion_tokens"),
        "total_tokens": _get(usage, "total_tokens"),
    }


def is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    return "rate limit" in str(exc).lower()


def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (APIError, Timeout)):
        return True

    error_text = str(exc).lower()
    retry_markers = [
        "timeout",
        "temporarily unavailable",
        "connection error",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    ]
    return any(marker in error_text for marker in retry_markers)
