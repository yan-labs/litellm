from litellm.llms.base_llm.image_edit.transformation import BaseImageEditConfig

from ..common_utils import API_BASE, auth, failure
from ..model_config import build_input


class KieImageEditConfig(BaseImageEditConfig):
    def get_supported_openai_params(self, model):
        return ["n", "quality", "response_format", "size", "background"]

    def map_openai_params(self, image_edit_optional_params, model, drop_params):
        return dict(image_edit_optional_params)

    def validate_environment(self, headers, model, api_key=None):
        return auth(headers, api_key)

    def get_complete_url(self, model, api_base, litellm_params):
        return f"{(api_base or API_BASE).rstrip('/')}/jobs/createTask"

    def use_multipart_form_data(self):
        return False

    def transform_image_edit_request(
        self,
        model,
        prompt,
        image,
        image_edit_optional_request_params,
        litellm_params,
        headers,
    ):
        # The executor uploads multipart inputs before building this body.
        return build_input(
            model, prompt, image_edit_optional_request_params, edit=True
        ), []

    def transform_image_edit_response(self, model, raw_response, logging_obj):
        raise NotImplementedError("Kie edits require the task executor")

    def get_error_class(self, error_message, status_code, headers):
        return failure(status_code, error_message)
