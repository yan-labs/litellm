import json
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union

from pydantic import BaseModel
from typing_extensions import TypedDict


class OpenRouterErrorMessage(TypedDict):
    message: str
    code: int
    metadata: Dict


class OpenRouterImageCompletionTokensDetails(BaseModel):
    """``usage.completion_tokens_details`` on ``POST /api/v1/images`` responses."""

    image_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None


class OpenRouterImagePromptTokensDetails(BaseModel):
    """``usage.prompt_tokens_details`` on ``POST /api/v1/images`` responses (image edit only)."""

    image_tokens: Optional[int] = None
    text_tokens: Optional[int] = None


class OpenRouterImageUsage(BaseModel):
    """Usage block of the OpenRouter unified image API response, incl. real cost for spend-log settlement."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: Optional[float] = None
    cost_details: Optional[Mapping[str, Optional[float]]] = None
    completion_tokens_details: Optional[OpenRouterImageCompletionTokensDetails] = None
    prompt_tokens_details: Optional[OpenRouterImagePromptTokensDetails] = None


class OpenRouterImageData(BaseModel):
    """One image entry in ``data[]``; OpenRouter always returns base64, never a bare URL, but both fields are modeled defensively."""

    b64_json: Optional[str] = None
    url: Optional[str] = None
    media_type: Optional[str] = None
    revised_prompt: Optional[str] = None


class OpenRouterImagesResponse(BaseModel):
    """
    Response body of ``POST https://openrouter.ai/api/v1/images``.

    Used by litellm/llms/openrouter/image_api.py to validate and parse both image
    generation and image edit responses, since both request types answer through this
    same endpoint and payload shape.
    """

    data: Tuple[OpenRouterImageData, ...]
    created: Optional[int] = None
    model: Optional[str] = None
    usage: Optional[OpenRouterImageUsage] = None
