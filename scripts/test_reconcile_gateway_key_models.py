#!/usr/bin/env python3
"""Dependency-free checks for replayable gateway-key model grants."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import reconcile_gateway_key_models as subject


class GatewayKeyModelReconciliationTest(unittest.TestCase):
    """Protect exact key ownership, preservation, and idempotent updates."""

    def test_adds_only_missing_models_and_verifies_readback(self) -> None:
        """Existing grants remain ordered while managed routes are added once."""
        managed = list(subject.managed_models("test"))
        responses = [
            {
                "keys": [
                    {
                        "key_alias": subject.KEY_ALIASES["test"],
                        "token": "hashed-key-id",
                        "models": ["kimi-k2.7"],
                    }
                ]
            },
            {"updated": True},
            {
                "info": {"models": ["kimi-k2.7", *managed]}
            },
        ]
        with patch.object(subject, "_request_json", side_effect=responses) as request:
            desired = subject.reconcile("http://proxy", "master", "test")
        self.assertEqual(
            desired,
            ["kimi-k2.7", *managed],
        )
        self.assertEqual(
            request.call_args_list[1].args[3],
            {"key": "hashed-key-id", "models": desired},
        )

    def test_skips_update_when_grant_already_exists(self) -> None:
        """A repeated deployment performs only list and readback calls."""
        managed = list(subject.managed_models("test"))
        responses = [
            {
                "keys": [
                    {
                        "key_alias": subject.KEY_ALIASES["test"],
                        "token": "hashed-key-id",
                        "models": managed,
                    }
                ]
            },
            {
                "info": {"models": managed}
            },
        ]
        with patch.object(subject, "_request_json", side_effect=responses) as request:
            desired = subject.reconcile("http://proxy", "master", "test")
        self.assertEqual(desired, managed)
        self.assertEqual(request.call_count, 2)

    def test_preserves_empty_unrestricted_allowlist(self) -> None:
        """LiteLLM's empty-list all-model grant must never be narrowed."""
        responses = [
            {
                "keys": [
                    {
                        "key_alias": subject.KEY_ALIASES["test"],
                        "token": "hashed-key-id",
                        "models": [],
                    }
                ]
            },
            {"info": {"models": []}},
        ]
        with patch.object(subject, "_request_json", side_effect=responses) as request:
            desired = subject.reconcile("http://proxy", "master", "test")
        self.assertEqual(desired, [])
        self.assertEqual(request.call_count, 2)

    def test_shared_seedance_alias_reaches_both_gateway_keys(self) -> None:
        """The managed Seedance route stays allowed in TEST and production."""
        for alias in ("seedance-2-mini", "grok-imagine-image", "gemini-3.1-flash-image"):
            self.assertIn(alias, subject.managed_models("test"))
            self.assertIn(alias, subject.REQUIRED_MODELS["prod"])
        self.assertIn("tripo-3d", subject.managed_models("test"))
        self.assertIn("tripo-3d", subject.REQUIRED_MODELS["prod"])
        self.assertIn("minimax-music", subject.managed_models("test"))
        self.assertIn("minimax-music", subject.REQUIRED_MODELS["prod"])
        self.assertIn("kling-v3", subject.managed_models("test"))
        self.assertIn("kling-v3", subject.REQUIRED_MODELS["prod"])
        self.assertIn("seedance-2", subject.managed_models("test"))
        self.assertEqual(subject.REQUIRED_MODELS["prod"], subject.REQUIRED_MODELS["test"])
        responses = [
            {
                "keys": [
                    {
                        "key_alias": subject.KEY_ALIASES["prod"],
                        "token": "hashed-key-id",
                        "models": ["openrouter/*"],
                    }
                ]
            },
            {"updated": True},
            {"info": {"models": list(subject.managed_models("prod"))}},
        ]
        with patch.object(subject, "_request_json", side_effect=responses) as request:
            desired = subject.reconcile("http://proxy", "master", "prod")
        self.assertEqual(desired, list(subject.managed_models("prod")))
        self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
