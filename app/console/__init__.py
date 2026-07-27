from app.console import (  # noqa: F401
    api_keys,
    daily_prompts,
    exports,
    home,
    prompt_templates,
    summarization_prompts,
    system_prompts,
)
from app.console.admin_ui import init_console
from app.console.core import console_router

__all__ = ["console_router", "init_console"]
