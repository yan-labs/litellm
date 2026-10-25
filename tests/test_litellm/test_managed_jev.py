"""Mock-only managed JEV route checks; supplier requests never leave the process."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse
from litellm.proxy._types import ProxyException
from starlette.testclient import TestClient

from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.pass_through_endpoints import jev


@pytest.mark.parametrize("enabled,key,status", [(False, "offline", 404), (True, "", 503), (True, "offline", 200), (True, "offline", 429)])
def test_named_credential_passthrough(enabled, key, status):
    """TEST opt-in and named auth gate one POST; status and usage remain intact."""
    app = FastAPI()
    app.include_router(jev.router)
    app.dependency_overrides[user_api_key_auth] = lambda: SimpleNamespace()
    upstream = httpx.Response(status, json={"answers": {"guard": {"choice": "no", "confidence": 0.9}}, "usage": {"input_tokens": 4, "output_tokens": 1}})
    client = AsyncMock()
    client.post.return_value = upstream
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    with patch("litellm.proxy.proxy_server.proxy_config", SimpleNamespace(get_config_state=lambda: {"jev_passthrough": {"enabled": enabled}})), patch.object(jev.CredentialAccessor, "get_credential_values", return_value={"api_key": key}), patch.object(jev.httpx, "AsyncClient", return_value=manager):
        result = TestClient(app).post("/jev/v1/systemone", json={"model": "jev-latest", "state": {"request_excerpt": "普通旅行问题", "original_chars": 6}, "questions": {}}, headers={"Authorization": "Bearer gateway-offline"})
    assert result.status_code == status
    if enabled and key:
        assert client.post.await_count == 1
        assert client.post.call_args.kwargs["json"]["state"] == {"request_excerpt": "普通旅行问题", "original_chars": 6}
        assert client.post.call_args.args == ("https://api.typesafe.ai/v1/systemone",)
        assert client.post.call_args.kwargs["headers"] == {"Authorization": "Bearer offline"}
        assert result.json()["usage"] == {"input_tokens": 4, "output_tokens": 1}
    else:
        client.post.assert_not_awaited()


def test_route_reuses_gateway_auth():
    """Anonymous requests must fail the existing gateway auth before supplier access."""
    app = FastAPI()
    app.include_router(jev.router)
    app.add_exception_handler(ProxyException, lambda request, error: JSONResponse({"error": error.to_dict()}, status_code=int(error.code)))
    with patch("litellm.proxy.proxy_server.master_key", "gateway-offline"), patch.object(jev.CredentialAccessor, "get_credential_values") as credentials:
        result = TestClient(app).post("/jev/v1/systemone", json={"state": "travel", "questions": {}})
    assert result.status_code in (401, 403)
    credentials.assert_not_called()
