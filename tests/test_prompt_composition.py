from app.response.prompt import compose_instruction_prompt


def test_base_only() -> None:
    result = compose_instruction_prompt("You are helpful.")
    assert result == "You are helpful."


def test_base_with_daily() -> None:
    result = compose_instruction_prompt("Base.", daily_content="Do exercise today.")
    assert result == "Base.\n\n[Daily Activity]\nDo exercise today."


def test_base_with_weekly_summary() -> None:
    result = compose_instruction_prompt("Base.", weekly_summary="Last week: user walked 3 miles.")
    assert result == "Base.\n\n[Previous week summary]\nLast week: user walked 3 miles."


def test_base_with_all_sections() -> None:
    result = compose_instruction_prompt(
        "Base.",
        daily_content="Day 5 activity.",
        weekly_summary="Week 1 summary.",
    )
    assert result == (
        "Base.\n\n"
        "[Daily Activity]\nDay 5 activity.\n\n"
        "[Previous week summary]\nWeek 1 summary."
    )


def test_none_daily_excluded() -> None:
    result = compose_instruction_prompt("Base.", daily_content=None)
    assert "[Daily Activity]" not in result
    assert result == "Base."


def test_none_weekly_excluded() -> None:
    result = compose_instruction_prompt("Base.", weekly_summary=None)
    assert "[Previous week summary]" not in result
    assert result == "Base."


def test_empty_string_daily_excluded() -> None:
    result = compose_instruction_prompt("Base.", daily_content="   ")
    assert "[Daily Activity]" not in result


def test_empty_string_weekly_excluded() -> None:
    result = compose_instruction_prompt("Base.", weekly_summary="")
    assert "[Previous week summary]" not in result


def test_base_whitespace_stripped() -> None:
    result = compose_instruction_prompt("  Base.  ")
    assert result == "Base."


def test_sections_whitespace_stripped() -> None:
    result = compose_instruction_prompt(
        "Base.",
        daily_content="  Day activity.  ",
        weekly_summary="  Week summary.  ",
    )
    assert "[Daily Activity]\nDay activity." in result
    assert "[Previous week summary]\nWeek summary." in result
