"""
LiteLLM-backed client wrapper for provider-agnostic text generation.
"""

from dataclasses import dataclass
from typing import Any, Optional, List, Dict

from litellm import acompletion


class LLMError(RuntimeError):
    """Raised when the upstream LLM call fails or returns an invalid payload."""


@dataclass
class LiteLLMClient:
    model: str
    api_key: str
    provider: Optional[str] = None
    api_base: Optional[str] = None
    timeout_seconds: int = 30

    async def generate(
        self,
        *,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        max_tokens: int,
    ) -> str:
        request_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *messages,
        ]

        request_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": request_messages,
            "max_tokens": max_tokens,
            "api_key": self.api_key,
            "timeout": self.timeout_seconds,
        }
        if self.provider:
            request_kwargs["custom_llm_provider"] = self.provider
        if self.api_base:
            request_kwargs["api_base"] = self.api_base

        try:
            response = await acompletion(**request_kwargs)
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError("LLM response did not contain message content") from exc

        text = _coerce_text(content)
        if not text.strip():
            raise LLMError("LLM response content was empty")
        return text


def _coerce_text(content: Any) -> str:
    """Normalize provider-specific content blocks into plain text."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)

    return str(content)
