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
            return {}

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            first = self._complete_once()
            second = self._complete_once()
            third = self._complete_once()

        self.assertEqual(seen_keys, ["key-a", "key-b", "key-c"])
        self.assertEqual(llm_client.extract_response_metadata(first)["key_index"], 0)
        self.assertEqual(llm_client.extract_response_metadata(second)["key_index"], 1)
        self.assertEqual(llm_client.extract_response_metadata(third)["key_index"], 2)

    def test_round_robin_wraps_after_last_key(self):
        seen_keys = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            return {}

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            responses = [self._complete_once() for _ in range(5)]

        self.assertEqual(seen_keys, ["key-a", "key-b", "key-c", "key-a", "key-b"])
        self.assertEqual(
            [llm_client.extract_response_metadata(response)["key_index"] for response in responses],
            [0, 1, 2, 0, 1],
        )

    def test_retryable_error_falls_through_to_next_key_in_same_request(self):
        seen_keys = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            if kwargs["api_key"] == "key-a":
                raise Exception("rate limit exceeded")
            return {}

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            response = self._complete_once()

        self.assertEqual(seen_keys, ["key-a", "key-b"])
        self.assertEqual(llm_client.extract_response_metadata(response)["key_index"], 1)

    def test_all_retryable_keys_failing_raises_last_error(self):
        seen_keys = []

        def fake_completion(**kwargs):
            seen_keys.append(kwargs["api_key"])
            raise Exception("service unavailable")

        with patch.object(llm_client, "completion", side_effect=fake_completion):
            with self.assertRaisesRegex(Exception, "service unavailable"):
                self._complete_once(provider={**self.provider, "api_keys": ["key-a", "key-b"]})

        self.assertEqual(seen_keys, ["key-a", "key-b"])

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
            return {"used_key": kwargs["api_key"]}

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


if __name__ == "__main__":
    unittest.main()
