"""Offline Argon protocol checks using the gateway's actual parameter converters.

Extract only the relevant class methods so this check needs no provider credentials
or full gateway dependencies; it verifies legacy and Argon request payloads together.
"""
import ast
from pathlib import Path
from typing import Optional
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py'

class ArgonThinkingProtocolTest(unittest.TestCase):
    """Ensure managed Argon requests never contain the rejected thinkingBudget."""

    def test_thinking_levels_preserve_old_protocol(self):
        """Reuse actual converters to compare Gemini 2.5, 3.8, and Argon payloads."""
        tree = ast.parse(SOURCE.read_text())
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'VertexGeminiConfig')
        names = {'_is_gemini_3_or_newer', '_map_thinking_param', '_is_thinking_budget_zero', '_map_reasoning_effort_to_thinking_level'}
        owner.body = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name in names]
        owner.bases = []
        scope = {'Optional': Optional, 'AnthropicThinkingParam': dict, 'GeminiThinkingConfig': dict}
        exec(compile(ast.Module(body=[owner], type_ignores=[]), str(SOURCE), 'exec'), scope)
        config = scope['VertexGeminiConfig']
        enabled = {'type': 'enabled', 'budget_tokens': 2048}
        self.assertEqual(config._map_thinking_param(enabled, 'gemini-2.5-pro')['thinkingBudget'], 2048)
        for model in ['gemini-3.8-flash', 'gemini-4-argon']:
            payload = config._map_thinking_param(enabled, model)
            self.assertNotIn('thinkingBudget', payload)
            self.assertEqual(payload['thinkingLevel'], 'low')
        for effort in ['low', 'medium', 'high']:
            self.assertEqual(config._map_reasoning_effort_to_thinking_level(effort, 'gemini-4-argon')['thinkingLevel'], effort)

if __name__ == '__main__':
    unittest.main()
