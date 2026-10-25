"""Offline contract tests: no credentials, models or network calls."""

import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest

import litellm
from litellm.llms.kie.common_utils import KieException, envelope, result_urls, usage
from litellm.llms.kie.model_config import build_input, config
from litellm.llms.kie.task_engine import TaskEngine, execute_images
from litellm.llms.kie.videos.transformation import KieVideoConfig
from litellm.types.utils import LlmProviders
from litellm.types.videos.utils import decode_video_id_with_provider
from litellm.utils import ProviderConfigManager

IMAGE = "gpt-image-2-5-flare"
VIDEO = "bytedance/seedance-2-fast"


def response(data=None, code=200):
    return httpx.Response(
        200,
        json={"code": code, "data": data},
        request=httpx.Request("GET", "https://api.kie.ai"),
    )


def task(state="success", **kw):
    return {
        "taskId": "task-123",
        "state": state,
        "resultJson": json.dumps({"resultUrls": ["https://media.example/result"]}),
        "creditsConsumed": 6,
        "costTime": 42,
        **kw,
    }


def mock_transport(states=("success",)):
    captured = []
    remaining = iter(states)

    def handle(request):
        captured.append(request)
        if request.url.host == "media.example":
            assert "authorization" not in request.headers
            return httpx.Response(
                200, content=b"image-bytes", headers={"content-type": "image/png"}
            )
        assert request.headers["authorization"] == "Bearer mock-credential"
        if request.url.path.endswith("file-base64-upload"):
            assert json.loads(request.content)["base64Data"].startswith("data:")
            return response({"downloadUrl": "https://media.example/input.png"})
        if request.url.path.endswith("createTask"):
            return response({"taskId": "task-123"})
        assert request.url.params["taskId"] == "task-123"
        return response(task(next(remaining)))

    return httpx.MockTransport(handle), captured


@pytest.mark.parametrize(
    "model",
    [
        IMAGE,
        "grok-imagine-image-2-0",
        VIDEO,
        "bytedance/seedance-2-5",
        "bytedance/seedance-2-mini",
        "kling-3.0/video",
        "grok-imagine-video-1-5-preview",
    ],
)
def test_catalog_and_registration(model):
    rule = config(model)
    if "edit_model" in rule:
        assert ProviderConfigManager.get_provider_image_generation_config(
            model, LlmProviders.KIE
        )
        assert ProviderConfigManager.get_provider_image_edit_config(
            model, LlmProviders.KIE
        )
    else:
        assert ProviderConfigManager.get_provider_video_config(model, LlmProviders.KIE)
    assert litellm.get_llm_provider("kie/" + model)[:2] == (model, "kie")


def test_catalog_packaged_copy_matches_root():
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    # Resolve repository rather than relying on a sparse test layout.
    root = next(
        p
        for p in Path(__file__).resolve().parents
        if (p / "model_prices_and_context_window.json").exists()
    )
    records = json.loads((root / "model_prices_and_context_window.json").read_text())
    packaged = json.loads((root / "litellm/llms/kie/model_catalog.json").read_text())
    assert packaged == {k: v for k, v in records.items() if k.startswith("kie/")}


@pytest.mark.parametrize(
    "model", [VIDEO, "bytedance/seedance-2-5", "bytedance/seedance-2-mini"]
)
def test_seedance_mapping(model):
    body = build_input(
        model,
        "scene",
        {
            "seconds": "5",
            "size": "1280x720",
            "frame_images": [
                {
                    "frame_type": "first_frame",
                    "image_url": {"url": "https://media.example/first"},
                },
                {
                    "frame_type": "last_frame",
                    "image_url": {"url": "https://media.example/last"},
                },
            ],
        },
    )
    assert body["input"]["first_frame_url"].endswith("first")
    assert body["input"]["last_frame_url"].endswith("last")
    assert body["input"]["duration"] == 5
    assert body["input"]["aspect_ratio"] == "16:9"
    body = build_input(
        model,
        "scene",
        {
            "input_references": [
                {"type": k + "_url", k + "_url": {"url": "https://media.example/" + k}}
                for k in ["image", "video", "audio"]
            ]
        },
    )
    for kind in ["image", "video", "audio"]:
        assert body["input"]["reference_" + kind + "_urls"] == [
            "https://media.example/" + kind
        ]


def test_kling_mapping():
    body = build_input(
        "kling-3.0/video",
        "scene",
        {"seconds": 3, "size": "1920x1080", "generate_audio": True},
    )
    assert body["input"]["duration"] == "3"
    assert body["input"]["mode"] == "pro"
    assert body["input"]["aspect_ratio"] == "16:9"
    assert body["input"]["sound"] is True
    assert body["input"]["multi_prompt"] == []


def test_grok_video_mapping():
    body = build_input(
        "grok-imagine-video-1-5-preview",
        "scene",
        {
            "seconds": 1,
            "size": "1280x720",
            "input_references": [
                {"type": "image_url", "image_url": {"url": "https://media.example/a"}}
            ],
        },
    )
    assert body["input"]["duration"] == 1
    assert body["input"]["resolution"] == "720p"
    assert body["input"]["image_urls"] == ["https://media.example/a"]


@pytest.mark.parametrize(
    "model,params",
    [
        (VIDEO, {"seed": 1}),
        ("kling-3.0/video", {"resolution": "2k"}),
        ("kling-3.0/video", {"aspect_ratio": "21:9"}),
        ("grok-imagine-video-1-5-preview", {"generate_audio": True}),
        (
            "grok-imagine-video-1-5-preview",
            {"frame_images": [{"frame_type": "last_frame", "image_url": {"url": "x"}}]},
        ),
        (VIDEO, {"seconds": 4.5}),
        (
            "bytedance/seedance-2-5",
            {"first_frame_url": "x", "reference_image_urls": ["y"]},
        ),
        (IMAGE, {"mask": "x"}),
    ],
)
def test_unsupported_controls(model, params):
    with pytest.raises(KieException):
        build_input(model, "scene", params)


def test_image_models_and_required_grok_ratio():
    assert build_input(IMAGE, "scene", {"size": "1536x1024", "quality": "high"})[
        "input"
    ] == {"prompt": "scene", "aspect_ratio": "3:2", "resolution": "4K"}
    assert (
        build_input(IMAGE, "scene", {"input_urls": ["https://media.example/a"]}, True)[
            "model"
        ]
        == "gpt-image-2-5-flare-image-to-image"
    )
    assert (
        build_input("grok-imagine-image-2-0", "scene", {})["input"]["aspect_ratio"]
        == "1:1"
    )
    assert (
        build_input("grok-imagine-image-2-0", "scene", {"image_urls": ["x"]}, True)[
            "model"
        ]
        == "grok-imagine-image-2-0/image-edit"
    )


def test_sync_poll_upload_and_download(monkeypatch):
    monkeypatch.setattr("litellm.llms.kie.task_engine.time.sleep", lambda _: None)
    transport, requests = mock_transport(
        ("waiting", "queuing", "generating", "success")
    )
    with httpx.Client(transport=transport) as client:
        output = execute_images(
            IMAGE,
            "scene",
            {"n": 1},
            {},
            api_key="mock-credential",
            client=client,
            image=b"input",
        )
    assert output.data[0].b64_json == base64.b64encode(b"image-bytes").decode()
    assert output.usage["creditsConsumed"] == 6
    assert output.usage["cost_usd"] == 0.03
    assert (
        output._hidden_params["additional_headers"][
            "llm_provider-x-litellm-response-cost"
        ]
        == 0.03
    )
    creates = [r for r in requests if r.url.path.endswith("createTask")]
    assert len(creates) == 1
    assert json.loads(creates[0].content)["input"]["input_urls"] == [
        "https://media.example/input.png"
    ]
    assert len([r for r in requests if r.url.path.endswith("recordInfo")]) == 4


@pytest.mark.asyncio
async def test_async_poll_does_not_block(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("litellm.llms.kie.task_engine.asyncio.sleep", sleep)
    transport, requests = mock_transport(("waiting", "generating", "success"))
    async with httpx.AsyncClient(transport=transport) as client:
        output = await execute_images(
            IMAGE,
            "scene",
            {},
            {},
            api_key="mock-credential",
            client=client,
            is_async=True,
            image=b"input",
        )
    assert delays == [2, 3]
    assert output.usage["cost_usd"] == 0.03
    assert len([r for r in requests if r.url.path.endswith("createTask")]) == 1


def test_total_deadline_no_resubmit(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "litellm.llms.kie.task_engine.time.sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    transport, requests = mock_transport(("generating",) * 5)
    with httpx.Client(transport=transport) as client:
        engine = TaskEngine(
            "mock-credential", client=client, budget=3, clock=lambda: now[0]
        )
        with pytest.raises(KieException, match="taskId=task-123") as caught:
            engine.images(IMAGE, "scene", {})
    assert caught.value.status_code == 408
    assert len([r for r in requests if r.url.path.endswith("createTask")]) == 1


@pytest.mark.parametrize("code", [400, 401, 402, 422, 429, 500, 503])
def test_business_error_mapping(code):
    with pytest.raises(KieException) as caught:
        envelope(response(code=code))
    assert caught.value.status_code == code


def test_content_rejection_unknown_failure_and_json():
    with pytest.raises(KieException, match="Content policy violation") as caught:
        TaskEngine.ready(task("fail", failCode="policy", failMsg="NSFW content"))
    assert caught.value.status_code == 400
    with pytest.raises(KieException) as caught:
        TaskEngine.ready(task("fail", failCode="unknown", failMsg="render failure"))
    assert caught.value.status_code == 500
    assert result_urls(
        task(resultJson=json.dumps(json.dumps({"resultUrls": ["url"]})))
    ) == ["url"]
    assert usage(task())["cost_usd"] == 0.03
    assert usage(task(creditsConsumed=None))["billing_pending"] is True


@pytest.mark.parametrize(
    "state,status",
    [
        ("waiting", "queued"),
        ("queuing", "queued"),
        ("generating", "processing"),
        ("success", "completed"),
        ("fail", "failed"),
    ],
)
def test_video_id_status(state, status):
    converter = KieVideoConfig()
    created = converter.transform_video_create_response(
        VIDEO, response({"taskId": "task-123"}), None, "kie"
    )
    decoded = decode_video_id_with_provider(created.id)
    assert decoded == {
        "custom_llm_provider": "kie",
        "model_id": VIDEO,
        "video_id": "task-123",
    }
    assert (
        created._hidden_params["additional_headers"][
            "llm_provider-x-litellm-response-cost"
        ]
        == 0
    )
    url, _ = converter.transform_video_status_retrieve_request(
        created.id, "https://api.kie.ai/api/v1", {}, {}
    )
    assert url.endswith("recordInfo?taskId=task-123")
    result = converter.transform_video_status_retrieve_response(
        response(task(state, failCode="400", failMsg="invalid request")), None, "kie"
    )
    assert result.status == status and result.id == created.id
    assert (
        (result.usage["cost_usd"] == 0.03)
        if state == "success"
        else result.usage is None
    )
    if state == "fail":
        assert result.error["code"] == "400"


def test_images_public_entry_httpx(monkeypatch):
    transport, requests = mock_transport()
    with httpx.Client(transport=transport) as client:
        result = litellm.image_generation(
            model="kie/" + IMAGE,
            prompt="scene",
            api_key="mock-credential",
            client=client,
        )
    create = next(r for r in requests if r.url.path.endswith("createTask"))
    assert str(create.url) == "https://api.kie.ai/api/v1/jobs/createTask"
    assert json.loads(create.content) == {
        "model": "gpt-image-2-5-flare-text-to-image",
        "input": {"prompt": "scene", "resolution": "1K", "aspect_ratio": "auto"},
    }
    assert result.data[0].b64_json


def test_videos_entry_httpx(monkeypatch):
    from litellm.llms.custom_httpx.http_handler import HTTPHandler
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    transport, requests = mock_transport()
    client = HTTPHandler()
    client.client = httpx.Client(transport=transport)
    result = BaseLLMHTTPHandler().video_generation_handler(
        model=VIDEO,
        prompt="scene",
        video_generation_provider_config=KieVideoConfig(),
        video_generation_optional_request_params={"seconds": 5, "size": "1280x720"},
        custom_llm_provider="kie",
        litellm_params={},
        logging_obj=MagicMock(),
        timeout=30,
        api_key="mock-credential",
        client=client,
    )
    client.client.close()
    create = next(r for r in requests if r.url.path.endswith("createTask"))
    assert str(create.url) == "https://api.kie.ai/api/v1/jobs/createTask"
    assert json.loads(create.content) == {
        "model": VIDEO,
        "input": {
            "prompt": "scene",
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "generate_audio": True,
        },
    }
    assert result.status == "queued"


def test_public_image_edit_httpx():
    transport, requests = mock_transport()
    with httpx.Client(transport=transport) as client:
        result = litellm.image_edit(
            model="kie/" + IMAGE,
            prompt="scene",
            image=b"input",
            api_key="mock-credential",
            client=client,
        )
    body = json.loads(
        next(r for r in requests if r.url.path.endswith("createTask")).content
    )
    assert body["model"] == "gpt-image-2-5-flare-image-to-image"
    assert body["input"]["input_urls"] == ["https://media.example/input.png"]
    assert result.usage["cost_usd"] == 0.03


@pytest.mark.asyncio
async def test_public_async_images_httpx():
    transport, requests = mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await litellm.aimage_generation(
            model="kie/" + IMAGE,
            prompt="scene",
            api_key="mock-credential",
            client=client,
        )
    assert result.data[0].b64_json
    assert len([r for r in requests if r.url.path.endswith("createTask")]) == 1


@pytest.mark.asyncio
async def test_public_async_edit_httpx():
    transport, requests = mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await litellm.aimage_edit(
            model="kie/grok-imagine-image-2-0",
            prompt="scene",
            image=b"input",
            api_key="mock-credential",
            client=client,
        )
    body = json.loads(
        next(r for r in requests if r.url.path.endswith("createTask")).content
    )
    assert body["model"] == "grok-imagine-image-2-0/image-edit"
    assert body["input"]["aspect_ratio"] == "1:1"
    assert body["input"]["image_urls"] == ["https://media.example/input.png"]
    assert result.data[0].b64_json


def test_multiple_images_cost_and_no_missing_credit_fabrication():
    transport, _requests = mock_transport(("success", "success"))
    with httpx.Client(transport=transport) as client:
        result = execute_images(
            IMAGE, "scene", {"n": 2}, {}, api_key="mock-credential", client=client
        )
    assert len(result.data) == 2
    assert result.usage["creditsConsumed"] == 12 and result.usage["cost_usd"] == 0.06
    missing = TaskEngine.response(IMAGE, [], [task(creditsConsumed=None)])
    assert "additional_headers" not in missing._hidden_params
    assert missing.usage["billing_pending"] is True


def test_record_not_visible_yet(monkeypatch):
    def handle(request):
        if request.url.path.endswith("createTask"):
            return response({"taskId": "task-123"})
        return response(None, 422)

    now = [0.0]
    monkeypatch.setattr(
        "litellm.llms.kie.task_engine.time.sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        engine = TaskEngine(
            "mock-credential", client=client, budget=3, clock=lambda: now[0]
        )
        with pytest.raises(litellm.Timeout):
            engine.images(IMAGE, "scene", {})
    converter = KieVideoConfig()
    created = converter.transform_video_create_response(
        VIDEO, response({"taskId": "task-123"}), None
    )
    converter.transform_video_status_retrieve_request(
        created.id, "https://api.kie.ai/api/v1", {}, {}
    )
    assert (
        converter.transform_video_status_retrieve_response(
            response(None, 422), None
        ).status
        == "queued"
    )


def test_native_exception_classes():
    from litellm.llms.kie.common_utils import failure

    assert isinstance(failure(401, "unauthorized"), litellm.AuthenticationError)
    assert isinstance(failure(429, "rate limit"), litellm.RateLimitError)
    assert isinstance(failure(400, "NSFW content"), litellm.ContentPolicyViolationError)
    assert isinstance(
        failure("CONTENT_POLICY_VIOLATION", "rejected"),
        litellm.ContentPolicyViolationError,
    )
    assert isinstance(failure(422, "invalid input"), litellm.BadRequestError)
    assert isinstance(failure(402, "insufficient credits"), litellm.APIError)


def test_video_content_download(monkeypatch):
    real_client = httpx.Client
    requests = []

    def handle(request):
        requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"video-bytes")

    monkeypatch.setattr(
        "litellm.llms.kie.videos.transformation.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert (
        KieVideoConfig().transform_video_content_response(response(task()), None)
        == b"video-bytes"
    )
    assert str(requests[0].url) == "https://media.example/result"


@pytest.mark.asyncio
async def test_async_video_content_download(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "litellm.llms.kie.videos.transformation.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"video-bytes")
            ),
            **kwargs,
        ),
    )
    assert (
        await KieVideoConfig().async_transform_video_content_response(
            response(task()), None
        )
        == b"video-bytes"
    )


def test_yaml_media_routes_and_unique_aliases():
    from pathlib import Path

    from scripts.validate_multimedia_config import validate_media_routes

    # PROD 图片视频别名待 Kollab 新版发布后再切 kie，届时把 PROD 期望并入 TEST 期望。
    for environment in ["test", "prod"]:
        raw = Path("config/litellm." + environment + ".yaml").read_text()
        assert len(validate_media_routes(raw, environment)) == (8 if environment == "test" else 5)


def test_public_video_entry_httpx():
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    transport, requests = mock_transport()
    client = HTTPHandler()
    client.client = httpx.Client(transport=transport)
    result = litellm.video_generation(
        model="kie/kling-3.0/video",
        prompt="scene",
        seconds="3",
        size="1280x720",
        api_key="mock-credential",
        client=client,
    )
    client.client.close()
    body = json.loads(
        next(r for r in requests if r.url.path.endswith("createTask")).content
    )
    assert body["model"] == "kling-3.0/video"
    assert body["input"]["mode"] == "std" and body["input"]["duration"] == "3"
    assert result.id.startswith("video_")


def test_grok_edit_auto_ratio():
    assert (
        build_input(
            "grok-imagine-image-2-0",
            "scene",
            {"aspect_ratio": "auto", "image_urls": ["x"]},
            True,
        )["input"]["aspect_ratio"]
        == "auto"
    )
    with pytest.raises(KieException):
        build_input("grok-imagine-image-2-0", "scene", {"aspect_ratio": "auto"})


def test_video_reads_do_not_double_bill():
    from litellm.cost_calculator import get_response_cost_from_hidden_params

    converter = KieVideoConfig()
    for _ in range(2):
        result = converter.transform_video_status_retrieve_response(
            response(task()), None
        )
        assert result.usage["cost_usd"] == 0.03
        assert get_response_cost_from_hidden_params(result._hidden_params) == 0


@pytest.mark.asyncio
async def test_router_timeout_does_not_resubmit_created_image(monkeypatch):
    creates = []

    async def arequest(self, method, url, body=None, download=False):
        if url.endswith("createTask"):
            creates.append(url)
            return {"taskId": "task-123"}
        from litellm.llms.kie.common_utils import failure

        raise failure(408, "Kie poll timeout; taskId=task-123; do not resubmit")

    monkeypatch.setattr(TaskEngine, "arequest", arequest)
    router = litellm.Router(
        model_list=[
            {
                "model_name": "gpt-image-2",
                "litellm_params": {
                    "model": "kie/" + IMAGE,
                    "custom_llm_provider": "kie",
                    "api_key": "mock-credential",
                },
                "model_info": {"mode": "image_generation"},
            }
        ],
        disable_cooldowns=True,
    )
    assert router.num_retries > 0
    with pytest.raises(litellm.Timeout):
        await router.aimage_generation(model="gpt-image-2", prompt="scene")
    assert len(creates) == 1


def test_kling_tail_frame_cannot_become_first_frame():
    with pytest.raises(KieException, match="requires a first frame"):
        build_input(
            "kling-3.0/video",
            "scene",
            {
                "frame_images": [
                    {"frame_type": "last_frame", "image_url": {"url": "tail"}}
                ]
            },
        )
    body = build_input(
        "kling-3.0/video",
        "scene",
        {
            "frame_images": [
                {"frame_type": "last_frame", "image_url": {"url": "tail"}},
                {"frame_type": "first_frame", "image_url": {"url": "head"}},
            ]
        },
    )
    assert body["input"]["image_urls"] == ["head", "tail"]


def test_sync_router_timeout_does_not_resubmit(monkeypatch):
    creates = []

    def request(self, method, url, body=None, download=False):
        from litellm.llms.kie.common_utils import failure

        if url.endswith("createTask"):
            creates.append(url)
            return {"taskId": "task-123"}
        raise failure(408, "Kie poll deadline; do not resubmit")

    monkeypatch.setattr(TaskEngine, "request", request)
    router = litellm.Router(
        model_list=[
            {
                "model_name": "gpt-image-2",
                "litellm_params": {
                    "model": "kie/" + IMAGE,
                    "api_key": "mock-credential",
                },
            }
        ],
        disable_cooldowns=True,
    )
    with pytest.raises(litellm.Timeout):
        router.image_generation(model="gpt-image-2", prompt="scene")
    assert len(creates) == 1


@pytest.mark.asyncio
async def test_router_video_create_transport_timeout_does_not_resubmit(monkeypatch):
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    creates = []

    def handle(request):
        creates.append(request.url.path)
        raise httpx.ReadTimeout("mock create timeout", request=request)

    client = AsyncHTTPHandler()
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(
        "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
        lambda **kwargs: client,
    )
    router = litellm.Router(
        model_list=[
            {
                "model_name": "seedance-2",
                "litellm_params": {
                    "model": "kie/" + VIDEO,
                    "api_key": "mock-credential",
                },
            }
        ],
        disable_cooldowns=True,
    )
    try:
        with pytest.raises(litellm.Timeout):
            await router.avideo_generation(model="seedance-2", prompt="scene")
    finally:
        await client.client.aclose()
    assert creates == ["/api/v1/jobs/createTask"]
