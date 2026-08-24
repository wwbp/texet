"""Every text-generating path defaults to Bedrock Llama.

The app had OpenAI defaults scattered across four places — _generate_reply's
arguments, the fallback used when no system_prompts row exists, the SystemPrompt
column defaults, and the console form. Prod's active row happened to say bedrock,
so the OpenAI defaults were invisible right up until something fell back to one.
These tests name each place so a silent reversion fails here.
"""

import inspect

from app.config import BEDROCK_DEFAULT_MODEL, DEFAULT_LLM_PROVIDER
from app.models.response import SystemPrompt
from app.response.service import _generate_reply
from app.summary.service import SUMMARY_MODEL_ID, SUMMARY_PROVIDER

_LLAMA = "us.meta.llama4-maverick-17b-instruct-v1:0"


def test_configured_default_is_bedrock_llama():
    assert DEFAULT_LLM_PROVIDER == "bedrock"
    assert BEDROCK_DEFAULT_MODEL == _LLAMA


def test_generate_reply_arguments_default_to_bedrock_llama():
    """The defaults the summariser used to inherit by accident."""
    params = inspect.signature(_generate_reply).parameters
    assert params["provider"].default == "bedrock"
    assert params["model_id"].default == _LLAMA


def test_system_prompt_columns_default_to_bedrock_llama():
    """Covers a row inserted without an explicit provider/model."""
    row = SystemPrompt(prompt="anything")
    assert row.provider is None or row.provider == "bedrock"

    provider_col = SystemPrompt.__table__.c.provider
    model_col = SystemPrompt.__table__.c.model_id
    assert provider_col.default.arg == "bedrock"
    assert model_col.default.arg == _LLAMA
    assert provider_col.server_default.arg == "bedrock"
    assert model_col.server_default.arg == _LLAMA


def test_console_form_defaults_to_bedrock_llama():
    from app.console.system_prompts import _MODEL_DEFAULTS

    assert _MODEL_DEFAULTS["bedrock"] == _LLAMA


def test_summary_pin_agrees_with_the_default_but_stays_its_own_choice():
    """They match today. The pin is still a separate literal on purpose: moving
    the reply model should not drag a running study's summaries along with it."""
    assert (SUMMARY_PROVIDER, SUMMARY_MODEL_ID) == (DEFAULT_LLM_PROVIDER, BEDROCK_DEFAULT_MODEL)
