"""One submission per image, bounded polling, uploads and immediate downloads."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx

from litellm.types.utils import ImageResponse, KieImageUsage

from .common_utils import (
    API_BASE,
    UPLOAD_URL,
    auth,
    envelope,
    failure,
    result_urls,
    usage,
)
from .model_config import build_input, config


class TaskEngine:
    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        client: Any = None,
        budget: float = 270,
        clock=time.monotonic,
    ):
        self.headers = auth({}, api_key)
        self.base = (api_base or API_BASE).rstrip("/")
        self.client = client
        self.clock = clock
        self.deadline = clock() + min(budget, 270)
        self.task_id = None

    def timeout(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise failure(
                408,
                f"Kie image deadline exceeded; taskId={self.task_id or 'not-created'}; do not resubmit",
            )
        return min(30, remaining)

    def request(self, method: str, url: str, body: dict | None = None, download=False):
        kwargs = {
            "timeout": self.timeout(),
            "headers": {} if download else self.headers,
        }
        if body is not None:
            kwargs["json"] = body
        try:
            response = getattr(self.client, method)(url=url, **kwargs)
        except httpx.TimeoutException:
            raise failure(
                408,
                f"Kie HTTP timeout; taskId={self.task_id or 'unknown'}; do not resubmit",
            ) from None
        self.timeout()
        if download:
            response.raise_for_status()
            return response
        return envelope(response, allow_pending="/jobs/recordInfo?" in url)

    async def arequest(
        self, method: str, url: str, body: dict | None = None, download=False
    ):
        kwargs = {
            "timeout": self.timeout(),
            "headers": {} if download else self.headers,
        }
        if body is not None:
            kwargs["json"] = body
        try:
            response = await asyncio.wait_for(
                getattr(self.client, method)(url=url, **kwargs), timeout=self.timeout()
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise failure(
                408,
                f"Kie HTTP timeout; taskId={self.task_id or 'unknown'}; do not resubmit",
            ) from None
        self.timeout()
        if download:
            response.raise_for_status()
            return response
        return envelope(response, allow_pending="/jobs/recordInfo?" in url)

    @staticmethod
    def upload_body(image: Any) -> dict:
        if isinstance(image, str) and image.startswith("data:"):
            data = image
        else:
            if isinstance(image, tuple):
                image = image[1]
            if hasattr(image, "read"):
                position = image.tell()
                image.seek(0)
                data = image.read()
                image.seek(position)
            else:
                data = image
            if not isinstance(data, bytes):
                raise failure(400, "Unsupported Kie edit image input")
            from litellm.images.utils import ImageEditRequestUtils

            mime = ImageEditRequestUtils.get_image_content_type(data)
            data = f"data:{mime};base64," + base64.b64encode(data).decode()
        return {
            "base64Data": data,
            "uploadPath": "litellm/images",
            "fileName": "input.png",
        }

    @staticmethod
    def ready(task: dict) -> bool:
        if task.get("state") == "fail":
            raise failure(task.get("failCode"), task.get("failMsg"))
        return task.get("state") == "success"

    def poll(self) -> dict:
        delay = 2.0
        while True:
            task = self.request(
                "get", f"{self.base}/jobs/recordInfo?taskId={self.task_id}"
            )
            if self.ready(task):
                return task
            time.sleep(max(0, min(delay, self.deadline - self.clock())))
            self.timeout()
            delay = min(10, delay * 1.5)

    async def apoll(self) -> dict:
        delay = 2.0
        while True:
            task = await self.arequest(
                "get", f"{self.base}/jobs/recordInfo?taskId={self.task_id}"
            )
            if self.ready(task):
                return task
            await asyncio.sleep(max(0, min(delay, self.deadline - self.clock())))
            self.timeout()
            delay = min(10, delay * 1.5)

    def prepare(self, model: str, params: dict, urls: list) -> dict:
        p = dict(params)
        if urls:
            p[config(model)["edit_field"]] = urls
        return p

    @staticmethod
    def response(model: str, data: list, tasks: list) -> ImageResponse:
        records = [usage(task) for task in tasks]
        authoritative = all(record["cost_authoritative"] for record in records)
        provider_usage = {
            "provider": "kie",
            "tasks": records,
            "cost_authoritative": authoritative,
            "billing_pending": not authoritative,
        }
        if authoritative:
            credits = sum(float(r["kie_credits"]) for r in records)
            cost = sum(r["cost_usd"] for r in records)
            provider_usage.update(
                kie_credits=credits, creditsConsumed=credits, cost=cost, cost_usd=cost
            )
        response = ImageResponse(data=data, usage=KieImageUsage(**provider_usage))
        response._hidden_params["model"] = model
        response._hidden_params["custom_llm_provider"] = "kie"
        if authoritative:
            response._hidden_params["additional_headers"] = {
                "llm_provider-x-litellm-response-cost": cost
            }
        return response

    @staticmethod
    def count(params: dict) -> int:
        n = params.get("n") or 1
        if isinstance(n, bool) or int(n) != float(n) or int(n) < 1:
            raise failure(400, "n must be a positive integer")
        return int(n)

    def images(
        self, model: str, prompt: str, params: dict, image=None
    ) -> ImageResponse:
        if self.client is None:
            with httpx.Client(follow_redirects=True) as client:
                self.client = client
                return self.images(model, prompt, params, image)
        urls = []
        for item in (
            image if isinstance(image, list) else ([image] if image is not None else [])
        ):
            if isinstance(item, str) and item.startswith(("https://", "http://")):
                urls.append(item)
            else:
                urls.append(
                    self.request("post", UPLOAD_URL, self.upload_body(item))[
                        "downloadUrl"
                    ]
                )
        body = build_input(
            model,
            prompt or "",
            self.prepare(model, params, urls),
            edit=image is not None,
        )
        data, tasks = [], []
        for _ in range(self.count(params)):
            self.task_id = self.request(
                "post", f"{self.base}/jobs/createTask", body
            ).get("taskId")
            if not self.task_id:
                raise failure(502, "Kie createTask returned no taskId")
            task = self.poll()
            for url in result_urls(task):
                media = self.request("get", url, download=True)
                data.append(
                    {
                        "b64_json": base64.b64encode(media.content).decode(),
                        "mime_type": media.headers.get("content-type", "image/png"),
                    }
                )
            tasks.append(task)
        return self.response(model, data, tasks)

    async def aimages(
        self, model: str, prompt: str, params: dict, image=None
    ) -> ImageResponse:
        if self.client is None:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                self.client = client
                return await self.aimages(model, prompt, params, image)
        urls = []
        for item in (
            image if isinstance(image, list) else ([image] if image is not None else [])
        ):
            if isinstance(item, str) and item.startswith(("https://", "http://")):
                urls.append(item)
            else:
                urls.append(
                    (await self.arequest("post", UPLOAD_URL, self.upload_body(item)))[
                        "downloadUrl"
                    ]
                )
        body = build_input(
            model,
            prompt or "",
            self.prepare(model, params, urls),
            edit=image is not None,
        )
        data, tasks = [], []
        for _ in range(self.count(params)):
            self.task_id = (
                await self.arequest("post", f"{self.base}/jobs/createTask", body)
            ).get("taskId")
            if not self.task_id:
                raise failure(502, "Kie createTask returned no taskId")
            task = await self.apoll()
            for url in result_urls(task):
                media = await self.arequest("get", url, download=True)
                data.append(
                    {
                        "b64_json": base64.b64encode(media.content).decode(),
                        "mime_type": media.headers.get("content-type", "image/png"),
                    }
                )
            tasks.append(task)
        return self.response(model, data, tasks)


def execute_images(
    model,
    prompt,
    params,
    litellm_params,
    api_key=None,
    api_base=None,
    client=None,
    timeout=None,
    is_async=False,
    image=None,
):
    from .health_check import acheck_credit, check_credit, health_check_context

    if health_check_context.get():
        probe = acheck_credit if is_async else check_credit
        return probe(api_key or litellm_params.get("api_key"), model, client)
    budget = timeout if isinstance(timeout, (int, float)) else 270
    engine = TaskEngine(
        api_key or litellm_params.get("api_key"),
        api_base or litellm_params.get("api_base"),
        client,
        budget,
    )
    call = engine.aimages if is_async else engine.images
    return call(model, prompt, params, image)
