"""
Provider-agnostic AI client.

Abstracts over multiple LLM backends so the rest of the codebase only deals with
a single `generate(prompt) -> str` call.

Selection is driven by env vars:

- ``AI_PROVIDER``   one of ``gemini`` (default), ``openai``, ``anthropic``
- ``AI_MODEL``      optional model id override; falls back to a sane per-provider default
- Per-provider auth env vars (see each provider class)

Each provider lazily imports its SDK so users only need to install the one they
actually use (see ``pyproject.toml`` extras).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


DEFAULT_MODELS = {
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
}


class AIProvider(ABC):
    """Minimal text-in / text-out LLM interface."""

    name: str = ""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or os.getenv("AI_MODEL") or DEFAULT_MODELS[self.name]

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Send a single user prompt, return the model's text response."""


# --- Gemini (Google Gen AI / Vertex AI) -------------------------------------

class GeminiProvider(AIProvider):
    """
    Google Gen AI SDK. Supports both the Gemini Developer API (API key) and
    Vertex AI (ADC). Vertex defaults to the global endpoint to reduce
    regional 429 RESOURCE_EXHAUSTED errors.
    See https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations#global-endpoint
    """

    name = "gemini"

    def __init__(self, model: Optional[str] = None) -> None:
        # Backwards compat: honor legacy GEMINI_MODEL if AI_MODEL is unset.
        model = model or os.getenv("AI_MODEL") or os.getenv("GEMINI_MODEL")
        super().__init__(model)
        self._client = self._build_client()

    @staticmethod
    def _use_vertex() -> bool:
        return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")

    def _build_client(self):
        from google import genai  # lazy import

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

        if self._use_vertex():
            if api_key:
                return genai.Client(vertexai=True, api_key=api_key)
            kwargs: dict = {
                "vertexai": True,
                "location": os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            }
            project = os.getenv("GOOGLE_CLOUD_PROJECT")
            if project:
                kwargs["project"] = project
            return genai.Client(**kwargs)

        if not api_key:
            raise ValueError(
                "Missing API key: set GEMINI_API_KEY or GOOGLE_API_KEY, or "
                "enable Vertex AI with GOOGLE_GENAI_USE_VERTEXAI=true and "
                "Application Default Credentials (and GOOGLE_CLOUD_PROJECT)."
            )
        return genai.Client(api_key=api_key)

    def generate(self, prompt: str) -> str:
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
        )
        return response.text or ""


# --- OpenAI -----------------------------------------------------------------

class OpenAIProvider(AIProvider):
    """OpenAI Chat Completions. Compatible with any OpenAI-shaped API via ``OPENAI_BASE_URL``."""

    name = "openai"

    def __init__(self, model: Optional[str] = None) -> None:
        super().__init__(model)
        self._client = self._build_client()

    def _build_client(self):
        try:
            from openai import OpenAI  # lazy import
        except ImportError as e:
            raise ImportError(
                "openai SDK not installed. Install with: pip install 'smart-logger[openai]' "
                "or pip install openai"
            ) from e

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Missing OPENAI_API_KEY for OpenAI provider.")

        kwargs: dict = {"api_key": api_key}
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAI(**kwargs)

    def generate(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""


# --- Anthropic --------------------------------------------------------------

class AnthropicProvider(AIProvider):
    """Anthropic Claude via the Messages API."""

    name = "anthropic"

    def __init__(self, model: Optional[str] = None) -> None:
        super().__init__(model)
        self._client = self._build_client()
        self._max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))

    def _build_client(self):
        try:
            import anthropic  # lazy import
        except ImportError as e:
            raise ImportError(
                "anthropic SDK not installed. Install with: pip install 'smart-logger[anthropic]' "
                "or pip install anthropic"
            ) from e

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Missing ANTHROPIC_API_KEY for Anthropic provider.")
        return anthropic.Anthropic(api_key=api_key)

    def generate(self, prompt: str) -> str:
        message = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # Concatenate text blocks; Anthropic returns a list of content blocks.
        parts = [block.text for block in message.content if getattr(block, "type", "") == "text"]
        return "".join(parts)


# --- Factory ----------------------------------------------------------------

_PROVIDERS: dict[str, type[AIProvider]] = {
    "gemini": GeminiProvider,
    "google": GeminiProvider,  # alias
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,  # alias
}


def get_ai_client(provider: Optional[str] = None, model: Optional[str] = None) -> AIProvider:
    """
    Build an AI client. Resolution order for provider:
    explicit arg > ``AI_PROVIDER`` env > ``gemini`` (default).
    """
    name = (provider or os.getenv("AI_PROVIDER") or "gemini").lower().strip()
    if name not in _PROVIDERS:
        valid = ", ".join(sorted(set(_PROVIDERS.keys())))
        raise ValueError(f"Unknown AI provider '{name}'. Valid: {valid}.")
    return _PROVIDERS[name](model=model)


def generate(prompt: str) -> str:
    """Convenience one-shot using the default-configured provider."""
    return get_ai_client().generate(prompt)
