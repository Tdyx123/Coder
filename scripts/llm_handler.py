import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from file_processor import PDDLError
from llm_client import (
    complete_with_provider,
    extract_response_metadata,
    extract_text,
    extract_usage,
    get_provider_for_model,
    is_rate_limit_error,
    is_retryable_error,
    load_providers,
)
from llm_logger import log_llm_call
from run_config import DEFAULT_RUN_CONFIG, RunConfig, load_run_config


DEFAULT_MAX_TOKENS = 20000
DEFAULT_TEMPERATURE = DEFAULT_RUN_CONFIG["llm"]["default_temperature"]
DEFAULT_RETRY_DELAY = DEFAULT_RUN_CONFIG["llm"]["default_retry_delay"]
MAX_RETRIES = DEFAULT_RUN_CONFIG["llm"]["max_retries"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class LLMError(Exception):
    """Exception raised for Language Model related errors."""


class LLMHandler:
    """Handles interactions with Language Models (LLMs)."""

    def __init__(self, config: Optional[RunConfig] = None):
        """Initialize the LLM handler."""
        self.config = config or load_run_config(_repo_root(), error_cls=PDDLError)
        self.providers = None

    def _load_providers(self):
        """Load providers from yaml file."""
        if self.providers is None:
            self.providers = load_providers(self.config.providers_file)
        return self.providers

    def _get_provider_for_model(self, model):
        """Get provider configuration for the given model."""
        provider = get_provider_for_model(model, self._load_providers())
        return provider, provider["name"]

    def query_model(
        self,
        prompt: Union[str, List[Dict]],
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
        logprobs: Optional[int] = 1,
        frequency_penalty: float = 0,
    ) -> Tuple[dict, str]:
        """Query the configured language model."""
        if max_tokens is None:
            max_tokens = DEFAULT_MAX_TOKENS
        if temperature is None:
            temperature = float(self.config.get("llm", "default_temperature", DEFAULT_TEMPERATURE))

        retry_delay = float(self.config.get("llm", "default_retry_delay", DEFAULT_RETRY_DELAY))
        max_retries = int(self.config.get("llm", "max_retries", MAX_RETRIES))
        provider_config, provider = self._get_provider_for_model(model)
        extra_body = None
        sampling_params = {
            "top_p": None,
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "repetition_penalty": None,
        }
        if model.lower().startswith("qwen3.5"):
            temperature = 1.0
            extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
            sampling_params.update({
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
                "repetition_penalty": 1.0,
            })

        for attempt in range(max_retries):
            try:
                start_time = time.time()
                response = complete_with_provider(
                    model=model,
                    prompt=prompt,
                    provider=provider_config,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                    frequency_penalty=frequency_penalty,
                    top_p=sampling_params["top_p"],
                    top_k=sampling_params["top_k"],
                    min_p=sampling_params["min_p"],
                    presence_penalty=sampling_params["presence_penalty"],
                    repetition_penalty=sampling_params["repetition_penalty"],
                    extra_body=extra_body,
                )
                duration_ms = (time.time() - start_time) * 1000
                text = extract_text(response)
                response_metadata = extract_response_metadata(response)
                usage = extract_usage(response)

                log_params = {
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "frequency_penalty": frequency_penalty,
                }
                log_params.update({
                    key: value
                    for key, value in sampling_params.items()
                    if value is not None
                })

                log_llm_call(
                    model=model,
                    provider=provider,
                    messages=prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}],
                    params=log_params,
                    response_text=text,
                    usage=usage,
                    duration_ms=duration_ms,
                    key_index=response_metadata.get("key_index"),
                )

                return response, text

            except Exception as e:
                if is_rate_limit_error(e):
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    raise LLMError("Rate limit exceeded")

                if is_retryable_error(e):
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    raise LLMError(f"API Error after all retries: {str(e)}")

                raise LLMError(f"Unexpected error in LLM query: {str(e)}")
