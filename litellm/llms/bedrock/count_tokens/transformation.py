"""
AWS Bedrock CountTokens API transformation logic.

This module handles the transformation of requests from Anthropic Messages API format
to AWS Bedrock's CountTokens API format and vice versa.
"""

import re
from typing import Any, Dict, List, Optional

from litellm.llms.bedrock.base_aws_llm import BaseAWSLLM
from litellm.llms.bedrock.common_utils import get_bedrock_base_model


class BedrockCountTokensConfig(BaseAWSLLM):
    """
    Configuration and transformation logic for AWS Bedrock CountTokens API.

    AWS Bedrock CountTokens API Specification:
    - Endpoint: POST /model/{modelId}/count-tokens
    - Input formats: 'invokeModel' or 'converse'
    - Response: {"inputTokens": <number>}
    """

    def _detect_input_type(self, request_data: Dict[str, Any]) -> str:
        """
        Detect whether to use 'converse' or 'invokeModel' input format.

        Args:
            request_data: The original request data

        Returns:
            'converse' or 'invokeModel'
        """
        # If the request has messages in the expected Anthropic format, use converse
        if "messages" in request_data and isinstance(request_data["messages"], list):
            return "converse"

        # For raw text or other formats, use invokeModel
        # This handles cases where the input is prompt-based or already in raw Bedrock format
        return "invokeModel"

    def transform_anthropic_to_bedrock_count_tokens(
        self,
        request_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Transform request to Bedrock CountTokens format.
        Supports both Converse and InvokeModel input types.

        Input (Anthropic format):
        {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Hello!"}]
        }

        Output (Bedrock CountTokens format for Converse):
        {
            "input": {
                "converse": {
                    "messages": [...],
                    "system": [...] (if present)
                }
            }
        }

        Output (Bedrock CountTokens format for InvokeModel):
        {
            "input": {
                "invokeModel": {
                    "body": "{...raw model input...}"
                }
            }
        }
        """
        input_type = self._detect_input_type(request_data)

        if input_type == "converse":
            return self._transform_to_converse_format(request_data)
        else:
            return self._transform_to_invoke_model_format(request_data)

    def _transform_to_converse_format(
        self, request_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Transform to Converse input format, including system and tools."""
        messages = request_data.get("messages", [])
        system = request_data.get("system")
        tools = request_data.get("tools")

        # Transform messages. Bedrock Converse rejects empty content arrays, while
        # Anthropic-compatible clients can send empty historical turns. Empty
        # turns carry no token-bearing content, so omit them from CountTokens.
        user_messages = []
        for message in messages:
            transformed_content = self._transform_message_content(
                message.get("content", "")
            )
            if not transformed_content:
                continue
            transformed_message: Dict[str, Any] = {
                "role": message.get("role"),
                "content": transformed_content,
            }
            user_messages.append(transformed_message)

        converse_input: Dict[str, Any] = {"messages": user_messages}

        # Transform system prompt (string or list of blocks → Bedrock format)
        system_blocks = self._transform_system(system)
        if system_blocks:
            converse_input["system"] = system_blocks

        # Transform tools (Anthropic format → Bedrock toolConfig)
        tool_config = self._transform_tools(tools)
        if tool_config:
            converse_input["toolConfig"] = tool_config

        return {"input": {"converse": converse_input}}

    def _transform_message_content(self, content: Any) -> List[Dict[str, Any]]:
        transformed_content: List[Dict[str, Any]] = []

        if isinstance(content, str):
            if content:
                transformed_content.append({"text": content})
            return transformed_content

        if isinstance(content, list):
            for block in content:
                transformed_block = self._transform_content_block(block)
                if transformed_block:
                    transformed_content.append(transformed_block)
            return transformed_content

        if content is None:
            return transformed_content

        transformed_content.append({"text": str(content)})
        return transformed_content

    def _transform_content_block(self, block: Any) -> Optional[Dict[str, Any]]:
        if isinstance(block, str):
            return {"text": block} if block else None
        if not isinstance(block, dict):
            return {"text": str(block)}

        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            return {"text": text} if text else None
        if block_type == "image":
            return self._transform_anthropic_image_block(block)
        if block_type == "document":
            return self._transform_anthropic_document_block(block)
        if block_type == "thinking":
            thinking = block.get("thinking", "")
            return {"text": thinking} if thinking else None

        # Already in Bedrock Converse shape or a future provider-native block.
        if any(
            key in block
            for key in ("text", "image", "document", "toolUse", "toolResult")
        ):
            return block

        text = self._extract_text_from_unknown_block(block)
        return {"text": text} if text else None

    def _transform_anthropic_image_block(
        self, block: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        source = block.get("source")
        if not isinstance(source, dict):
            return None

        source_type = source.get("type")
        if source_type == "base64":
            media_type = source.get("media_type", "")
            image_format = self._format_from_media_type(media_type)
            data = source.get("data")
            if image_format and data:
                return {
                    "image": {
                        "format": image_format,
                        "source": {"bytes": data},
                    }
                }

        return None

    def _transform_anthropic_document_block(
        self, block: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            media_type = source.get("media_type", "")
            document_format = self._format_from_media_type(media_type)
            data = source.get("data")
            if document_format and data:
                return {
                    "document": {
                        "format": document_format,
                        "name": block.get("title") or "Document",
                        "source": {"bytes": data},
                    }
                }

        text = self._extract_text_from_unknown_block(block)
        return {"text": text} if text else None

    @staticmethod
    def _format_from_media_type(media_type: str) -> Optional[str]:
        if not media_type or "/" not in media_type:
            return None
        media_format = media_type.rsplit("/", 1)[1].lower()
        if media_format == "jpg":
            return "jpeg"
        if media_format == "plain":
            return "txt"
        return media_format

    @staticmethod
    def _extract_text_from_unknown_block(block: Dict[str, Any]) -> str:
        text_parts: List[str] = []
        for key in ("text", "content", "input", "query", "name", "title", "context"):
            value = block.get(key)
            if isinstance(value, str) and value:
                text_parts.append(value)
        return "\n".join(text_parts)

    def _transform_system(self, system: Optional[Any]) -> List[Dict[str, Any]]:
        """Transform Anthropic system prompt to Bedrock system blocks."""
        if system is None:
            return []
        if isinstance(system, str):
            return [{"text": system}]
        if isinstance(system, list):
            # Already in blocks format (e.g. [{"type": "text", "text": "..."}])
            return [
                {"text": block.get("text", "")}
                for block in system
                if isinstance(block, dict)
            ]
        return []

    def _transform_tools(
        self, tools: Optional[List[Dict[str, Any]]]
    ) -> Optional[Dict[str, Any]]:
        """Transform Anthropic tools to Bedrock toolConfig format."""
        if not tools:
            return None

        bedrock_tools = []
        for tool in tools:
            name = tool.get("name", "")
            # Bedrock tool names must match [a-zA-Z][a-zA-Z0-9_]* and max 64 chars
            name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            if name and not name[0].isalpha():
                name = "t_" + name
            name = name[:64]

            description = tool.get("description") or name
            input_schema = tool.get(
                "input_schema", {"type": "object", "properties": {}}
            )

            bedrock_tools.append(
                {
                    "toolSpec": {
                        "name": name,
                        "description": description,
                        "inputSchema": {"json": input_schema},
                    }
                }
            )

        return {"tools": bedrock_tools}

    def _transform_to_invoke_model_format(
        self, request_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Transform to InvokeModel input format."""
        import json

        # For InvokeModel, we need to provide the raw body that would be sent to the model
        # Remove the 'model' field from the body as it's not part of the model input
        body_data = {k: v for k, v in request_data.items() if k != "model"}

        return {"input": {"invokeModel": {"body": json.dumps(body_data)}}}

    def get_bedrock_count_tokens_endpoint(
        self,
        model: str,
        aws_region_name: str,
        api_base: Optional[str] = None,
        aws_bedrock_runtime_endpoint: Optional[str] = None,
    ) -> str:
        """
        Construct the AWS Bedrock CountTokens API endpoint using existing LiteLLM functions.

        Args:
            model: The resolved model ID from router lookup
            aws_region_name: AWS region (e.g., "eu-west-1")
            api_base: Optional custom API base URL (takes highest priority)
            aws_bedrock_runtime_endpoint: Optional custom Bedrock runtime endpoint

        Returns:
            Complete endpoint URL for CountTokens API
        """
        # Use existing LiteLLM function to get the base model ID (removes region prefix)
        model_id = get_bedrock_base_model(model)

        # Remove bedrock/ prefix if present
        if model_id.startswith("bedrock/"):
            model_id = model_id[8:]  # Remove "bedrock/" prefix

        base_url, _ = self.get_runtime_endpoint(
            api_base=api_base,
            aws_bedrock_runtime_endpoint=aws_bedrock_runtime_endpoint,
            aws_region_name=aws_region_name,
        )
        endpoint = f"{base_url}/model/{model_id}/count-tokens"

        return endpoint

    def transform_bedrock_response_to_anthropic(
        self, bedrock_response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Transform Bedrock CountTokens response to Anthropic format.

        Input (Bedrock response):
        {
            "inputTokens": 123
        }

        Output (Anthropic format):
        {
            "input_tokens": 123
        }
        """
        input_tokens = bedrock_response.get("inputTokens", 0)

        return {"input_tokens": input_tokens}

    def validate_count_tokens_request(self, request_data: Dict[str, Any]) -> None:
        """
        Validate the incoming count tokens request.
        Supports both Converse and InvokeModel input formats.

        Args:
            request_data: The request payload

        Raises:
            ValueError: If the request is invalid
        """
        if not request_data.get("model"):
            raise ValueError("model parameter is required")

        input_type = self._detect_input_type(request_data)

        if input_type == "converse":
            # Validate Converse format (messages-based)
            messages = request_data.get("messages", [])
            if not messages:
                raise ValueError("messages parameter is required for Converse input")

            if not isinstance(messages, list):
                raise ValueError("messages must be a list")

            for i, message in enumerate(messages):
                if not isinstance(message, dict):
                    raise ValueError(f"Message {i} must be a dictionary")

                if "role" not in message:
                    raise ValueError(f"Message {i} must have a 'role' field")

                if "content" not in message:
                    raise ValueError(f"Message {i} must have a 'content' field")
        else:
            # For InvokeModel format, we need at least some content to count tokens
            # The content structure varies by model, so we do minimal validation
            if len(request_data) <= 1:  # Only has 'model' field
                raise ValueError("Request must contain content to count tokens")
