import sys
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm_client
import llm_handler


class SimpleObj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class LLMClientTests(unittest.TestCase):
    def setUp(self) -> None:
        with llm_client._api_key_rotation_pool._registry_lock:
            llm_client._api_key_rotation_pool._states.clear()

        self.provider = {
            "name": "deepseek",
            "base_url": "https://api.example.com/v1",
            "api_keys": ["key-a", "key-b", "key-c"],
            "models": ["deepseek-chat"],
        }

    def _stream_chunk(self, content=None, finish_reason=None, usage=None):
        delta = {}
        if content is not None:
            delta["content"] = content

        chunk = {
            "choices": [
                {
                    "delta": delta,
                    "finish_reason": finish_reason,
                    "index": 0,
                }
            ]
        }
        if usage is not None:
            chunk["usage"] = usage
        return chunk

    def _stream_response(self, *contents, usage=None):
        chunks = [self._stream_chunk(content=content) for content in contents]
        chunks.append(self._stream_chunk(finish_reason="stop", usage=usage))
        return iter(chunks)

    def _complete_once(self, provider=None):
        return llm_client.complete_with_provider(
            model="deepseek-chat",
            prompt="hello",
            provider=provider or self.provider,
            max_tokens=16,
            temperature=0.1,
        )

    def test_load_providers_rejects_missing_api_keys(self):
        config_path = ROOT / "tests" / "fixtures" / "providers_missing_api_keys.yaml"

        with self.assertRaises(llm_client.ProviderConfigError):
            llm_client.load_providers(config_path)

    def test_load_providers_rejects_empty_api_key_list(self):
        config_path = ROOT / "tests" / "fixtures" / "providers_empty_api_keys.yaml"

        with self.assertRaises(llm_client.ProviderConfigError):
            llm_client.load_providers(config_path)

    def test_load_providers_rejects_blank_api_key_entries(self):
        config_path = ROOT / "tests" / "fixtures" / "providers_blank_api_key.yaml"

        with self.assertRaises(llm_client.ProviderConfigError):
            llm_client.load_providers(config_path)

    def test_round_robin_uses_next_key_for_each_request(self):
        seen_keys = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            return self._stream_response("ok")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            first = self._complete_once()
            second = self._complete_once()
            third = self._complete_once()

        self.assertEqual(seen_keys, ["key-a", "key-b", "key-c"])
        self.assertEqual(llm_client.extract_response_metadata(first)["key_index"], 0)
        self.assertEqual(llm_client.extract_response_metadata(second)["key_index"], 1)
        self.assertEqual(llm_client.extract_response_metadata(third)["key_index"], 2)

    def test_complete_with_provider_passes_extra_body_to_completion(self):
        seen_kwargs = []
        extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

        def fake_completion(**kwargs):
            seen_kwargs.append(kwargs)
            return self._stream_response("ok")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            llm_client.complete_with_provider(
                model="qwen3.5-test",
                prompt="hello",
                provider=self.provider,
                max_tokens=16,
                temperature=0.1,
                top_p=0.95,
                top_k=20,
                min_p=0.0,
                presence_penalty=1.5,
                repetition_penalty=1.0,
                extra_body=extra_body,
            )

        self.assertEqual(seen_kwargs[0]["extra_body"], extra_body)
        self.assertEqual(seen_kwargs[0]["top_p"], 0.95)
        self.assertEqual(seen_kwargs[0]["top_k"], 20)
        self.assertEqual(seen_kwargs[0]["min_p"], 0.0)
        self.assertEqual(seen_kwargs[0]["presence_penalty"], 1.5)
        self.assertEqual(seen_kwargs[0]["repetition_penalty"], 1.0)

    def test_complete_with_provider_streams_and_aggregates_text(self):
        seen_kwargs = []

        def fake_completion(**kwargs):
            seen_kwargs.append(kwargs)
            return self._stream_response("Hel", "lo")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            response = llm_client.complete_with_provider(
                model="deepseek-chat",
                prompt="hello",
                provider=self.provider,
                max_tokens=16,
                temperature=0.1,
            )

        self.assertTrue(seen_kwargs[0]["stream"])
        self.assertEqual(seen_kwargs[0]["stream_options"], {"include_usage": True})
        self.assertEqual(llm_client.extract_text(response), "Hello")
        self.assertEqual(response["choices"][0]["finish_reason"], "stop")
        self.assertEqual(llm_client.extract_response_metadata(response)["key_index"], 0)

    def test_complete_with_provider_handles_object_stream_chunks(self):
        def chunk(content=None, finish_reason=None):
            delta = SimpleObj()
            if content is not None:
                delta.content = content
            return SimpleObj(
                model="deepseek-chat",
                choices=[
                    SimpleObj(delta=delta, finish_reason=finish_reason, index=0)
                ],
            )

        with patch.object(
            llm_client,
            "completion",
            return_value=iter([chunk("Hel"), chunk("lo", finish_reason="stop")]),
        ):
            response = self._complete_once()

        self.assertEqual(llm_client.extract_text(response), "Hello")
        self.assertEqual(response["choices"][0]["finish_reason"], "stop")

    def test_complete_with_provider_preserves_stream_usage(self):
        usage = {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
        }

        with patch.object(
            llm_client,
            "completion",
            return_value=self._stream_response("ok", usage=usage),
        ):
            response = self._complete_once()

        self.assertEqual(llm_client.extract_usage(response), usage)

    def test_complete_with_provider_stream_without_usage_returns_none(self):
        with patch.object(
            llm_client,
            "completion",
            return_value=self._stream_response("ok"),
        ):
            response = self._complete_once()

        self.assertIsNone(llm_client.extract_usage(response))

    def test_complete_with_provider_omits_frequency_penalty_when_none(self):
        seen_kwargs = []

        def fake_completion(**kwargs):
            seen_kwargs.append(kwargs)
            return self._stream_response("ok")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            llm_client.complete_with_provider(
                model="gpt5-mini",
                prompt="hello",
                provider=self.provider,
                max_tokens=16,
                temperature=0.1,
                frequency_penalty=None,
            )

        self.assertNotIn("frequency_penalty", seen_kwargs[0])

    def test_llm_handler_applies_qwen35_request_params(self):
        class DummyConfig:
            def get(self, section, key, default=None):
                return default

        handler = llm_handler.LLMHandler(config=DummyConfig())

        with patch.object(handler, "_get_provider_for_model", return_value=(self.provider, "deepseek")), \
                patch.object(llm_handler, "complete_with_provider", return_value={}) as fake_complete, \
                patch.object(llm_handler, "extract_text", return_value="ok"), \
                patch.object(llm_handler, "extract_response_metadata", return_value={}), \
                patch.object(llm_handler, "extract_usage", return_value=None), \
                patch.object(llm_handler, "log_llm_call") as fake_log:
            handler.query_model("hello", "qwen3.5-test", temperature=0.2)

        request_kwargs = fake_complete.call_args.kwargs
        self.assertEqual(request_kwargs["temperature"], 1.0)
        self.assertEqual(request_kwargs["top_p"], 0.95)
        self.assertEqual(request_kwargs["top_k"], 20)
        self.assertEqual(request_kwargs["min_p"], 0.0)
        self.assertEqual(request_kwargs["presence_penalty"], 1.5)
        self.assertEqual(request_kwargs["repetition_penalty"], 1.0)
        self.assertEqual(request_kwargs["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})

        log_params = fake_log.call_args.kwargs["params"]
        self.assertEqual(log_params["temperature"], 1.0)
        self.assertEqual(log_params["top_p"], 0.95)
        self.assertEqual(log_params["top_k"], 20)
        self.assertEqual(log_params["min_p"], 0.0)
        self.assertEqual(log_params["presence_penalty"], 1.5)
        self.assertEqual(log_params["repetition_penalty"], 1.0)

    def test_llm_handler_drops_frequency_penalty_for_gpt5_models(self):
        class DummyConfig:
            def get(self, section, key, default=None):
                return default

        handler = llm_handler.LLMHandler(config=DummyConfig())

        with patch.object(handler, "_get_provider_for_model", return_value=(self.provider, "deepseek")), \
                patch.object(llm_handler, "complete_with_provider", return_value={}) as fake_complete, \
                patch.object(llm_handler, "extract_text", return_value="ok"), \
                patch.object(llm_handler, "extract_response_metadata", return_value={}), \
                patch.object(llm_handler, "extract_usage", return_value=None), \
                patch.object(llm_handler, "log_llm_call") as fake_log:
            handler.query_model("hello", "gpt-5-mini", frequency_penalty=0.6)

        self.assertIsNone(fake_complete.call_args.kwargs["frequency_penalty"])
        self.assertNotIn("frequency_penalty", fake_log.call_args.kwargs["params"])

    def test_llm_handler_keeps_frequency_penalty_for_other_models(self):
        class DummyConfig:
            def get(self, section, key, default=None):
                return default

        handler = llm_handler.LLMHandler(config=DummyConfig())

        with patch.object(handler, "_get_provider_for_model", return_value=(self.provider, "deepseek")), \
                patch.object(llm_handler, "complete_with_provider", return_value={}) as fake_complete, \
                patch.object(llm_handler, "extract_text", return_value="ok"), \
                patch.object(llm_handler, "extract_response_metadata", return_value={}), \
                patch.object(llm_handler, "extract_usage", return_value=None), \
                patch.object(llm_handler, "log_llm_call") as fake_log:
            handler.query_model("hello", "deepseek-chat", frequency_penalty=0.6)

        self.assertEqual(fake_complete.call_args.kwargs["frequency_penalty"], 0.6)
        self.assertEqual(fake_log.call_args.kwargs["params"]["frequency_penalty"], 0.6)

    def test_llm_handler_leaves_extra_body_empty_for_other_models(self):
        class DummyConfig:
            def get(self, section, key, default=None):
                return default

        handler = llm_handler.LLMHandler(config=DummyConfig())

        with patch.object(handler, "_get_provider_for_model", return_value=(self.provider, "deepseek")), \
                patch.object(llm_handler, "complete_with_provider", return_value={}) as fake_complete, \
                patch.object(llm_handler, "extract_text", return_value="ok"), \
                patch.object(llm_handler, "extract_response_metadata", return_value={}), \
                patch.object(llm_handler, "extract_usage", return_value=None), \
                patch.object(llm_handler, "log_llm_call"):
            handler.query_model("hello", "deepseek-chat")

        self.assertIsNone(fake_complete.call_args.kwargs["extra_body"])
        self.assertIsNone(fake_complete.call_args.kwargs["top_p"])
        self.assertIsNone(fake_complete.call_args.kwargs["top_k"])
        self.assertIsNone(fake_complete.call_args.kwargs["min_p"])
        self.assertIsNone(fake_complete.call_args.kwargs["presence_penalty"])
        self.assertIsNone(fake_complete.call_args.kwargs["repetition_penalty"])

    def test_round_robin_wraps_after_last_key(self):
        seen_keys = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            return self._stream_response("ok")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            responses = [self._complete_once() for _ in range(5)]

        self.assertEqual(seen_keys, ["key-a", "key-b", "key-c", "key-a", "key-b"])
        self.assertEqual(
            [llm_client.extract_response_metadata(response)["key_index"] for response in responses],
            [0, 1, 2, 0, 1],
        )

    def test_retryable_error_falls_through_to_next_key_in_same_request(self):
        seen_keys = []
        sleep_calls = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            if kwargs["api_key"] == "key-a":
                raise Exception("rate limit exceeded")
            return self._stream_response("ok")

        with patch.object(llm_client, "completion", side_effect=fake_completion), patch.object(
            llm_client.time, "sleep", side_effect=sleep_calls.append
        ):
            response = self._complete_once()

        self.assertEqual(seen_keys, ["key-a", "key-b"])
        self.assertEqual(sleep_calls, [5])
        self.assertEqual(llm_client.extract_response_metadata(response)["key_index"], 1)

    def test_stream_consumption_retry_discards_partial_text(self):
        seen_keys = []
        sleep_calls = []

        def failing_stream():
            yield self._stream_chunk("partial")
            raise Exception("service unavailable")

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            if kwargs["api_key"] == "key-a":
                return failing_stream()
            return self._stream_response("final")

        with patch.object(llm_client, "completion", side_effect=fake_completion), patch.object(
            llm_client.time, "sleep", side_effect=sleep_calls.append
        ):
            response = self._complete_once()

        self.assertEqual(seen_keys, ["key-a", "key-b"])
        self.assertEqual(sleep_calls, [5])
        self.assertEqual(llm_client.extract_text(response), "final")
        self.assertEqual(llm_client.extract_response_metadata(response)["key_index"], 1)

    def test_all_retryable_keys_failing_raises_last_error(self):
        seen_keys = []
        sleep_calls = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            raise Exception("service unavailable")

        with patch.object(llm_client, "completion", side_effect=fake_completion), patch.object(
            llm_client.time, "sleep", side_effect=sleep_calls.append
        ):
            with self.assertRaisesRegex(Exception, "service unavailable"):
                self._complete_once(
                    provider={**self.provider, "api_keys": ["key-a", "key-b", "key-c", "key-d"]}
                )

        self.assertEqual(seen_keys, ["key-a", "key-b", "key-c", "key-d"])
        self.assertEqual(sleep_calls, [5, 10, 15])

    def test_non_retryable_error_does_not_switch_keys(self):
        seen_keys = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            raise Exception("bad request payload")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            with self.assertRaisesRegex(Exception, "bad request payload"):
                self._complete_once(provider={**self.provider, "api_keys": ["key-a", "key-b"]})

        self.assertEqual(seen_keys, ["key-a"])

    def test_concurrent_requests_share_rotation_pool(self):
        responses = []

        def fake_completion(**kwargs):
            time.sleep(0.02)
            return self._stream_response("ok")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = [executor.submit(self._complete_once) for _ in range(6)]
                for future in futures:
                    responses.append(future.result())

        key_indices = [
            llm_client.extract_response_metadata(response)["key_index"]
            for response in responses
        ]
        self.assertEqual(Counter(key_indices), Counter({0: 2, 1: 2, 2: 2}))

    def test_retryable_key_switching_is_capped_at_three_retries(self):
        seen_keys = []
        sleep_calls = []
        provider = {
            **self.provider,
            "api_keys": ["key-a", "key-b", "key-c", "key-d", "key-e"],
        }

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            raise Exception("gateway timeout")

        with patch.object(llm_client, "completion", side_effect=fake_completion), patch.object(
            llm_client.time, "sleep", side_effect=sleep_calls.append
        ):
            with self.assertRaisesRegex(Exception, "gateway timeout"):
                self._complete_once(provider=provider)

        self.assertEqual(seen_keys, ["key-a", "key-b", "key-c", "key-d"])
        self.assertEqual(sleep_calls, [5, 10, 15])


if __name__ == "__main__":
    unittest.main()
