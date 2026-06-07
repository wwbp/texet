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
        "Base.\n\n[Daily Activity]\nDay 5 activity.\n\n[Previous week summary]\nWeek 1 summary."
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


def test_opening_message_included() -> None:
    result = compose_instruction_prompt("Base.", opening_message="Hi! I'm your study buddy.")
    assert result == "Base.\n\n[Opening message]\nHi! I'm your study buddy."


def test_opening_message_none_excluded() -> None:
    result = compose_instruction_prompt("Base.", opening_message=None)
    assert "[Opening message]" not in result
    assert result == "Base."


def test_opening_message_empty_excluded() -> None:
    result = compose_instruction_prompt("Base.", opening_message="   ")
    assert "[Opening message]" not in result


def test_opening_message_before_daily_and_weekly() -> None:
    result = compose_instruction_prompt(
        "Base.",
        opening_message="Hello!",
        daily_content="Day 1.",
        weekly_summary="Week 1.",
    )
    assert result == (
        "Base.\n\n[Opening message]\nHello!\n\n[Daily Activity]\nDay 1.\n\n[Previous week summary]\nWeek 1."
    )


def test_user_local_time_included() -> None:
    result = compose_instruction_prompt("Base.", user_local_time="2026-06-07T14:30:00-05:00")
    assert "[User's Local Time]" in result
    assert "Sunday, June 7, 2026 at 2:30 PM (UTC-5)" in result
    assert "time of day" in result


def test_user_local_time_none_excluded() -> None:
    result = compose_instruction_prompt("Base.", user_local_time=None)
    assert "[User's Local Time]" not in result
    assert result == "Base."


def test_user_local_time_empty_excluded() -> None:
    result = compose_instruction_prompt("Base.", user_local_time="   ")
    assert "[User's Local Time]" not in result


def test_user_local_time_invalid_excluded() -> None:
    result = compose_instruction_prompt("Base.", user_local_time="not-a-date")
    assert "[User's Local Time]" not in result


def test_user_local_time_utc_offset() -> None:
    result = compose_instruction_prompt("Base.", user_local_time="2026-01-15T09:00:00+05:30")
    assert "UTC+5:30" in result


def test_user_local_time_utc_zero() -> None:
    result = compose_instruction_prompt("Base.", user_local_time="2026-06-07T12:00:00+00:00")
    assert "UTC+0" in result


def test_user_local_time_after_weekly_summary() -> None:
    result = compose_instruction_prompt(
        "Base.",
        daily_content="Day 1.",
        weekly_summary="Week 1.",
        user_local_time="2026-06-07T14:30:00-05:00",
    )
    weekly_pos = result.index("[Previous week summary]")
    time_pos = result.index("[User's Local Time]")
    assert time_pos > weekly_pos
