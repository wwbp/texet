from app.console import api_keys, exports, home, system_prompts  # noqa: F401
from app.console.admin_ui import init_console
from app.console.core import console_router

__all__ = ["console_router", "init_console"]
