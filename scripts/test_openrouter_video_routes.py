"""Local assertions for the three managed video aliases and OpenRouter converter.

This script checks gateway configuration and provider conversion without
starting a proxy or sending a paid request.
"""

from __future__ import annotations

import unittest
import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROUTES = {
    "seedance-2.5": "bytedance/seedance-2.5",
    "hailuo-3": "minimax/hailuo-3",
    "grok-imagine-video": "x-ai/grok-imagine-video",
}


class ManagedOpenRouterVideoRoutesTest(unittest.TestCase):
    """Check aliases, request controls, and terminal usage at the converter."""

    def test_aliases_in_both_environment_configs(self) -> None:
        """Both gateway environments expose the same three video aliases."""

        for environment in ("test", "prod"):
            with self.subTest(environment=environment):
                config = yaml.safe_load((ROOT / f"config/litellm.{environment}.yaml").read_text())
                models = {row["model_name"]: row for row in config["model_list"]}
                # TEST prepares Grok on Wokey; Seedance/Kling and all PROD media stay Kie.
                expected = {"seedance-2.5": "bytedance/seedance-2-5", "hailuo-3": "kling-3.0/video", "grok-imagine-video": "grok-imagine-video-1-5-preview"}
                for alias, upstream in expected.items():
                    route = models[alias]
                    expected_model = "wokey/grok-imagine-video-1.5" if environment == "test" and alias == "grok-imagine-video" else f"kie/{upstream}"
                    self.assertEqual(route["litellm_params"]["model"], expected_model)
                    self.assertEqual(route["model_info"]["mode"], "video_generation")

    def test_new_controls_and_terminal_usage(self) -> None:
        """Edit references and reported cost survive conversion for each route."""

        try:
            from litellm.llms.openrouter.videos.transformation import OpenRouterVideoConfig
        except ModuleNotFoundError as exc:
            self.skipTest(f"LiteLLM runtime dependency unavailable: {exc.name}")
        converter = OpenRouterVideoConfig()
        controls = {
            "seconds": 5,
            "frame_images": [{"type": "image_url", "frame_type": "first_frame"}],
            "input_references": [{"type": "video_url", "video_url": {"url": "https://media.example/v.mp4"}}],
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "generate_audio": False,
            "seed": 7,
            "provider": {"watermark": False},
        }
        for upstream in ROUTES.values():
            with self.subTest(model=upstream):
                mapped = converter.map_openai_params(controls, upstream, False)
                body, _, _ = converter.transform_video_create_request(
                    upstream, "scene", "https://openrouter.ai/api/v1", mapped, {}, {}
                )
                self.assertEqual(body["duration"], 5)
                for key in controls.keys() - {"seconds"}:
                    self.assertEqual(body[key], controls[key])
                video = converter._video_object(
                    payload={"id": "task-1", "status": "completed", "usage": {"output_tokens": 3}, "cost": 0.25},
                    model=upstream,
                    custom_llm_provider=None,
                    request_data=None,
                )
                self.assertEqual(video.usage, {"output_tokens": 3, "cost": 0.25})

    def test_seedance_two_keeps_narrow_controls(self) -> None:
        """The existing Seedance 2 route does not gain new provider controls."""

        try:
            from litellm.llms.openrouter.videos.transformation import OpenRouterVideoConfig
        except ModuleNotFoundError as exc:
            self.skipTest(f"LiteLLM runtime dependency unavailable: {exc.name}")
        mapped = OpenRouterVideoConfig().map_openai_params(
            {"seconds": 5, "resolution": "720p"}, "bytedance/seedance-2.0-fast", False
        )
        self.assertEqual(mapped, {"seconds": 5})

    def test_converter_source_parses_and_carries_usage(self) -> None:
        """A dependency-free assertion still catches omitted terminal usage."""

        source = (ROOT / "litellm/llms/openrouter/videos/transformation.py").read_text()
        tree = ast.parse(source)
        methods = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OpenRouterVideoConfig").body
        video_object = next(node for node in methods if isinstance(node, ast.FunctionDef) and node.name == "_video_object")
        literals = {node.value for node in ast.walk(video_object) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertIn("usage", literals)
        self.assertIn("cost", literals)


if __name__ == "__main__":
    unittest.main()
