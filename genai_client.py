"""
Google Gen AI client configuration.

Vertex AI: default location is ``global`` to use the global endpoint and reduce
429 RESOURCE_EXHAUSTED errors from single-region capacity limits.
See https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations#global-endpoint
"""

import os

from dotenv import load_dotenv
from google import genai

load_dotenv()


def _use_vertex_from_env() -> bool:
    return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")


def get_genai_client() -> genai.Client:
    """
    Build a client for the Gemini Developer API or Vertex AI (via env / ADC).

    Vertex with Application Default Credentials: defaults ``location`` to
    ``global`` when ``GOOGLE_CLOUD_LOCATION`` is unset. Override with
    ``GOOGLE_CLOUD_LOCATION=us-central1`` (or another region) if needed.
    """
    use_vertex = _use_vertex_from_env()
    gemini_key = os.getenv("GEMINI_API_KEY")
    google_key = os.getenv("GOOGLE_API_KEY")
    api_key = gemini_key or google_key

    if use_vertex:
        if api_key:
            return genai.Client(vertexai=True, api_key=api_key)
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
        kwargs: dict = {"vertexai": True, "location": location}
        if project:
            kwargs["project"] = project
        return genai.Client(**kwargs)

    if not api_key:
        raise ValueError(
            "Missing API key: set GEMINI_API_KEY or GOOGLE_API_KEY, or enable "
            "Vertex AI with GOOGLE_GENAI_USE_VERTEXAI=true and Application "
            "Default Credentials (and GOOGLE_CLOUD_PROJECT for quota)."
        )
    return genai.Client(api_key=api_key)


def default_gemini_model() -> str:
    """Model id for generate_content; override with GEMINI_MODEL."""
    return os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
