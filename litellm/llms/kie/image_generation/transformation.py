from litellm.llms.base_llm.image_generation.transformation import (
    BaseImageGenerationConfig,
)

from ..common_utils import API_BASE, auth, failure
from ..model_config import build_input


class KieImageGenerationConfig(BaseImageGenerationConfig):
    def get_supported_openai_params(self, model):
        return ["n", "quality", "response_format", "size", "background"]

    def map_openai_params(
        self, non_default_params, optional_params, model, drop_params
    ):
        return {**optional_params, **non_default_params}

    def validate_environment(
        self,
        headers,
        model,
        messages=None,
        optional_params=None,
        litellm_params=None,
        api_key=None,
        api_base=None,
    ):
        return auth(headers, api_key or (litellm_params or {}).get("api_key"))

    def get_complete_url(self, api_base=None, **kwargs):
        return f"{(api_base or API_BASE).rstrip('/')}/jobs/createTask"

    def transform_image_generation_request(
        self, model, prompt, optional_params, litellm_params, headers
    ):
        return build_input(model, prompt, optional_params)

    def transform_image_generation_response(self, **kwargs):
        raise NotImplementedError("Kie images require the task executor")

    def get_error_class(self, error_message, status_code, headers):
        return failure(status_code, error_message)
