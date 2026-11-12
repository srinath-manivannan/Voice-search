"""The page, checked against WCAG 2.1 AA where a test can check it.

Two things a test genuinely can do: compute every contrast ratio the
stylesheet declares, and assert the markup a keyboard and a screen reader
need is actually in the file. What it cannot do is tell you the page is
usable, so this is a floor, not a certificate.

The contrast maths is the W3C formula, written out rather than imported, so
the numbers in the stylesheet's own comment can be re-derived by anyone
reading this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "index.html"
HTML = PAGE.read_text()

# WCAG 2.1 AA: 4.5:1 for normal text, 3:1 for large text and for the
# boundaries of user interface components.
TEXT_MINIMUM = 4.5
UI_MINIMUM = 3.0

WHITE = "#ffffff"
SOFT = "#fafafa"  # --soft, the other ground text sits on


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    h = colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(foreground: str, background: str) -> float:
    a, b = luminance(foreground), luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def variable(name: str) -> str:
    """Read a custom property straight out of the stylesheet.

    Reading the file rather than restating the value is the whole point: a
    colour changed in the page without being measured fails here.
    """
    found = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{3,8}})\s*;", HTML)
    assert found, f"--{name} is not declared in {PAGE.name}"
    return found.group(1)


def test_the_contrast_formula_agrees_with_the_known_extremes():
    """Guards the maths itself, so a bug here cannot pass the rest."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("name", ["ink", "dim", "good", "warn", "bad"])
@pytest.mark.parametrize("ground", [WHITE, SOFT])
def test_every_colour_that_carries_text_clears_aa(name, ground):
    """Measured, not eyeballed.

    Before this existed the page had eight failing pairs, the worst of them
    at 1.69:1 - and that one was an input's focus border, which is the single
    thing a keyboard user needs to be able to see.
    """
    assert contrast(variable(name), ground) >= TEXT_MINIMUM


def test_white_text_on_the_brand_colour_clears_aa():
    """The call button. The logo orange is 3.4:1 under white text, so the
    page carries a deeper one for anywhere text sits on it."""
    assert contrast(WHITE, variable("brand-ink")) >= TEXT_MINIMUM
    assert contrast(WHITE, variable("brand-ink-hover")) >= TEXT_MINIMUM


def test_the_brand_colour_is_only_ever_used_for_non_text_and_clears_that_bar():
    """--brand is the selected tab's underline and the cost table's share
    bar. Both are UI, not text, so 3:1 applies - and the bar's value is in
    the number beside it, so nothing depends on seeing the colour at all."""
    assert contrast(variable("brand"), WHITE) >= UI_MINIMUM
    # `color: var(--brand)` would put it on text. `border-bottom-color` and
    # `background` are the two non-text uses it is allowed.
    assert not re.search(r"(?<!-)\bcolor:\s*var\(--brand\)", HTML), (
        "--brand is being used for text somewhere"
    )


def test_no_hardcoded_grey_sneaks_past_the_palette():
    """Three ad-hoc greys were doing --dim's job at 2.17, 2.52 and 1.69.

    They were invisible to review precisely because they were not in the
    palette, so this looks for the shape of the mistake rather than the
    three particular values.
    """
    styles = HTML[HTML.index("<style>") : HTML.index("</style>")]
    for match in re.finditer(r"color:\s*(#[0-9a-fA-F]{6})\s*;", styles):
        colour = match.group(1)
        assert contrast(colour, WHITE) >= TEXT_MINIMUM, (
            f"{colour} is used for text at {contrast(colour, WHITE):.2f}:1"
        )


# --- keyboard and screen reader ---------------------------------------------


def test_the_tabs_are_a_tablist_and_not_two_buttons():
    assert 'role="tablist"' in HTML
    assert HTML.count('role="tab"') == 2
    assert HTML.count('role="tabpanel"') == 2


def test_each_tab_names_the_panel_it_controls_and_each_panel_its_tab():
    for tab, panel in (("tab-voice", "voice-panel"), ("tab-chat", "chat-panel")):
        assert f'aria-controls="{panel}"' in HTML
        assert f'aria-labelledby="{tab}"' in HTML


def test_only_the_selected_tab_is_in_the_page_tab_order():
    """The roving tabindex. Without it, Tab walks through every tab instead
    of past the list, and a two-tab list is merely annoying where a ten-tab
    one is a trap."""
    assert 'aria-selected="true"' in HTML and 'tabindex="0"' in HTML
    assert 'aria-selected="false"' in HTML and 'tabindex="-1"' in HTML


def test_the_arrow_keys_move_between_tabs():
    """Asserted against the script, because the markup cannot show it."""
    script = (PAGE.parent / "app.js").read_text()
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert key in script, f"{key} does not move between tabs"
    assert "tabIndex" in script, "the roving tabindex is never updated"


def test_focus_is_visible_without_drawing_a_ring_for_mouse_users():
    assert ":focus-visible" in HTML
    assert "outline:" in HTML


def test_the_message_box_has_a_label_and_not_just_a_placeholder():
    """A placeholder disappears the moment anyone types, which is exactly
    when someone re-reading the field needs to know what it is."""
    assert '<label class="sr-only" for="chat-input">' in HTML


def test_every_decorative_icon_is_hidden_from_a_screen_reader():
    """Otherwise each button is read out twice: once as its path data and
    once as its label."""
    assert HTML.count("<svg") == HTML.count('aria-hidden="true"')


def test_every_icon_only_button_says_what_it_does():
    for button in re.finditer(r'<button[^>]*class="(icon-btn|send)"[^>]*>', HTML):
        assert "aria-label=" in button.group(0), button.group(0)


def test_the_page_declares_its_language():
    assert '<html lang="en">' in HTML


def test_an_arrow_press_does_not_move_focus_into_the_panel():
    """Caught in a browser, not in review.

    The first version moved focus to the chat composer whenever the chat tab
    became visible. With a pointer that is a kindness; with the arrow keys it
    is a trap, because focus leaves the tablist and the other tab can no
    longer be reached with an arrow. The arrows move between TABS; Tab enters
    the panel.
    """
    app_js = (PAGE.parent / "app.js").read_text()
    chat_js = (PAGE.parent / "chat.js").read_text()

    # The click path asks for the panel to take focus; the arrow path does not.
    assert "'chat', true)" in app_js, "a click should focus the panel"
    assert "false);" in app_js, "an arrow press should not"
    # And the chat module honours the distinction rather than always focusing.
    assert "event.detail.focusPanel" in chat_js
