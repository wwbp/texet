"""Unit tests for strip_bracketed_segments — pure string work, no DB.

The corpus behind these cases is prod: of 1272 bot utterances in bot-prod,
five carried a bracketed segment and every one of them was already 'sent'.
Two families showed up — day markers the model echoed back out of the chat
history, and prompt scaffolding it leaked verbatim. Participants sent none,
so a bracket in a bot reply is an artifact, not content.
"""

import pytest

from app.response.utils import day_marker, strip_bracketed_segments

# ---------------------------------------------------------------------------
# The prod strings. Kept verbatim so a regression is recognisable.
# ---------------------------------------------------------------------------

PROD_ECHOED_MARKER = (
    "\"Thanks\" is a simple but meaningful word. It's great that you're able to "
    "express your appreciation to those who make a positive impact in your life.\n"
    "\n"
    "[Friday, August 21]\n"
    "Good morning! Everyone has days that feel more meaningful than others."
)

PROD_ECHOED_MARKER_PRUNED = (
    "\"Thanks\" is a simple but meaningful word. It's great that you're able to "
    "express your appreciation to those who make a positive impact in your life.\n"
    "\n"
    "Good morning! Everyone has days that feel more meaningful than others."
)

PROD_LEAKED_SCAFFOLDING = (
    "I'm glad you're interested in the prompt. The opening message and daily "
    "activity examples I provided earlier are based on the following prompt:\n"
    "\n"
    '"[Opening message]\n'
    "Good morning! Remember, you can text me anytime.\n"
    "\n"
    "[Daily Activity]\n"
    'Name one thing you are grateful for."'
)


# ---------------------------------------------------------------------------
# The two prod families
# ---------------------------------------------------------------------------


def test_echoed_day_marker_is_removed_with_its_line():
    """The marker occupied a whole line; the line goes with it.

    Deleting the token alone would leave "...your life.\n\n\nGood morning!" —
    an orphan blank line where the marker used to be.
    """
    assert strip_bracketed_segments(PROD_ECHOED_MARKER) == PROD_ECHOED_MARKER_PRUNED


def test_leaked_scaffolding_labels_are_removed():
    result = strip_bracketed_segments(PROD_LEAKED_SCAFFOLDING)
    assert "[Opening message]" not in result
    assert "[Daily Activity]" not in result
    # The surrounding prose survives intact.
    assert "Good morning! Remember, you can text me anytime." in result
    assert "Name one thing you are grateful for." in result


@pytest.mark.parametrize(
    "placeholder",
    ["[specific task or situation]", "[specific area]", "[problem]"],
)
def test_unfilled_placeholder_slots_are_removed(placeholder):
    """Mid-sentence slots leave no doubled space behind."""
    result = strip_bracketed_segments(f"Try naming the {placeholder} out loud.")
    assert result == "Try naming the out loud."


def test_every_marker_day_marker_can_produce_is_pruned():
    """Guards the real coupling: whatever day_marker emits, this removes."""
    import datetime

    for offset in range(0, 366, 29):
        date = datetime.date(2026, 1, 1) + datetime.timedelta(days=offset)
        marker = day_marker(date)
        assert strip_bracketed_segments(f"{marker}\nGood morning!") == "Good morning!"


# ---------------------------------------------------------------------------
# Bounded matching: a bracket must open and close on one line
# ---------------------------------------------------------------------------


def test_unclosed_bracket_is_left_alone():
    """An unmatched '[' is prose, not a marker. Eating to end-of-string would
    silently truncate a real reply — far worse than leaving one stray char."""
    text = "I felt [ overwhelmed today, but it passed."
    assert strip_bracketed_segments(text) == text


def test_match_does_not_span_a_newline():
    """A '[' with no closer on its line must not swallow the paragraph below."""
    text = "Consider this [ unclosed\n\nA whole second paragraph survives.]"
    assert strip_bracketed_segments(text) == text


def test_two_segments_on_one_line_are_both_removed():
    assert strip_bracketed_segments("a [one] b [two] c") == "a b c"


def test_matching_is_not_greedy_across_segments():
    """A greedy '\\[.*\\]' would eat 'keep' along with both brackets."""
    assert strip_bracketed_segments("[drop] keep [drop]") == "keep"


# ---------------------------------------------------------------------------
# Text that must survive untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Sleep was rough but I managed.",
        "I rated it 7/10 today.",
        "Costs $5 (about the same as before).",
        "",
        "   ",
    ],
)
def test_text_without_brackets_is_unchanged(text):
    assert strip_bracketed_segments(text) == text


def test_none_is_passed_through():
    """Utterance.text is nullable; callers should not have to guard."""
    assert strip_bracketed_segments(None) is None


# ---------------------------------------------------------------------------
# Whitespace tidying
# ---------------------------------------------------------------------------


def test_leading_marker_leaves_no_leading_blank_line():
    assert strip_bracketed_segments("[Monday, August 24]\nGood morning!") == "Good morning!"


def test_trailing_marker_leaves_no_trailing_whitespace():
    assert strip_bracketed_segments("Good morning!\n\n[Monday, August 24]") == "Good morning!"


def test_paragraph_structure_between_real_content_is_preserved():
    text = "First para.\n\nSecond para."
    assert strip_bracketed_segments(text) == text


def test_marker_only_message_prunes_to_empty():
    """Signals 'nothing left to say' to the caller rather than inventing text."""
    assert strip_bracketed_segments("[Friday, August 21]") == ""
