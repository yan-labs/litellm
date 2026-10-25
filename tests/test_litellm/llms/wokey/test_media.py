"""Offline Wokey media contracts; every HTTP call uses MockTransport.

Covers real LiteLLM entry points, multipart controls, provider-bound video reads,
actual-cost envelopes and the submission-only Kie fallback boundary.
"""

import base64
import json
from io import BytesIO

from PIL import Image
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.llms.wokey.common import uncertain_submission, usage
from litellm.llms.wokey.images import WokeyImageEditConfig, image_body, image_response
from litellm.llms.wokey.videos import WokeyVideoConfig
from litellm.types.videos.utils import decode_video_id_with_provider
from litellm.utils import _get_wrapper_num_retries

IMAGE = "gpt-image-2.5"
VIDEO = "grok-imagine-video-1.5"
buffer = BytesIO()
Image.new("RGBA", (1254, 1254), (40, 80, 120, 128)).save(buffer, format="PNG")
PNG = buffer.getvalue()


@pytest.fixture(autouse=True)
def isolated_model_catalog(monkeypatch):
    """Router registration must not overwrite price/capability state of sibling tests."""
    import copy
    monkeypatch.setattr(litellm, "model_cost", copy.deepcopy(litellm.model_cost))


def reply(data, status=200):
    """Build synthetic responses with no live requests or credentials."""
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://api.wokey.ai/v1/videos/task"))


@pytest.mark.parametrize("model", [IMAGE, "grok-imagine-image-2.0", VIDEO])
def test_registration(model):
    """Managed model strings resolve to the new adapter, not OpenAI proper."""
    assert litellm.get_llm_provider("wokey/" + model)[:2] == (model, "wokey")


@pytest.mark.parametrize("params,size", [({}, "1024x1024"), ({"size": "1536x1024"}, "1536x1024"), ({"size": "1536x1024", "quality": "high"}, "4096x2731"), ({"aspect_ratio": "16:9"}, "1024x576"), ({"size": "auto", "quality": "medium"}, "2048x2048"), ({"resolution": "4K", "aspect_ratio": "9:16"}, "2304x4096")])
def test_image_dimensions(params, size):
    """Size/quality/ratio mapping preserves framing and bounds each side."""
    assert image_body(IMAGE, "lake", params)["size"] == size


@pytest.mark.parametrize("params", [{"n": 2}, {"n": True}, {"size": "5000x128"}, {"size": "1x1"}, {"size": "100000x100000", "quality": "low"}, {"size": "-1x-1", "aspect_ratio": "1:1"}, {"aspect_ratio": "0:1"}, {"background": "transparent"}, {"output_format": "webp"}, {"resolution": "8K"}])
def test_image_unsupported_before_submission(params):
    """Unverified controls reject before charging instead of silently dropping them."""
    with pytest.raises(litellm.BadRequestError):
        image_body(IMAGE, "lake", params)


@pytest.mark.parametrize("size", ["1536x1024", "1024x1024"])
@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_public_image_generation(async_call, size):
    """Exercise actual public sync/async dispatch and b64/usage serialization."""
    requests = []
    def handle(req):
        requests.append(req)
        assert req.headers["authorization"] == "Bearer mock-key"
        assert req.url.path == "/v1/images/generations"
        body = json.loads(req.content)
        assert body["size"] == size
        assert f"Requested output canvas: {size} pixels" in body["prompt"]
        return reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}], "price_usd": .01})
    client = AsyncHTTPHandler() if async_call else HTTPHandler()
    client.client = (httpx.AsyncClient if async_call else httpx.Client)(transport=httpx.MockTransport(handle))
    args = dict(model="wokey/" + IMAGE, prompt="lake", size=size, api_key="mock-key", client=client)
    result = await litellm.aimage_generation(**args) if async_call else litellm.image_generation(**args)
    assert len(requests) == 1
    assert result.usage.provider == "wokey"
    assert result.usage.cost_usd == .01
    output = Image.open(BytesIO(base64.b64decode(result.data[0].b64_json)))
    assert base64.b64decode(result.data[0].b64_json) == PNG
    assert output.size == (1254, 1254)
    assert output.mode == "RGBA"
    assert output.getpixel((output.width // 2, output.height // 2))[3] == 128
    assert result._hidden_params["additional_headers"]["llm_provider-x-litellm-response-cost"] == .01
    await client.client.aclose() if async_call else client.client.close()


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_public_image_edit(async_call):
    """Edits use multipart image on generations, with output identical to text mode."""
    def handle(req):
        assert req.url.path == "/v1/images/generations"
        assert "multipart/form-data" in req.headers["content-type"]
        assert b'name="image"' in req.content
        assert b'name="image[]"' not in req.content
        assert b"1024x576" in req.content
        return reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    client = AsyncHTTPHandler() if async_call else HTTPHandler()
    client.client = (httpx.AsyncClient if async_call else httpx.Client)(transport=httpx.MockTransport(handle))
    args = dict(model="wokey/" + IMAGE, image=PNG, prompt="blue", size="1024x1024", extra_body={"aspect_ratio": "16:9"}, api_key="mock-key", client=client)
    result = await litellm.aimage_edit(**args) if async_call else litellm.image_edit(**args)
    assert result.usage.billing_pending is True
    assert base64.b64decode(result.data[0].b64_json) == PNG
    await client.client.aclose() if async_call else client.client.close()


@pytest.mark.parametrize("data", [{}, {"data": []}, {"data": [{"url": "https://unused"}]}])
def test_missing_inline_image(data):
    """Never return a temporary URL as a completed image artifact."""
    with pytest.raises(litellm.APIError):
        image_response(IMAGE, reply(data))


def test_multiple_edit_references_fail_before_fetch():
    """Preserve the full request for Kie when Wokey multi-image support is unknown."""
    with pytest.raises(litellm.BadRequestError):
        WokeyImageEditConfig().transform_image_edit_request(IMAGE, "blue", [PNG, PNG], {}, {}, {})


@pytest.mark.parametrize("raw,exact", [(None, False), (float("nan"), False), (-1, False), (.01, True)])
def test_truthful_billing(raw, exact):
    """Missing/invalid prices remain pending, not fabricated from catalog rates."""
    assert usage({"price_usd": raw})["cost_authoritative"] is exact


def create_request(params=None, headers=None):
    """Build a video request exactly where the shared handler calls the adapter."""
    return WokeyVideoConfig().transform_video_create_request(VIDEO, "boat", "https://api.wokey.ai/v1/videos", params or {"seconds": 1, "aspect_ratio": "16:9", "resolution": "480p"}, {}, headers if headers is not None else {})


def test_idempotency_and_video_mapping():
    """Retain supplied keys; duration and controls match the documented JSON API."""
    headers = {"idempotency-key": "same-request-0001"}
    body, files, _url = create_request({"seconds": "1", "extra_body": {"aspect_ratio": "16:9", "resolution": "480p"}}, headers)
    assert body == {"model": VIDEO, "prompt": "boat", "duration_seconds": 1, "ratio": "16:9", "video_resolution": "480p", "mode": "text_to_video"}
    assert headers["idempotency-key"] == "same-request-0001" and len(headers) == 1
    assert files == []
    headers = {}
    create_request(headers=headers)
    assert headers["Idempotency-Key"].startswith("wokey-media-")


@pytest.mark.parametrize("params", [{"seconds": 0}, {"seconds": 1.5}, {"seconds": True}, {"ratio": "auto"}, {"ratio": "3:2"}, {"resolution": "4k"}, {"generate_audio": True}, {"image_urls": ["https://one", "https://two"]}, {"frame_images": [{"frame_type": "last_frame"}]}])
def test_video_unsupported_submission(params):
    """Reject unsupported Kie-to-Wokey controls before any paid request."""
    with pytest.raises(litellm.BadRequestError):
        create_request(params)


def test_video_image_reference_download_without_key():
    """Kollab URL references become multipart bytes without leaking provider auth."""
    fetched = []
    def handle(req):
        fetched.append(req)
        assert "authorization" not in req.headers
        return httpx.Response(200, content=PNG)
    original = httpx.Client
    with patch("httpx.Client", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
        body, files, _ = create_request({"seconds": 1, "input_references": [{"type": "image_url", "image_url": {"url": "https://media.example/ref.png"}}]})
    assert body["mode"] == "image_to_video"
    assert files[0][0] == "image" and files[0][1][1] == PNG
    assert len(fetched) == 1


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_public_video_lifecycle(async_call):
    """Submit 202, poll pending/completed and authenticated canonical content download."""
    requests = []
    status_calls = 0
    def handle(req):
        nonlocal status_calls
        requests.append(req)
        assert req.headers["authorization"] == "Bearer mock-key"
        if req.method == "POST":
            assert req.headers["idempotency-key"] == "test-create-0001"
            return reply({"id": "task-123", "status": "in_progress", "price_usd": .0056}, 202)
        if req.url.path.endswith("/content"):
            return httpx.Response(200, content=b"mp4-result", headers={"content-type": "video/mp4"})
        status_calls += 1
        return reply({"id": "task-123", "status": "in_progress" if status_calls == 1 else "completed", "price_usd": .0056, "content_url": "/v1/videos/task-123/content"})
    client = AsyncHTTPHandler() if async_call else HTTPHandler()
    client.client = (httpx.AsyncClient if async_call else httpx.Client)(transport=httpx.MockTransport(handle))
    shared = dict(api_key="mock-key", api_base="https://api.wokey.ai/v1", client=client)
    args = dict(model="wokey/" + VIDEO, prompt="boat", seconds=1, extra_body={"aspect_ratio": "16:9", "resolution": "480p"}, extra_headers={"Idempotency-Key": "test-create-0001"}, **shared)
    created = await litellm.avideo_generation(**args) if async_call else litellm.video_generation(**args)
    assert decode_video_id_with_provider(created.id)["custom_llm_provider"] == "wokey"
    assert created.usage is None
    for expected in ("processing", "completed"):
        result = await litellm.avideo_status(video_id=created.id, **shared) if async_call else litellm.video_status(video_id=created.id, **shared)
        assert result.status == expected
    assert result.usage["cost_authoritative"] and result.usage["cost_usd"] == .0056
    assert result._hidden_params["additional_headers"]["llm_provider-x-litellm-response-cost"] == 0
    content = await litellm.avideo_content(video_id=created.id, **shared) if async_call else litellm.video_content(video_id=created.id, **shared)
    assert content == b"mp4-result"
    assert sum(req.method == "POST" for req in requests) == 1
    await client.client.aclose() if async_call else client.client.close()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_jobs_have_no_settled_usage(status):
    """Failed/cancelled reservations must not look like completed billable output."""
    cfg = WokeyVideoConfig()
    cfg._model = VIDEO
    result = cfg.transform_video_status_retrieve_response(reply({"id": "job", "status": status, "price_usd": .0056, "error": {"code": "failed", "message": "upstream failed"}}), None)
    assert result.status == "failed" and result.usage is None


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_submission_timeout_blocks_resubmission(async_call):
    """Public transport timeout retains the no-resubmit marker in both wrappers."""
    def handle(req):
        raise httpx.ReadTimeout("synthetic timeout", request=req)
    client = AsyncHTTPHandler() if async_call else HTTPHandler()
    client.client = (httpx.AsyncClient if async_call else httpx.Client)(transport=httpx.MockTransport(handle))
    args = dict(model="wokey/" + VIDEO, prompt="boat", seconds=1, api_key="mock-key", client=client, num_retries=0)
    with pytest.raises(Exception) as caught:
        await litellm.avideo_generation(**args) if async_call else litellm.video_generation(**args)
    assert getattr(caught.value, "media_no_resubmit", False)
    assert _get_wrapper_num_retries({"num_retries": 3}, caught.value)[0] == 0
    await client.client.aclose() if async_call else client.client.close()


def test_accepted_response_without_id_is_not_safe_fallback():
    """An ambiguous success cannot justify a second paid generation."""
    with pytest.raises(litellm.Timeout) as caught:
        WokeyVideoConfig().transform_video_create_response(VIDEO, reply({"status": "in_progress"}, 202), None)
    assert caught.value.media_no_resubmit


@pytest.mark.asyncio
async def test_router_fallback_boundary():
    """Actual router fallback entry accepts submission rejection, rejects job reads."""
    router = litellm.Router(model_list=[], num_retries=0)
    rejection = litellm.APIError(status_code=502, message="rejected", model=VIDEO, llm_provider="wokey")
    kwargs = dict(e=rejection, disable_fallbacks=False, fallbacks=[{"media": ["backup"]}], context_window_fallbacks=None, content_policy_fallbacks=None, model_group="media", args=(), kwargs={"model": "media"})
    with patch("litellm.router.run_async_fallback", new=AsyncMock(return_value="kie-submitted")) as fallback:
        assert await router.async_function_with_fallbacks_common_utils(**kwargs) == "kie-submitted"
        assert fallback.await_count == 1
        created = WokeyVideoConfig().transform_video_create_response(VIDEO, reply({"id": "job", "status": "in_progress"}, 202), None)
        kwargs["kwargs"] = {"model": "media", "video_id": created.id}
        with pytest.raises(litellm.APIError):
            await router.async_function_with_fallbacks_common_utils(**kwargs)
        assert fallback.await_count == 1
        kwargs["kwargs"] = {"model": "media"}
        kwargs["e"] = uncertain_submission()
        with pytest.raises(litellm.Timeout):
            await router.async_function_with_fallbacks_common_utils(**kwargs)
        assert fallback.await_count == 1


@pytest.mark.asyncio
async def test_router_retry_policy_cannot_override_uncertainty():
    """The full retry/fallback wrapper performs one call even with retry policies."""
    router = litellm.Router(model_list=[], num_retries=3, retry_policy=litellm.RetryPolicy(TimeoutErrorRetries=3))
    original = AsyncMock(side_effect=uncertain_submission())
    with patch.object(router, "make_call", original):
        with pytest.raises(litellm.Timeout):
            await router.async_function_with_fallbacks(model="media", original_function=original, num_retries=3, metadata={})
    assert original.await_count == 1


@pytest.mark.parametrize("status", [401, 409, 404, 500])
def test_content_failure_never_looks_like_mp4(status):
    """Expired/not-ready/unauthenticated content remains a read error, never a job."""
    with pytest.raises(litellm.AuthenticationError if status == 401 else litellm.APIError):
        WokeyVideoConfig().transform_video_content_response(reply({}, status), None)


def test_route_apply_roundtrip_and_scope(tmp_path):
    """Apply preserves the approved media, private V4.1, GLM and Haiku scope."""
    import subprocess
    from pathlib import Path

    import yaml

    from scripts.route_cli import DEFAULT, apply, load, render
    table = load(DEFAULT)
    root = Path.cwd()
    before = yaml.safe_load(subprocess.check_output(["git", "show", "315d15a19:config/litellm.test.yaml"], text=True))
    targets = {"gpt-image-2", "grok-imagine-image", "grok-imagine-video", "bedrock-claude-haiku", "glm-5.3-flash"}
    after = yaml.safe_load((root / "config/litellm.test.yaml").read_text())
    def unrelated(rows):
        return [{**r, "model_info": {k: v for k, v in r.get("model_info", {}).items() if k != "disable_health_check"}}
                for r in rows if r["model_name"] not in targets | {"deepseek-v4.1-flash"}
                and not any(r["model_name"] == f"__route__{name}__kie" for name in targets)]
    assert unrelated(before["model_list"]) == unrelated(after["model_list"])
    assert (root / "config/litellm.prod.yaml").read_bytes() == subprocess.check_output(["git", "show", "1b80d94fd:config/litellm.prod.yaml"])
    for environment in ("test", "prod"):
        path = tmp_path / (environment + ".yaml")
        path.write_bytes((root / f"config/litellm.{environment}.yaml").read_bytes())
        assert apply(table, environment, path) == 0
        assert apply(table, environment, path) == 0
        assert yaml.safe_load(path.read_text())["model_list"] == render(table, environment)["model_list"]
    for route in table["environments"]["test"]["routes"]:
        if route["alias"] in {"gpt-image-2", "grok-imagine-image", "grok-imagine-video"}:
            assert route["primary"] == "wokey" and route["fallback"] == ["kie"]


@pytest.mark.asyncio
async def test_real_router_submission_rejection_uses_kie():
    """The configured Router actually posts one Wokey rejection then one Kie job."""
    posts = []
    def handle(req):
        posts.append(req.url.host)
        if req.url.host == "api.wokey.ai":
            return reply({"error": {"code": "upstream_unavailable"}}, 502)
        assert req.url.path.endswith("/jobs/createTask")
        return reply({"code": 200, "data": {"taskId": "kie-task"}})
    client = AsyncHTTPHandler()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    router = litellm.Router(model_list=[
        {"model_name": "media", "litellm_params": {"model": "wokey/" + VIDEO, "api_key": "mock-key"}, "model_info": {"mode": "video_generation"}},
        {"model_name": "kie-backup", "litellm_params": {"model": "kie/grok-imagine-video-1-5-preview", "api_key": "mock-key"}, "model_info": {"mode": "video_generation"}},
    ], fallbacks=[{"media": ["kie-backup"]}], num_retries=0)
    with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=client):
        result = await router.avideo_generation(model="media", prompt="boat", seconds=1, extra_body={"aspect_ratio": "16:9", "resolution": "480p"})
    assert decode_video_id_with_provider(result.id)["custom_llm_provider"] == "kie"
    assert posts == ["api.wokey.ai", "api.kie.ai"]
    await client.client.aclose()


@pytest.mark.parametrize("status", [None, "new_upstream_state"])
def test_accepted_id_with_unknown_status_blocks_fallback(status):
    """An ID proves accepted generation even when the upstream status schema drifts."""
    with pytest.raises(litellm.Timeout) as caught:
        WokeyVideoConfig().transform_video_create_response(VIDEO, reply({"id": "reserved-job", "status": status}, 202), None)
    assert caught.value.media_no_resubmit


@pytest.mark.asyncio
async def test_uncertainty_in_second_retry_stops_third_attempt():
    """A definite rejection then a lost response stops the full router retry loop."""
    router = litellm.Router(model_list=[], num_retries=3, retry_policy=litellm.RetryPolicy(RateLimitErrorRetries=3))
    rejection = litellm.RateLimitError(message="rejected", model=VIDEO, llm_provider="wokey")
    original = AsyncMock(side_effect=[rejection, uncertain_submission(), "must-not-run"])
    with patch.object(router, "make_call", original), patch.object(router, "_async_get_healthy_deployments", new=AsyncMock(return_value=([], []))), patch.object(router, "_time_to_sleep_before_retry", return_value=0):
        with pytest.raises(litellm.Timeout):
            await router.async_function_with_fallbacks(model="media", original_function=original, num_retries=3, metadata={})
    assert original.await_count == 2


@pytest.mark.parametrize("mode,model", [("image_generation", IMAGE), ("video_generation", VIDEO)])
@pytest.mark.asyncio
async def test_internal_health_probe_never_submits(mode, model):
    """Real ahealth_check reads metadata only; no billable POST for either modality."""
    requests = []
    def handle(req):
        requests.append(req)
        assert req.method == "GET" and req.url.path == "/v1/models"
        assert req.headers["authorization"] == "Bearer mock-key"
        return reply({"data": [{"id": model}]})
    original = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
        result = await litellm.ahealth_check(model_params={"model": "wokey/" + model, "api_key": "mock-key"}, mode=mode)
    assert "error" not in result
    assert len(requests) == 1


def test_current_proxy_resolves_provider_encoded_id():
    """The proxy's actual alias resolver can recover the named Wokey deployment."""
    from scripts.route_cli import DEFAULT, load, render
    models = render(load(DEFAULT), "test")["model_list"]
    video = WokeyVideoConfig().transform_video_create_response(VIDEO, reply({"id": "job", "status": "in_progress"}, 202), None)
    decoded = decode_video_id_with_provider(video.id)
    router = litellm.Router(model_list=models, num_retries=0)
    assert router.resolve_model_name_from_model_id(decoded["model_id"]) == "grok-imagine-video"
    row = next(r for r in models if r["model_name"] == "grok-imagine-video")
    assert row["litellm_params"]["litellm_credential_name"] == "wokey"


@pytest.mark.parametrize("status,error_type", [(400, litellm.BadRequestError), (401, litellm.AuthenticationError), (402, litellm.APIError), (429, litellm.RateLimitError), (502, litellm.APIError), (504, litellm.APIError)])
def test_upstream_failure_codes_keep_public_error_contract(status, error_type):
    """Preserve HTTP rejection codes through public exception mapping without raw bodies."""
    from litellm.llms.wokey.common import error
    exc = error(status, "Wokey media request rejected")
    assert isinstance(exc, error_type) and exc.status_code == status
    assert exc.llm_provider == "wokey"


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_grok_image_top_level_ratio_survives_dispatch(async_call):
    """Actual Kollab generation sends aspect_ratio at top level, not extra_body."""
    def handle(req):
        assert json.loads(req.content)["size"] == "2048x1152"
        return reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    client = AsyncHTTPHandler() if async_call else HTTPHandler()
    client.client = (httpx.AsyncClient if async_call else httpx.Client)(transport=httpx.MockTransport(handle))
    args = dict(model="wokey/grok-imagine-image-2.0", prompt="boat", aspect_ratio="16:9", api_key="mock-key", client=client)
    result = await litellm.aimage_generation(**args) if async_call else litellm.image_generation(**args)
    assert result.data
    await client.client.aclose() if async_call else client.client.close()

@pytest.mark.parametrize("model", ["gpt-image-2.5", "gpt-image-2"])
@pytest.mark.parametrize("params,canvas,ratio", [({"size": "1024x1024"}, "1024x1024", "1:1"), ({"size": "1536x1024"}, "1536x1024", "3:2"), ({"aspect_ratio": "16:9"}, "1024x576", "16:9")])
def test_gpt_explicit_canvas_in_prompt(model, params, canvas, ratio):
    prompt = "A cup of coffee beside an open book in morning light."
    body = image_body(model, prompt, params)
    assert body["size"] == canvas
    assert body["prompt"] == prompt + f"\nRequested output canvas: {canvas} pixels, aspect ratio {ratio}. Compose the image within this canvas."


def test_auto_and_grok_prompts_unchanged():
    prompt = "A cup of coffee beside an open book in morning light."
    assert image_body(IMAGE, prompt, {})["prompt"] == prompt
    assert image_body("grok-imagine-image-2.0", prompt, {"size": "1024x1024"})["prompt"] == prompt


@pytest.mark.parametrize("model,params", [(IMAGE, {}), (IMAGE, {"size": "auto"}), ("grok-imagine-image-2.0", {"size": "1024x1024"})])
def test_image_response_leaves_auto_and_grok_bytes_unchanged(model, params):
    encoded = base64.b64encode(PNG).decode()
    result = image_response(model, reply({"data": [{"b64_json": encoded}]}), params)
    assert result.data[0].b64_json == encoded


@pytest.mark.parametrize("model", [IMAGE, "gpt-image-2"])
def test_explicit_size_and_quality_preserve_upstream_pixels(model):
    source = Image.new("RGB", (1600, 800), "red")
    source.paste("blue", (400, 0, 1200, 800))
    buffer = BytesIO()
    source.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    result = image_response(model, reply({"data": [{"b64_json": encoded}], "price_usd": .01}), {"size": "1024x1024", "quality": "high"})
    output = Image.open(BytesIO(base64.b64decode(result.data[0].b64_json)))
    assert result.data[0].b64_json == encoded
    assert output.size == (1600, 800)
    assert output.getpixel((50, 400)) == (255, 0, 0)
    assert output.getpixel((1550, 400)) == (255, 0, 0)
    assert result.usage.cost_usd == .01


def test_extra_body_size_preserves_upstream_bytes():
    encoded = base64.b64encode(PNG).decode()
    assert image_response(IMAGE, reply({"data": [{"b64_json": encoded}]}), {"extra_body": {"size": "1024x1024"}}).data[0].b64_json == encoded


def test_image_response_does_not_decode_upstream_bytes():
    assert image_response(IMAGE, reply({"data": [{"b64_json": "invalid"}]}), {"size": "1024x1024"}).data[0].b64_json == "invalid"
