"""Operator-editable instruction template: substitution and paragraph-drop rules."""

from __future__ import annotations

from app.response.prompt import (
    DEFAULT_INSTRUCTION_TEMPLATE,
    HISTORY_CONVENTIONS,
    compose_instruction_prompt,
)


def test_default_template_is_used_when_none_passed() -> None:
    explicit = compose_instruction_prompt("Base.", template=DEFAULT_INSTRUCTION_TEMPLATE)
    implicit = compose_instruction_prompt("Base.")
    assert explicit == implicit


def test_default_template_declares_every_section() -> None:
    for placeholder in ("{base}", "{day_suffix}", "{daily_content}", "{weekly_summary}"):
        assert placeholder in DEFAULT_INSTRUCTION_TEMPLATE
    assert HISTORY_CONVENTIONS in DEFAULT_INSTRUCTION_TEMPLATE


def test_custom_template_replaces_layout() -> None:
    template = "SYSTEM: {base}\n\nSUMMARY: {weekly_summary}"
    result = compose_instruction_prompt(
        "Be kind.", weekly_summary="User walked 3 miles.", template=template
    )
    assert result == "SYSTEM: Be kind.\n\nSUMMARY: User walked 3 miles."


def test_paragraph_dropped_when_gating_placeholder_empty() -> None:
    template = "SYSTEM: {base}\n\nSUMMARY: {weekly_summary}"
    result = compose_instruction_prompt("Be kind.", weekly_summary=None, template=template)
    assert result == "SYSTEM: Be kind."


def test_day_suffix_is_decorative_and_does_not_drop_its_paragraph() -> None:
    template = "{base}\n\n[Activity{day_suffix}]\n{daily_content}"
    result = compose_instruction_prompt("Base.", daily_content="Walk.", template=template)
    assert result == "Base.\n\n[Activity]\nWalk."


def test_day_suffix_renders_day_number() -> None:
    template = "{base}\n\n[Activity{day_suffix}]\n{daily_content}"
    result = compose_instruction_prompt(
        "Base.", daily_content="Walk.", day_number=26, template=template
    )
    assert result == "Base.\n\n[Activity (Day 26)]\nWalk."


def test_day_number_alone_still_drops_the_paragraph() -> None:
    template = "{base}\n\n[Activity{day_suffix}]\n{daily_content}"
    result = compose_instruction_prompt("Base.", day_number=26, template=template)
    assert result == "Base."


def test_operator_can_reorder_sections() -> None:
    template = "{weekly_summary}\n\n{base}"
    result = compose_instruction_prompt("Base.", weekly_summary="Week 1.", template=template)
    assert result == "Week 1.\n\nBase."


def test_unknown_placeholder_is_left_literal() -> None:
    template = "{base}\n\nBudget: {not_a_real_placeholder}"
    result = compose_instruction_prompt("Base.", template=template)
    assert result == "Base.\n\nBudget: {not_a_real_placeholder}"


def test_literal_braces_survive_substitution() -> None:
    template = '{base}\n\nReply as JSON: {{"mood": "calm"}}'
    result = compose_instruction_prompt("Base.", template=template)
    assert result == 'Base.\n\nReply as JSON: {{"mood": "calm"}}'


def test_paragraph_with_two_gating_placeholders_drops_if_either_empty() -> None:
    template = "{base}\n\n{daily_content} / {weekly_summary}"
    both = compose_instruction_prompt(
        "Base.", daily_content="Day.", weekly_summary="Week.", template=template
    )
    assert both == "Base.\n\nDay. / Week."

    one = compose_instruction_prompt("Base.", daily_content="Day.", template=template)
    assert one == "Base."


def test_formatted_time_placeholder_receives_human_readable_time() -> None:
    template = "{base}\n\nNow: {formatted_time}"
    result = compose_instruction_prompt(
        "Base.", user_local_time="2026-06-07T14:30:00-05:00", template=template
    )
    assert result == "Base.\n\nNow: Sunday, June 7, 2026 at 2:30 PM (UTC-5)"


def test_unparseable_time_drops_its_paragraph() -> None:
    template = "{base}\n\nNow: {formatted_time}"
    result = compose_instruction_prompt("Base.", user_local_time="not-a-date", template=template)
    assert result == "Base."


def test_blank_template_falls_back_to_default() -> None:
    assert compose_instruction_prompt("Base.", template="   ") == compose_instruction_prompt(
        "Base."
    )


def test_extra_blank_lines_between_paragraphs_are_normalized() -> None:
    template = "{base}\n\n\n\n{weekly_summary}"
    result = compose_instruction_prompt("Base.", weekly_summary="Week.", template=template)
    assert result == "Base.\n\nWeek."
