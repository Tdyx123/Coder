import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from litellm import completion
from litellm.exceptions import APIError, RateLimitError, Timeout


MessageInput = Union[str, List[Dict[str, Any]]]
ProviderConfig = Dict[str, Any]
API_KEY_RETRY_DELAYS = [5, 10, 15]


class ProviderConfigError(ValueError):
    """Raised when a provider entry is missing required configuration."""


class _ApiKeyRotationPool:
    """Thread-safe hourly rotation cursor storage for provider API keys."""

    _ROTATION_INTERVAL = 3600

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._states: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def reserve_start_index(self, provider: ProviderConfig) -> int:
        provider_key = (provider["name"], provider["base_url"])
        api_keys = tuple(provider["api_keys"])

        with self._registry_lock:
            state = self._states.get(provider_key)
            if state is None or state["api_keys"] != api_keys:
                state = {
                    "api_keys": api_keys,
                    "next_index": 0,
                    "last_rotation": time.time(),
                    "lock": threading.Lock(),
                }
                self._states[provider_key] = state

        with state["lock"]:
            current_time = time.time()
            if current_time - state["last_rotation"] >= self._ROTATION_INTERVAL:
                state["next_index"] = (state["next_index"] + 1) % len(api_keys)
                state["last_rotation"] = current_time
            return state["next_index"]


_api_key_rotation_pool = _ApiKeyRotationPool()


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


def _normalize_provider(provider: Dict[str, Any]) -> ProviderConfig:
    if not isinstance(provider, dict):
        raise ProviderConfigError("Each provider entry must be a mapping")

    name = str(provider.get("name", "")).strip()
    base_url = str(provider.get("base_url", "")).strip()
    models = provider.get("models")
    api_keys = provider.get("api_keys")

    if not name:
        raise ProviderConfigError("Provider is missing a non-empty 'name'")
    if not base_url:
        raise ProviderConfigError(
            f"Provider '{name}' is missing a non-empty 'base_url'"
        )
    if not isinstance(models, list) or not models:
        raise ProviderConfigError(
            f"Provider '{name}' must define a non-empty 'models' list"
        )
    normalized_models = [str(model).strip() for model in models if str(model).strip()]
    if len(normalized_models) != len(models):
        raise ProviderConfigError(
            f"Provider '{name}' has empty model names in 'models'"
        )
    if not isinstance(api_keys, list) or not api_keys:
        raise ProviderConfigError(
            f"Provider '{name}' must define a non-empty 'api_keys' list"
        )
    normalized_api_keys = [str(key).strip() for key in api_keys if str(key).strip()]
    if len(normalized_api_keys) != len(api_keys):
        raise ProviderConfigError(
            f"Provider '{name}' has empty values in 'api_keys'"
        )

    normalized_provider = dict(provider)
    normalized_provider["name"] = name
    normalized_provider["base_url"] = base_url
    normalized_provider["models"] = normalized_models
    normalized_provider["api_keys"] = normalized_api_keys
    return normalized_provider


def _normalize_providers(raw_config: Any) -> List[ProviderConfig]:
    if not isinstance(raw_config, dict):
        raise ProviderConfigError("providers.yaml must contain a top-level mapping")

    providers = raw_config.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ProviderConfigError(
            "providers.yaml must define a non-empty top-level 'providers' list"
        )

    normalized_providers = [_normalize_provider(provider) for provider in providers]
    provider_names = [provider["name"] for provider in normalized_providers]
    duplicate_names = {
        name for name in provider_names if provider_names.count(name) > 1
    }
    if duplicate_names:
        duplicates = ", ".join(sorted(duplicate_names))
        raise ProviderConfigError(
            f"Provider names must be unique, found duplicates: {duplicates}"
        )
    return normalized_providers


def load_providers(
    providers_file: Optional[Union[str, Path]] = None,
) -> List[ProviderConfig]:
    if providers_file is None:
        providers_file = Path(__file__).parent / "providers.yaml"

    with open(providers_file, "r", encoding="utf-8") as f:
        return _normalize_providers(yaml.safe_load(f))


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


def _attach_response_metadata(response: Any, metadata: Dict[str, Any]) -> Any:
    if isinstance(response, dict):
        response.setdefault("_lammap", {}).update(metadata)
        return response

    current = getattr(response, "_lammap_metadata", {})
    if not isinstance(current, dict):
        current = {}
    current.update(metadata)
    try:
        setattr(response, "_lammap_metadata", current)
    except Exception:
        return response
    return response


def extract_response_metadata(response: Any) -> Dict[str, Any]:
    if isinstance(response, dict):
        metadata = response.get("_lammap", {})
        return metadata if isinstance(metadata, dict) else {}

    metadata = getattr(response, "_lammap_metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def complete_with_provider(
    model: str,
    prompt: MessageInput,
    provider: Dict[str, Any],
    max_tokens: int,
    temperature: float,
    stop: Optional[List[str]] = None,
    frequency_penalty: float = 0,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    min_p: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    repetition_penalty: Optional[float] = None,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Any:
    messages = _normalize_messages(prompt)
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_base": provider["base_url"],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "frequency_penalty": frequency_penalty,
        "custom_llm_provider": "openai"
    }
    if stop:
        kwargs["stop"] = stop
    optional_params = {
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "presence_penalty": presence_penalty,
        "repetition_penalty": repetition_penalty,
    }
    for key, value in optional_params.items():
        if value is not None:
            kwargs[key] = value
    if extra_body is not None:
        kwargs["extra_body"] = extra_body

    api_keys = provider["api_keys"]
    start_index = _api_key_rotation_pool.reserve_start_index(provider)
    last_retryable_error: Optional[Exception] = None
    max_attempts = min(len(api_keys), len(API_KEY_RETRY_DELAYS) + 1)

    for offset in range(max_attempts):
        key_index = (start_index + offset) % len(api_keys)
        kwargs["api_key"] = api_keys[key_index]
        try:
            response = completion(**kwargs)
            return _attach_response_metadata(
                response,
                {
                    "key_index": key_index,
                    "key_count": len(api_keys),
                    "provider_name": provider["name"],
                },
            )
        except Exception as exc:
            sanitized_error = _sanitize_litellm_error(exc)
            if is_rate_limit_error(sanitized_error) or is_retryable_error(sanitized_error):
                last_retryable_error = sanitized_error
                if offset < max_attempts - 1:
                    time.sleep(API_KEY_RETRY_DELAYS[offset])
                continue
            raise sanitized_error

    if last_retryable_error is not None:
        raise last_retryable_error

    raise RuntimeError(f"Provider '{provider['name']}' has no usable API keys")


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
