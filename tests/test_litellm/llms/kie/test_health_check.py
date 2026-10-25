"""Offline probes: every unexpected HTTP method or URL fails the test."""

from unittest.mock import MagicMock

import httpx
import pytest
import litellm
from litellm.llms.kie.health_check import health_check_context
from litellm.llms.kie.task_engine import execute_images
from litellm.llms.kie.common_utils import KieException

IMAGE = "gpt-image-2-5-flare"
VIDEO = "bytedance/seedance-2-fast"


@pytest.fixture
def credit_http(monkeypatch):
    requests = []
    outcome = {"status": 200, "code": 200}

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://api.kie.ai/api/v1/chat/credit"
        assert request.headers["authorization"] == "Bearer mock-credential"
        if "error" in outcome:
            raise outcome["error"]
        return httpx.Response(
            outcome["status"], json={"code": outcome["code"], "data": 0}
        )

    sync_cls, async_cls = httpx.Client, httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: sync_cls(**{**kw, "transport": transport})
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: async_cls(**{**kw, "transport": transport})
    )
    return requests, outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,model", [("image_generation", IMAGE), ("video_generation", VIDEO)]
)
async def test_ahealth_check_free_probe(credit_http, mode, model):
    requests, _ = credit_http
    result = await litellm.ahealth_check(
        {"model": "kie/" + model, "api_key": "mock-credential", "max_tokens": 1},
        mode=mode,
    )
    assert result == {}, result
    assert len(requests) == 1
    assert not health_check_context.get()


@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.asyncio
async def test_image_entry_probe_before_upload(credit_http, edit, is_async):
    requests, _ = credit_http
    token = health_check_context.set(True)
    try:
        result = execute_images(
            IMAGE,
            None,
            {"max_tokens": 1},
            {},
            api_key="mock-credential",
            image=b"must-not-upload" if edit else None,
            is_async=is_async,
        )
        if is_async:
            result = await result
        assert len(result.data) == 1
        assert result.data[0].url is None
        assert result.usage is None
        assert len(requests) == 1
    finally:
        health_check_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, 200), (200, 401), (200, 500)])
@pytest.mark.parametrize(
    "mode,model", [("image_generation", IMAGE), ("video_generation", VIDEO)]
)
async def test_failed_credit_is_unhealthy(credit_http, status, code, mode, model):
    requests, outcome = credit_http
    outcome.update(status=status, code=code)
    result = await litellm.ahealth_check(
        {"model": "kie/" + model, "api_key": "mock-credential", "max_tokens": 1},
        mode=mode,
    )
    assert "error" in result
    assert len(requests) == 1
    assert not health_check_context.get()


@pytest.mark.parametrize("edit", [False, True])
def test_normal_max_tokens_rejected_without_http(edit):
    client = MagicMock()
    with pytest.raises(KieException, match="max_tokens"):
        execute_images(
            IMAGE,
            "scene",
            {"max_tokens": 1, "size": "1024x1024"},
            {},
            api_key="mock-credential",
            client=client,
            image="https://example.com/input.png" if edit else None,
        )
    client.get.assert_not_called()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_normal_video_max_tokens_rejected(credit_http):
    requests, _ = credit_http
    with pytest.raises(litellm.BadRequestError, match="max_tokens"):
        await litellm.avideo_generation(
            model="kie/" + VIDEO,
            prompt="test video generation",
            seconds="5",
            max_tokens=1,
            api_key="mock-credential",
        )
    assert requests == []


@pytest.mark.asyncio
async def test_public_image_edit_probe(credit_http):
    requests, _ = credit_http
    token = health_check_context.set(True)
    try:
        result = await litellm.aimage_edit(
            model="kie/" + IMAGE,
            prompt="test",
            image=b"must-not-upload",
            max_tokens=1,
            api_key="mock-credential",
        )
        assert len(result.data) == 1
        assert len(requests) == 1
    finally:
        health_check_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize(
    "case,expected",
    [
        ("http401", litellm.AuthenticationError),
        ("business401", litellm.AuthenticationError),
        ("business500", litellm.APIError),
        ("network", litellm.APIError),
        ("timeout", litellm.Timeout),
    ],
)
async def test_credit_error_mapping(credit_http, is_async, case, expected):
    from litellm.llms.kie.health_check import acheck_credit, check_credit

    requests, outcome = credit_http
    if case == "http401":
        outcome["status"] = 401
    elif case.startswith("business"):
        outcome["code"] = int(case.removeprefix("business"))
    else:
        outcome["error"] = (
            httpx.ConnectError("mock network failure")
            if case == "network"
            else httpx.ReadTimeout("mock timeout")
        )
    with pytest.raises(expected):
        if is_async:
            await acheck_credit("mock-credential", VIDEO, video=True)
        else:
            check_credit("mock-credential", IMAGE)
    assert len(requests) == 1
