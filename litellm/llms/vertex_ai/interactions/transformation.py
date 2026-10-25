"""Vertex Interactions transport for managed Omni routes.

Omni editing and extension are not exposed by this generate-only integration.
The proxy/router supplies the existing named Vertex credential. Google AI
Studio's JSON contract is reused, while authentication and global URLs use
Vertex; no Gemini API key or alternate supplier route is accepted.
"""

from typing import Optional
from urllib.parse import quote

from litellm.llms.gemini.interactions.transformation import GoogleAIStudioInteractionsConfig
from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
from litellm.types.utils import LlmProviders


class VertexAIInteractionsConfig(GoogleAIStudioInteractionsConfig, VertexBase):
    """Reuse Interactions JSON with the gateway's named Vertex credentials."""

    def __init__(self):
        """Initialize the existing Vertex token cache used by managed routes."""
        VertexBase.__init__(self)

    @property
    def custom_llm_provider(self) -> LlmProviders:
        """Keep provider identity Vertex for routing and error reporting."""
        return LlmProviders.VERTEX_AI

    @property
    def api_version(self) -> str:
        """Use the Cloud Interactions API version, independent of AI Studio."""
        return "v1beta1"

    def validate_environment(self, headers: dict, model: str, litellm_params) -> dict:
        """Authenticate using credentials already injected by the router."""
        params = dict(litellm_params or {})
        token, _ = self.get_access_token(
            credentials=self.safe_get_vertex_ai_credentials(params),
            project_id=self.safe_get_vertex_ai_project(params),
        )
        return {**(headers or {}), "Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    def get_complete_url(self, api_base: Optional[str], model: Optional[str], agent=None,
                         litellm_params=None, stream=None) -> str:
        """Build the global Cloud URL from the existing route project."""
        project = self.safe_get_vertex_ai_project(dict(litellm_params or {}))
        if not project:
            _, project = self.get_access_token(
                credentials=self.safe_get_vertex_ai_credentials(dict(litellm_params or {})),
                project_id=None,
            )
        if not project:
            raise ValueError("Vertex Interactions requires the existing credential's project")
        return f"https://aiplatform.googleapis.com/v1beta1/projects/{quote(project, safe='')}/locations/global/interactions"

    def transform_request(self, model, agent, input, optional_params, litellm_params, headers):
        """Use the Cloud preview id without the router's Vertex provider prefix."""
        return super().transform_request(
            model.removeprefix("vertex_ai/") if model else model,
            agent, input, optional_params, litellm_params, headers,
        )

    def transform_get_interaction_request(self, interaction_id, api_base, litellm_params, headers):
        """Retrieve a stored interaction through the same managed route."""
        return f"{self.get_complete_url(None, None, litellm_params=dict(litellm_params))}/{quote(interaction_id, safe='')}", {}

    def transform_response(self, model, raw_response, logging_obj):
        """Expose Cloud model output steps through the existing outputs contract."""
        response = super().transform_response(model, raw_response, logging_obj)
        response.outputs = [content for step in raw_response.json().get("steps", [])
                            if step.get("type") == "model_output" for content in step.get("content", [])] or response.outputs
        return response

    def transform_get_interaction_response(self, raw_response, logging_obj):
        """Preserve video bytes and modality usage on terminal poll responses."""
        return self.transform_response(None, raw_response, logging_obj)
