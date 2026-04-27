from __future__ import annotations


def compose_instruction_prompt(
    base: str,
    daily_content: str | None = None,
    weekly_summary: str | None = None,
) -> str:
    parts = [base.strip()]
    if daily_content and daily_content.strip():
        parts.append(f"[Daily Activity]\n{daily_content.strip()}")
    if weekly_summary and weekly_summary.strip():
        parts.append(f"[Previous week summary]\n{weekly_summary.strip()}")
    return "\n\n".join(parts)
