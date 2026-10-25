import os
from typing import Tuple

import litellm


def is_reasoning_auto_summary_enabled() -> bool:
    """Check whether the default 'summary: detailed' injection is enabled (opt-in)."""
    return (
        litellm.reasoning_auto_summary
        or os.getenv("LITELLM_REASONING_AUTO_SUMMARY", "false").lower() == "true"
    )


def extract_cache_tokens(usage) -> Tuple[int, int]:
    """Extract (cache_creation, cache_read) from a Usage object.

    Checks the Anthropic-format private attrs first, then falls back to
    OpenAI-format prompt_tokens_details fields.  Returns (0, 0) when
    neither source has data.
    """
    cache_creation = getattr(usage, "_cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "_cache_read_input_tokens", 0) or 0

    ptd = getattr(usage, "prompt_tokens_details", None)
    if ptd:
        if not cache_creation:
            cache_creation = getattr(ptd, "cache_creation_tokens", 0) or 0
        if not cache_read:
            cache_read = getattr(ptd, "cached_tokens", 0) or 0

    return cache_creation, cache_read
