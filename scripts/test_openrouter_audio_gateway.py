#!/usr/bin/env python3
"""Dependency-free tests for OpenRouter audio capability validation."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


MODULE_PATH = (
    pathlib.Path(__file__).parents[1] / "litellm/llms/openrouter/audio_catalog.py"
)
SPEC = importlib.util.spec_from_file_location("openrouter_audio_catalog", MODULE_PATH)
assert SPEC and SPEC.loader
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


def _load_whisper_transformation() -> type:
    """Load the real request transformer with only its import-time types stubbed.

    This repository-level test intentionally avoids importing the full LiteLLM
    package, whose optional runtime dependencies are not installed in the
    deployment-script test environment. The method under test is still the real
    production implementation; only unrelated base classes and response types
    are replaced while the module is loaded.
    """

    class AudioTranscriptionRequestData:
        """Expose transformed multipart data in the same shape used by LiteLLM."""

        def __init__(self, data: dict) -> None:
            self.data = data

    class TranscriptionResponse:
        """Provide the response metadata referenced by an uncalled method."""

        model_fields: dict = {}

    modules: dict[str, types.ModuleType] = {}
    httpx = types.ModuleType("httpx")
    httpx.Headers = dict
    httpx.Response = object
    modules[httpx.__name__] = httpx
    for package_name in (
        "litellm",
        "litellm.llms",
        "litellm.llms.base_llm",
        "litellm.llms.base_llm.audio_transcription",
        "litellm.llms.base_llm.chat",
        "litellm.llms.openai",
        "litellm.llms.openai.transcriptions",
        "litellm.llms.openrouter",
        "litellm.secret_managers",
        "litellm.types",
        "litellm.types.llms",
    ):
        package = types.ModuleType(package_name)
        package.__path__ = []
        modules[package_name] = package

    transcription_base = types.ModuleType(
        "litellm.llms.base_llm.audio_transcription.transformation"
    )
    transcription_base.AudioTranscriptionRequestData = AudioTranscriptionRequestData
    transcription_base.BaseAudioTranscriptionConfig = object
    modules[transcription_base.__name__] = transcription_base

    chat_base = types.ModuleType("litellm.llms.base_llm.chat.transformation")
    chat_base.BaseLLMException = Exception
    modules[chat_base.__name__] = chat_base

    catalog = types.ModuleType("litellm.llms.openrouter.audio_catalog")
    catalog.is_openrouter_api_base = subject.is_openrouter_api_base
    modules[catalog.__name__] = catalog

    secrets = types.ModuleType("litellm.secret_managers.main")
    secrets.get_secret_str = lambda _name: None
    modules[secrets.__name__] = secrets

    openai_types = types.ModuleType("litellm.types.llms.openai")
    openai_types.AllMessageValues = dict
    openai_types.OpenAIAudioTranscriptionOptionalParams = str
    modules[openai_types.__name__] = openai_types

    utility_types = types.ModuleType("litellm.types.utils")
    utility_types.FileTypes = object
    utility_types.TranscriptionResponse = TranscriptionResponse
    modules[utility_types.__name__] = utility_types

    common_utils = types.ModuleType("litellm.llms.openai.common_utils")
    common_utils.OpenAIError = Exception
    modules[common_utils.__name__] = common_utils

    module_path = (
        pathlib.Path(__file__).parents[1]
        / "litellm/llms/openai/transcriptions/whisper_transformation.py"
    )
    module_name = (
        "litellm.llms.openai.transcriptions.whisper_transformation_test_subject"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    transformation = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(transformation)
    return transformation.OpenAIWhisperAudioTranscriptionConfig


class OpenRouterAudioGatewayTest(unittest.TestCase):
    """Protect capability filtering, cache refresh behavior, and URL rejection."""

    def setUp(self) -> None:
        """Clear process-local catalog state so every check is deterministic."""
        subject._catalog_cache.clear()
        subject._negative_cache.clear()
        subject._last_miss_refresh.clear()

    def test_accepts_only_exact_model_with_requested_modality(self) -> None:
        """A TTS model cannot be reused through the transcription route."""
        with patch.object(
            subject,
            "_fetch_catalog",
            side_effect=[
                frozenset({"x-ai/grok-stt-1.0"}),
                frozenset({"x-ai/grok-voice-tts-1.0"}),
                frozenset({"x-ai/grok-voice-tts-1.0"}),
            ],
        ) as fetch:
            subject.validate_audio_model("x-ai/grok-stt-1.0", "transcription")
            with self.assertRaisesRegex(ValueError, "does not support speech"):
                subject.validate_audio_model("x-ai/grok-stt-1.0", "speech")
        self.assertEqual(fetch.call_count, 3)

    def test_refreshes_a_cached_miss_for_new_models(self) -> None:
        """A catalog miss refreshes without waiting for the normal cache expiry."""
        with patch.object(
            subject,
            "_fetch_catalog",
            side_effect=[
                frozenset({"openai/whisper-1"}),
                frozenset({"openai/whisper-1", "x-ai/grok-stt-1.0"}),
            ],
        ):
            subject.validate_audio_model("openai/whisper-1", "transcription")
            subject.validate_audio_model("x-ai/grok-stt-1.0", "transcription")

    def test_refreshes_the_first_miss_during_process_startup(self) -> None:
        """A process younger than the throttle window still refreshes its first catalog miss."""
        with (
            patch.object(
                subject,
                "_fetch_catalog",
                side_effect=[
                    frozenset({"openai/whisper-1"}),
                    frozenset({"openai/whisper-1", "x-ai/grok-stt-1.0"}),
                ],
            ) as fetch,
            patch.object(subject.time, "monotonic", return_value=0.25),
        ):
            subject.validate_audio_model("openai/whisper-1", "transcription")
            subject.validate_audio_model("x-ai/grok-stt-1.0", "transcription")
        self.assertEqual(fetch.call_count, 2)

    def test_rejects_endpoint_shaped_model_ids(self) -> None:
        """A caller cannot smuggle an alternate provider URL through the model field."""
        with self.assertRaisesRegex(ValueError, "Invalid"):
            subject.validate_audio_model("https://other.example/model", "speech")

    def test_rejects_caller_credentials_and_routes(self) -> None:
        """Only the Tokyo gateway may choose the upstream endpoint and credential."""
        with self.assertRaisesRegex(ValueError, "api_base, api_key"):
            subject.reject_caller_routing_fields(
                {"api_key": "caller-key", "api_base": "https://other.example/v1"}
            )

    def test_identifies_only_managed_audio_aliases(self) -> None:
        """The guard applies to dynamic audio routes and the corrected Grok alias."""
        self.assertTrue(subject.is_managed_audio_alias("openrouter-tts/x/model"))
        self.assertTrue(subject.is_managed_audio_alias("openrouter-stt/x/model"))
        self.assertTrue(subject.is_managed_audio_alias("grok-stt"))
        self.assertFalse(subject.is_managed_audio_alias("openrouter/x/model"))

    def test_repeated_invalid_model_uses_negative_cache(self) -> None:
        """Invalid IDs cannot force an upstream catalog refresh on every request."""
        with patch.object(
            subject, "_fetch_catalog", return_value=frozenset({"valid/model"})
        ) as fetch:
            for _ in range(2):
                with self.assertRaises(subject.OpenRouterAudioModelError):
                    subject.validate_audio_model("invalid/model", "speech")
        self.assertEqual(fetch.call_count, 2)

    def test_managed_request_rejects_override_before_catalog_lookup(self) -> None:
        """The real pre-route helper rejects caller routing fields without fetching."""
        with patch.object(subject, "_fetch_catalog") as fetch:
            with self.assertRaises(subject.OpenRouterAudioModelError):
                asyncio.run(
                    subject.validate_managed_audio_request(
                        {
                            "model": "openrouter-stt/x-ai/grok-stt-1.0",
                            "api_base": "https://other.example/v1",
                        }
                    )
                )
        fetch.assert_not_called()

    def test_resolves_audio_content_types(self) -> None:
        """PCM/WAV/MP3 bytes retain a matching MIME type."""
        self.assertEqual(subject.resolve_audio_media_type(None, "pcm"), "audio/pcm")
        self.assertEqual(subject.resolve_audio_media_type(None, "wav"), "audio/wav")
        self.assertEqual(subject.resolve_audio_media_type(None, "mp3"), "audio/mpeg")
        self.assertEqual(
            subject.resolve_audio_media_type("audio/custom", "mp3"), "audio/custom"
        )

    def test_proxy_calls_pre_route_guard_for_both_audio_endpoints(self) -> None:
        """Both public handlers must run the managed request guard before routing."""
        proxy = (
            pathlib.Path(__file__).parents[1] / "litellm/proxy/proxy_server.py"
        ).read_text()
        speech = proxy.split("async def audio_speech(", 1)[1].split(
            '"/v1/audio/transcriptions"', 1
        )[0]
        transcription = proxy.split("async def audio_transcriptions(", 1)[1].split(
            "#                 Vertex AI Live API", 1
        )[0]
        self.assertIn("await validate_managed_audio_request(data)", speech)
        self.assertIn("await validate_managed_audio_request(data)", transcription)
        self.assertEqual(proxy.count("await validate_managed_audio_request(data)"), 2)
        self.assertGreaterEqual(proxy.count("except OpenRouterAudioModelError"), 2)
        self.assertGreaterEqual(proxy.count("except OpenRouterAudioCatalogError"), 2)

    def test_audio_routes_use_existing_managed_credentials_in_both_environments(self) -> None:
        """Public audio families are replayable; the legacy short alias stays TEST-only."""
        root = pathlib.Path(__file__).parents[1]
        test_config = (root / "config/litellm.test.yaml").read_text()
        prod_config = (root / "config/litellm.prod.yaml").read_text()
        for alias in ("openrouter-tts/*", "openrouter-stt/*", "grok-stt"):
            self.assertIn(f"model_name: {alias}", test_config)
        for alias in ("openrouter-tts/*", "openrouter-stt/*"):
            self.assertIn(f"model_name: {alias}", prod_config)
        self.assertNotIn("model_name: grok-stt", prod_config)
        self.assertGreaterEqual(
            prod_config.count("api_base: https://openrouter.ai/api/v1"), 2
        )
        self.assertGreaterEqual(
            test_config.count("api_base: https://openrouter.ai/api/v1"), 3
        )
        self.assertGreaterEqual(
            test_config.count("litellm_credential_name: openrouter-toyo"), 3
        )

    def test_transcription_passes_resolved_api_base_to_transformer(self) -> None:
        """The entrypoint must tell the transformer which upstream it resolved."""
        root = pathlib.Path(__file__).parents[1]
        main = (root / "litellm/main.py").read_text()
        self.assertIn(
            'litellm_params={**litellm_params_dict, "api_base": api_base}',
            main,
        )

    def test_openrouter_transcription_response_format_behavior(self) -> None:
        """OpenRouter keeps portable formats while other providers keep metadata."""
        transformer = _load_whisper_transformation()()
        audio_file = object()

        for api_base in (
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/",
        ):
            with self.subTest(api_base=api_base, response_format="missing"):
                result = transformer.transform_audio_transcription_request(
                    model="provider/model",
                    audio_file=audio_file,
                    optional_params={},
                    litellm_params={"api_base": api_base},
                )
                self.assertEqual(result.data["response_format"], "json")

            for response_format in ("text", "json", "verbose_json"):
                with self.subTest(api_base=api_base, response_format=response_format):
                    result = transformer.transform_audio_transcription_request(
                        model="provider/model",
                        audio_file=audio_file,
                        optional_params={"response_format": response_format},
                        litellm_params={"api_base": api_base},
                    )
                    self.assertEqual(result.data["response_format"], response_format)

        for api_base in (
            "https://api.openai.com/v1",
            "https://other.example/v1",
            "https://openrouter.ai/api/v1.evil.example",
        ):
            for response_format in (None, "text", "json"):
                with self.subTest(api_base=api_base, response_format=response_format):
                    optional_params = (
                        {}
                        if response_format is None
                        else {"response_format": response_format}
                    )
                    result = transformer.transform_audio_transcription_request(
                        model="provider/model",
                        audio_file=audio_file,
                        optional_params=optional_params,
                        litellm_params={"api_base": api_base},
                    )
                    self.assertEqual(result.data["response_format"], "verbose_json")


if __name__ == "__main__":
    unittest.main()
