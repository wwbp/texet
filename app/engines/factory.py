from __future__ import annotations

from kani.engines.base import BaseEngine  # type: ignore[import-untyped]
from kani.engines.openai import OpenAIEngine  # type: ignore[import-untyped]

from app.config import (
    get_aws_access_key_id,
    get_aws_region,
    get_aws_secret_access_key,
    get_openai_api_key,
)
from app.engines.bedrock import BedrockEngine


def create_engine(provider: str, model_id: str) -> BaseEngine:
    if provider == "openai":
        api_key = get_openai_api_key()
        if not api_key:
            raise ValueError("Missing OPENAI_API_KEY.")
        return OpenAIEngine(api_key=api_key, model=model_id)
    if provider == "bedrock":
        return BedrockEngine(
            model_id=model_id,
            aws_access_key_id=get_aws_access_key_id(),
            aws_secret_access_key=get_aws_secret_access_key(),
            region_name=get_aws_region(),
        )
    raise ValueError(f"Unsupported provider: {provider!r}")
