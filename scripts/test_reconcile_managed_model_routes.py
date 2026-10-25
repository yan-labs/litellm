"""Unit tests for Tokyo LiteLLM route and DeepSeek price reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SCRIPT_PATH = Path(__file__).with_name("reconcile_managed_model_routes.py")
REPOSITORY_ROOT = SCRIPT_PATH.parents[1]
SPEC = importlib.util.spec_from_file_location("reconcile_managed_model_routes", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeModelTable:
    """Model the narrow async Prisma table contract used by route provisioning."""

    def __init__(self, rows: list[dict], *, fail_create_after_insert: bool = False):
        self.rows = rows
        self.fail_create_after_insert = fail_create_after_insert
        self.create_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def find_many(self, *, where: dict) -> list[dict]:
        """Return rows matching the deployment alias from the in-memory table."""
        return [row for row in self.rows if row.get("model_name") == where["model_name"]]

    async def create(self, *, data: dict) -> dict:
        """Insert one row and optionally emulate a concurrent unique-key loser."""
        self.create_calls.append(data)
        row = dict(data)
        self.rows.append(row)
        if self.fail_create_after_insert:
            raise RuntimeError("duplicate key")
        return row

    async def delete(self, *, where: dict) -> None:
        """Delete only the exact model id requested by compensation."""
        self.delete_calls.append(where)
        self.rows = [row for row in self.rows if row.get("model_id") != where["model_id"]]


class FakePrisma:
    """Expose the generated Prisma attribute name without a real database."""

    def __init__(self, table: FakeModelTable):
        self.litellm_proxymodeltable = table


def db_row(
    alias: str,
    route_id: str,
    *,
    binding: str = "credential-ciphertext",
    owner: str = MODULE.VISION_ROUTE_OWNER,
    owner_token: str | None = None,
) -> dict:
    """Build one encrypted DB row for provisioning tests."""
    return {
        "model_id": route_id,
        "model_name": alias,
        "litellm_params": {"api_key": binding},
        "model_info": {
            **(
                {"managed_route_owner_token": owner_token}
                if owner_token is not None
                else {}
            )
        },
        "created_by": owner,
        "updated_by": owner,
    }


def live_vision_route(route_id: str) -> dict:
    """Build the exact live Router row required after Admin-API reload."""
    return {
        "model_name": MODULE.VISION_ALIAS,
        "litellm_params": {
            "model": MODULE.COMMON_DEEPSEEK_ROUTES[MODULE.VISION_ALIAS]["model"],
            "api_base": "https://api.deepseek.com/anthropic",
        },
        "model_info": {"id": route_id, "db_model": True},
    }


def route(
    alias: str,
    model: str,
    route_id: str,
    *,
    database: bool,
    model_info: dict | None = None,
) -> dict:
    """Build the minimal `/model/info` row used by reconciliation tests."""
    return {
        "model_name": alias,
        "litellm_params": {"model": model},
        "model_info": {"id": route_id, "db_model": database, **(model_info or {})},
    }


class ReconcileManagedModelRoutesTest(unittest.TestCase):
    """Protect mutation scope, credential preservation, and current public prices."""

    def test_seedance_alias_is_reconciled_as_yaml_owned(self) -> None:
        """The restored managed video alias cannot compete with a stale DB route."""
        self.assertEqual(
            MODULE.YAML_MANAGED_ROUTES["seedance-2"],
            "kie/bytedance/seedance-2-fast",
        )

    def test_reconciles_yaml_and_deepseek_without_replacing_credentials(self) -> None:
        """Delete YAML fallbacks and apply price-only PATCHes to DeepSeek DB routes."""
        yaml_rows = [
            route(alias, model, f"yaml-{index}", database=False)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("test").items())
        ]
        yaml_database_rows = [
            route(alias, model, f"db-{index}", database=True)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("test").items())
        ]
        deepseek_routes = MODULE.deepseek_routes_for_environment("test")
        deepseek_before = [
            route(alias, config["model"], f"deepseek-{index}", database=True)
            for index, (alias, config) in enumerate(deepseek_routes.items())
        ]
        deepseek_after = [
            route(
                alias,
                config["model"],
                f"deepseek-{index}",
                database=True,
                model_info={key: value for key, value in config.items() if key != "model"},
            )
            for index, (alias, config) in enumerate(deepseek_routes.items())
        ]
        unrelated = [
            route("unrelated-fable", "openrouter/anthropic/claude-fable-5.1", "db-fable", database=True),
            route("kimi-k3", "dashscope/kimi/kimi-k3", "db-kimi-k3", database=True),
            # A mismatched retired route must neither block nor receive PATCHes.
            route(MODULE.VISION_ALIAS, "historical/vision", "retired-vision", database=True),
        ]
        healthy = {"healthy_count": 1, "unhealthy_count": 0}
        responses = [
            {"data": yaml_rows + yaml_database_rows + deepseek_before + unrelated},
            *[healthy for _ in MODULE.yaml_managed_routes_for_environment("test", health_only=True)],
            *[item for _ in deepseek_routes for item in (healthy, {})],
            *[{} for _ in MODULE.yaml_managed_routes_for_environment("test")],
            {"data": yaml_rows + deepseek_after + unrelated},
        ]

        with patch.object(MODULE, "_request_json", side_effect=responses) as request_json:
            MODULE.reconcile("http://proxy", "secret", "test")

        delete_payloads = [
            call.args[3]
            for call in request_json.call_args_list
            if call.args[2] == "/model/delete"
        ]
        self.assertEqual(
            delete_payloads,
            [{"id": f"db-{index}"} for index, _ in enumerate(MODULE.yaml_managed_routes_for_environment("test"))],
        )
        patch_calls = [
            call for call in request_json.call_args_list if call.args[2].endswith("/update")
        ]
        self.assertEqual(len(patch_calls), len(deepseek_routes))
        for call, (_, config) in zip(patch_calls, deepseek_routes.items()):
            self.assertEqual(
                call.args[3],
                {"model_info": {key: value for key, value in config.items() if key != "model"}},
            )
            self.assertEqual(call.kwargs["method"], "PATCH")

    def test_known_upstream_drift_alias_warns_instead_of_raising(self) -> None:
        """A documented provider-model drift must not abort the whole reconcile step."""
        drifted_alias = "deepseek-v4-flash"
        self.assertIn(drifted_alias, MODULE.KNOWN_UPSTREAM_DRIFT_ALIASES)
        drifted_model = "dashscope/deepseek-v4-flash-0731"

        yaml_rows = [
            route(alias, model, f"yaml-{index}", database=False)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("test").items())
        ]
        yaml_database_rows = [
            route(alias, model, f"db-{index}", database=True)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("test").items())
        ]
        deepseek_routes = MODULE.deepseek_routes_for_environment("test")
        non_drifted_aliases = [
            alias for alias in deepseek_routes if alias != drifted_alias
        ]
        deepseek_before = [
            route(
                alias,
                drifted_model if alias == drifted_alias else config["model"],
                f"deepseek-{index}",
                database=True,
            )
            for index, (alias, config) in enumerate(deepseek_routes.items())
        ]
        # Only the non-drifted aliases pick up the patched price fields; the
        # drifted alias's route is untouched (still on the drifted model).
        deepseek_after = [
            route(
                alias,
                drifted_model if alias == drifted_alias else config["model"],
                f"deepseek-{index}",
                database=True,
                model_info=(
                    {}
                    if alias == drifted_alias
                    else {key: value for key, value in config.items() if key != "model"}
                ),
            )
            for index, (alias, config) in enumerate(deepseek_routes.items())
        ]
        healthy = {"healthy_count": 1, "unhealthy_count": 0}
        responses = [
            {"data": yaml_rows + yaml_database_rows + deepseek_before},
            *[healthy for _ in MODULE.yaml_managed_routes_for_environment("test", health_only=True)],
            # Only the non-drifted aliases get a targeted health check + PATCH.
            *[item for _ in non_drifted_aliases for item in (healthy, {})],
            *[{} for _ in MODULE.yaml_managed_routes_for_environment("test")],
            {"data": yaml_rows + deepseek_after},
        ]

        stdout = io.StringIO()
        with patch.object(MODULE, "_request_json", side_effect=responses) as request_json:
            with contextlib.redirect_stdout(stdout):
                MODULE.reconcile("http://proxy", "secret", "test")

        # The drifted alias must never be PATCHed; every other alias still is.
        patch_calls = [
            call for call in request_json.call_args_list if call.args[2].endswith("/update")
        ]
        self.assertEqual(len(patch_calls), len(non_drifted_aliases))

        # YAML-managed routes (e.g. seedance-2) must still be deleted/converged
        # even though one unrelated DeepSeek alias is stuck on a drifted model.
        delete_payloads = [
            call.args[3]
            for call in request_json.call_args_list
            if call.args[2] == "/model/delete"
        ]
        self.assertEqual(
            delete_payloads,
            [{"id": f"db-{index}"} for index, _ in enumerate(MODULE.yaml_managed_routes_for_environment("test"))],
        )

        output = stdout.getvalue()
        self.assertIn(f"WARNING: {drifted_alias}:", output)
        self.assertIn("known upstream drift", output)

    def test_all_known_drift_aliases_can_warn_simultaneously(self) -> None:
        """Multiple aliases on the drift allowlist must each warn independently.

        Regression guard for the real failure observed in run 34370307847:
        the first fix only allowlisted deepseek-v4-flash, so the same deploy
        still hard-failed on deepseek-v4-pro's identical DashScope drift.
        """
        drifted_models = {
            "deepseek-v4-flash": "dashscope/deepseek-v4-flash-0731",
            "deepseek-v4-pro": "dashscope/deepseek-v4-pro-0813",
        }
        self.assertEqual(set(drifted_models), set(MODULE.KNOWN_UPSTREAM_DRIFT_ALIASES))

        yaml_rows = [
            route(alias, model, f"yaml-{index}", database=False)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("test").items())
        ]
        yaml_database_rows = [
            route(alias, model, f"db-{index}", database=True)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("test").items())
        ]
        deepseek_routes = MODULE.deepseek_routes_for_environment("test")
        non_drifted_aliases = [
            alias for alias in deepseek_routes if alias not in drifted_models
        ]
        deepseek_before = [
            route(
                alias,
                drifted_models.get(alias, config["model"]),
                f"deepseek-{index}",
                database=True,
            )
            for index, (alias, config) in enumerate(deepseek_routes.items())
        ]
        deepseek_after = [
            route(
                alias,
                drifted_models.get(alias, config["model"]),
                f"deepseek-{index}",
                database=True,
                model_info=(
                    {}
                    if alias in drifted_models
                    else {key: value for key, value in config.items() if key != "model"}
                ),
            )
            for index, (alias, config) in enumerate(deepseek_routes.items())
        ]
        healthy = {"healthy_count": 1, "unhealthy_count": 0}
        responses = [
            {"data": yaml_rows + yaml_database_rows + deepseek_before},
            *[healthy for _ in MODULE.yaml_managed_routes_for_environment("test", health_only=True)],
            *[item for _ in non_drifted_aliases for item in (healthy, {})],
            *[{} for _ in MODULE.yaml_managed_routes_for_environment("test")],
            {"data": yaml_rows + deepseek_after},
        ]

        stdout = io.StringIO()
        with patch.object(MODULE, "_request_json", side_effect=responses) as request_json:
            with contextlib.redirect_stdout(stdout):
                MODULE.reconcile("http://proxy", "secret", "test")

        patch_calls = [
            call for call in request_json.call_args_list if call.args[2].endswith("/update")
        ]
        self.assertEqual(len(patch_calls), len(non_drifted_aliases))

        output = stdout.getvalue()
        for alias in drifted_models:
            self.assertIn(f"WARNING: {alias}:", output)

    def test_refuses_mutation_when_expected_yaml_route_is_missing(self) -> None:
        """A config-load failure cannot delete the last database-backed route."""
        rows = [
            route(alias, model, f"db-{index}", database=True)
            for index, (alias, model) in enumerate(MODULE.YAML_MANAGED_ROUTES.items())
        ]
        with patch.object(MODULE, "_request_json", return_value={"data": rows}) as request_json:
            with self.assertRaisesRegex(RuntimeError, "expected exactly one YAML route"):
                MODULE.reconcile("http://proxy", "secret")
        self.assertEqual(request_json.call_count, 1)

    def test_refuses_mutation_when_targeted_route_health_fails(self) -> None:
        """An unusable YAML route cannot replace a working database fallback."""
        yaml_rows = [
            route(alias, model, f"yaml-{index}", database=False)
            for index, (alias, model) in enumerate(MODULE.yaml_managed_routes_for_environment("prod").items())
        ]
        deepseek_rows = [
            route(alias, config["model"], f"deepseek-{index}", database=True)
            for index, (alias, config) in enumerate(
                MODULE.deepseek_routes_for_environment("prod").items()
            )
        ]
        responses = [
            {"data": yaml_rows + deepseek_rows},
            {"healthy_count": 0, "unhealthy_count": 1},
        ]
        with patch.object(MODULE, "_request_json", side_effect=responses) as request_json:
            with self.assertRaisesRegex(RuntimeError, "targeted route health failed"):
                MODULE.reconcile("http://proxy", "secret")
        self.assertFalse(
            any(
                call.args[2] == "/model/delete" or call.args[2].endswith("/update")
                for call in request_json.call_args_list
            )
        )

    def test_retired_vision_is_not_owned_by_normal_deployment(self) -> None:
        """Retired Vision rows are excluded from deployment mutations in both environments."""
        self.assertNotIn(
            "deepseek-v4-flash-vision-exp", MODULE.deepseek_routes_for_environment("test")
        )
        self.assertNotIn(
            "deepseek-v4-flash-vision-exp", MODULE.deepseek_routes_for_environment("prod")
        )

    def test_json_object_normalizer_rejects_non_objects(self) -> None:
        """Credential cloning accepts Prisma JSON strings but rejects ambiguous shapes."""
        self.assertEqual(
            MODULE._as_json_object('{"api_key":"ciphertext"}', field="params"),
            {"api_key": "ciphertext"},
        )
        with self.assertRaisesRegex(RuntimeError, "expected JSON object"):
            MODULE._as_json_object([], field="params")

    def test_vision_route_preserves_only_the_opaque_source_credential(self) -> None:
        """Route creation copies ciphertext and encrypts every new provider parameter."""
        source = {"api_key": "ciphertext", "model": "old-model", "api_base": "old-base"}

        def encrypt_value(*, value: object) -> str:
            return f"encrypted:{value!r}"

        result = MODULE._build_vision_params(source, encrypt_value)

        self.assertEqual(result["api_key"], "ciphertext")
        self.assertEqual(
            result["model"], "encrypted:'anthropic/deepseek-v4-flash-vision-exp'"
        )
        self.assertEqual(
            result["api_base"], "encrypted:'https://api.deepseek.com/anthropic'"
        )
        self.assertIsNot(result, source)

    def test_vision_route_refuses_a_source_without_managed_credentials(self) -> None:
        """A malformed Flash row cannot create an unauthenticated production route."""
        with self.assertRaisesRegex(RuntimeError, "credential binding is missing"):
            MODULE._build_vision_params({"model": "ciphertext"}, lambda **_: "value")

    def test_provision_creates_reloads_and_reads_back_exact_route(self) -> None:
        """A first production promotion becomes live before reconciliation continues."""
        source = db_row("deepseek-v4-flash", "flash-id")
        table = FakeModelTable([source])
        route_id = MODULE._stable_vision_route_id()
        responses = [{}, {"data": [live_vision_route(route_id)]}]

        with patch.object(MODULE, "_request_json", side_effect=responses) as request_json:
            observed = asyncio.run(
                MODULE._provision_production_vision_route(
                    FakePrisma(table),
                    lambda *, value: f"encrypted:{value!r}",
                    "http://proxy",
                    "secret",
                )
            )

        self.assertEqual(observed[:2], (route_id, True))
        self.assertIsInstance(observed[2], str)
        self.assertEqual(len(table.create_calls), 1)
        created_params = MODULE._as_json_object(
            table.create_calls[0]["litellm_params"], field="created.params"
        )
        self.assertEqual(created_params["api_key"], "credential-ciphertext")
        self.assertEqual(request_json.call_args_list[0].args[2], f"/model/{route_id}/update")
        self.assertEqual(request_json.call_args_list[1].args[2], "/model/info")

    def test_provision_existing_owned_route_is_idempotent_but_reloads(self) -> None:
        """A repeated deploy validates ownership and refreshes Router without another insert."""
        source = db_row("deepseek-v4-flash", "flash-id")
        target = db_row(MODULE.VISION_ALIAS, "vision-existing")
        table = FakeModelTable([source, target])
        responses = [{}, {"data": [live_vision_route("vision-existing")]}]

        with patch.object(MODULE, "_request_json", side_effect=responses) as request_json:
            observed = asyncio.run(
                MODULE._provision_production_vision_route(
                    FakePrisma(table), lambda **_: "unused", "http://proxy", "secret"
                )
            )

        self.assertEqual(observed, ("vision-existing", False, None))
        self.assertEqual(table.create_calls, [])
        self.assertEqual(request_json.call_args_list[0].args[2], "/model/vision-existing/update")

    def test_provision_marks_ambiguous_post_commit_as_created_by_this_run(self) -> None:
        """A per-run marker keeps post-commit unknown outcomes eligible for compensation."""
        source = db_row("deepseek-v4-flash", "flash-id")
        table = FakeModelTable([source], fail_create_after_insert=True)
        route_id = MODULE._stable_vision_route_id()
        responses = [{}, {"data": [live_vision_route(route_id)]}]
        owner_token = "tokyo-release-reconciliation:test-run"

        with patch.object(MODULE, "_request_json", side_effect=responses):
            observed = asyncio.run(
                MODULE._provision_production_vision_route(
                    FakePrisma(table),
                    lambda *, value: f"encrypted:{value!r}",
                    "http://proxy",
                    "secret",
                    owner_token,
                )
            )

        self.assertEqual(observed, (route_id, True, owner_token))
        self.assertEqual(
            len([row for row in table.rows if row["model_name"] == MODULE.VISION_ALIAS]),
            1,
        )

    def test_provision_rejects_foreign_or_wrong_credential_route(self) -> None:
        """An administrator-owned or differently bound alias stays authoritative and untouched."""
        cases = (
            db_row(MODULE.VISION_ALIAS, "vision", owner="admin"),
            db_row(MODULE.VISION_ALIAS, "vision", binding="other-ciphertext"),
        )
        for target in cases:
            with self.subTest(target=target):
                table = FakeModelTable([db_row("deepseek-v4-flash", "flash-id"), target])
                with patch.object(MODULE, "_request_json") as request_json:
                    with self.assertRaisesRegex(
                        RuntimeError, "not deployment-owned|credential binding differs"
                    ):
                        asyncio.run(
                            MODULE._provision_production_vision_route(
                                FakePrisma(table),
                                lambda **_: "unused",
                                "http://proxy",
                                "secret",
                            )
                        )
                request_json.assert_not_called()

    def test_provision_failure_deletes_created_route_and_reloads(self) -> None:
        """A failed live reload compensates the DB row before the deploy can fail."""
        table = FakeModelTable([db_row("deepseek-v4-flash", "flash-id")])
        request_count = 0

        def request_json(_base: str, _key: str, path: str, *_args, **_kwargs):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                # The real Admin PATCH overwrites updated_by before clear_cache.
                target = next(
                    row for row in table.rows if row["model_name"] == MODULE.VISION_ALIAS
                )
                target["updated_by"] = "proxy-admin"
                raise RuntimeError("reload failed")
            if path == "/model/delete":
                return {}
            if path == "/model/flash-id/update":
                return {}
            if path == "/model/info":
                return {"data": []}
            raise AssertionError(path)

        with patch.object(MODULE, "_request_json", side_effect=request_json) as request_json_mock:
            with self.assertRaisesRegex(RuntimeError, "reload failed"):
                asyncio.run(
                    MODULE._provision_production_vision_route(
                        FakePrisma(table),
                        lambda *, value: f"encrypted:{value!r}",
                        "http://proxy",
                        "secret",
                    )
                )

        self.assertEqual(request_json_mock.call_args_list[1].args[2], "/model/delete")
        self.assertEqual(
            request_json_mock.call_args_list[2].args[2], "/model/flash-id/update"
        )
        self.assertEqual(request_json_mock.call_args_list[3].args[2], "/model/info")
        self.assertFalse(
            any(row["model_name"] == MODULE.VISION_ALIAS for row in table.rows)
        )

    def test_compensation_tolerates_admin_delete_committed_before_router_error(self) -> None:
        """An already-deleted DB row still forces a trusted reload and exact absence readback."""
        owner_token = "tokyo-release-reconciliation:test-run"
        table = FakeModelTable(
            [
                db_row("deepseek-v4-flash", "flash-id"),
                db_row(MODULE.VISION_ALIAS, "vision-id", owner_token=owner_token),
            ]
        )

        def request_json(_base: str, _key: str, path: str, *_args, **_kwargs):
            if path == "/model/delete":
                table.rows = [row for row in table.rows if row["model_id"] != "vision-id"]
                raise RuntimeError("Router delete failed after DB commit")
            if path == "/model/flash-id/update":
                return {}
            if path == "/model/info":
                return {"data": []}
            raise AssertionError(path)

        with patch.object(MODULE, "_request_json", side_effect=request_json):
            asyncio.run(
                MODULE._compensate_created_vision_route(
                    FakePrisma(table),
                    "http://proxy",
                    "secret",
                    "vision-id",
                    "flash-id",
                    owner_token,
                    {"api_key": "credential-ciphertext"},
                )
            )

    def test_compensation_retries_when_router_readback_remains_stale(self) -> None:
        """A successful DB delete cannot pass until a later Router readback is also empty."""
        owner_token = "tokyo-release-reconciliation:test-run"
        table = FakeModelTable(
            [
                db_row("deepseek-v4-flash", "flash-id"),
                db_row(MODULE.VISION_ALIAS, "vision-id", owner_token=owner_token),
            ]
        )
        model_info_reads = 0

        def request_json(_base: str, _key: str, path: str, *_args, **_kwargs):
            nonlocal model_info_reads
            if path == "/model/delete":
                table.rows = [row for row in table.rows if row["model_id"] != "vision-id"]
                return {}
            if path == "/model/flash-id/update":
                return {}
            if path == "/model/info":
                model_info_reads += 1
                return {
                    "data": [live_vision_route("vision-id")]
                    if model_info_reads == 1
                    else []
                }
            raise AssertionError(path)

        with patch.object(MODULE, "_request_json", side_effect=request_json):
            asyncio.run(
                MODULE._compensate_created_vision_route(
                    FakePrisma(table),
                    "http://proxy",
                    "secret",
                    "vision-id",
                    "flash-id",
                    owner_token,
                    {"api_key": "credential-ciphertext"},
                )
            )
        self.assertEqual(model_info_reads, 2)

    def test_compensation_refuses_route_changed_during_admin_delete(self) -> None:
        """A credential takeover during Admin I/O cannot reach direct Prisma deletion."""
        owner_token = "tokyo-release-reconciliation:test-run"
        table = FakeModelTable(
            [
                db_row("deepseek-v4-flash", "flash-id"),
                db_row(MODULE.VISION_ALIAS, "vision-id", owner_token=owner_token),
            ]
        )

        def request_json(_base: str, _key: str, path: str, *_args, **_kwargs):
            if path == "/model/delete":
                target = next(
                    row for row in table.rows if row["model_name"] == MODULE.VISION_ALIAS
                )
                target["litellm_params"] = {"api_key": "administrator-ciphertext"}
                raise RuntimeError("Admin request failed after concurrent takeover")
            raise AssertionError(path)

        with patch.object(MODULE, "_request_json", side_effect=request_json):
            with self.assertRaisesRegex(RuntimeError, "compensation failed"):
                asyncio.run(
                    MODULE._compensate_created_vision_route(
                        FakePrisma(table),
                        "http://proxy",
                        "secret",
                        "vision-id",
                        "flash-id",
                        owner_token,
                        {"api_key": "credential-ciphertext"},
                    )
                )
        self.assertEqual(table.delete_calls, [])

    def test_main_does_not_provision_retired_vision(self) -> None:
        """Both deployment environments reconcile without recreating retired routes."""
        for environment in ("test", "prod"):
            with (
                self.subTest(environment=environment),
                patch.object(MODULE, "_ensure_production_vision_route", AsyncMock()) as ensure,
                patch.object(MODULE, "_rollback_production_vision_route", AsyncMock()) as rollback,
                patch.object(MODULE, "reconcile") as reconcile,
                patch.dict(MODULE.os.environ, {"LITELLM_MASTER_KEY": "test-placeholder"}),
                patch.object(sys, "argv", ["reconcile", "--environment", environment]),
            ):
                self.assertEqual(MODULE.main(), 0)
                reconcile.assert_called_once_with("http://127.0.0.1:4000", "test-placeholder", environment)
                ensure.assert_not_awaited()
                rollback.assert_not_awaited()

    def test_public_deepseek_prices_match_current_rate_card(self) -> None:
        """Lock Flash, Pro, and inferred Vision prices to the reviewed public values."""
        routes = MODULE.deepseek_routes_for_environment("test")
        self.assertEqual(
            {key: value for key, value in routes["deepseek-v4-flash"].items() if key != "model"},
            {
                "input_cost_per_token": 0.00000014,
                "cache_read_input_token_cost": 0.0000000028,
                "cache_creation_input_token_cost": 0,
                "output_cost_per_token": 0.00000028,
            },
        )
        self.assertEqual(
            {key: value for key, value in routes["deepseek-v4-pro"].items() if key != "model"},
            {
                "input_cost_per_token": 0.000000435,
                "cache_read_input_token_cost": 0.000000003625,
                "cache_creation_input_token_cost": 0,
                "output_cost_per_token": 0.00000087,
            },
        )
        for field in (
            "input_cost_per_token",
            "cache_read_input_token_cost",
            "cache_creation_input_token_cost",
            "output_cost_per_token",
        ):
            self.assertEqual(
                MODULE.COMMON_DEEPSEEK_ROUTES["deepseek-v4-flash-vision-exp"][field],
                routes["deepseek-v4-flash"][field],
            )


class ConfigOwnershipTest(unittest.TestCase):
    """Keep database-owned provider credentials out of Git-backed YAML routes."""

    def test_configs_keep_glm_flash_on_the_managed_environment_route(self) -> None:
        """Both environments use private Aliyun GLM deployments without OpenRouter backup."""
        for relative_path in ("config/litellm.test.yaml", "config/litellm.prod.yaml"):
            with self.subTest(config=relative_path):
                source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                import yaml
                config = yaml.safe_load(source)
                spec = config["db_managed_routes"]["glm-5.3-flash"]
                self.assertEqual(spec["source_alias"], "deepseek-v4-flash")
                self.assertEqual(spec["model"], "dashscope/ZHIPU/GLM-5.3-Flash")
                self.assertFalse(any(row["model_name"] == "glm-5.3-flash" for row in config["model_list"]))
                self.assertFalse(any("glm-5.3-flash" in row for row in config["router_settings"]["fallbacks"]))

    def test_configs_do_not_recreate_database_owned_routes(self) -> None:
        """Config sync cannot replace Kimi or DeepSeek routes and their stored credentials."""
        for relative_path in ("config/litellm.test.yaml", "config/litellm.prod.yaml"):
            with self.subTest(config=relative_path):
                source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                model_names = re.findall(
                    r"^\s*-\s+model_name:\s*([^\s#]+)", source, flags=re.MULTILINE
                )
                upstream_models = re.findall(
                    r"^\s+model:\s*([^\s#]+)", source, flags=re.MULTILINE
                )
                self.assertNotIn("kimi-k3", model_names)
                self.assertNotIn("openrouter/moonshotai/kimi-k3", upstream_models)
                self.assertFalse(any(name.startswith("deepseek-v4-") for name in model_names))
                self.assertIn("store_model_in_db: true", source)


if __name__ == "__main__":
    unittest.main()
