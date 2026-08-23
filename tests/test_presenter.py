"""What the window says, as pure data.

Every word the user reads and every button state is decided here, with no Qt in
sight, so the wording of a failure can be tested as carefully as the logic that
produced it. The Qt layer only draws this.
"""

from __future__ import annotations

from maxrescue.ui.presenter import AppState, Phase, present


def test_with_no_file_it_asks_for_one_and_offers_nothing_else():
    view = present(AppState())
    assert view.phase is Phase.EMPTY
    assert "drop" in view.headline.lower()
    assert ".max" in view.headline.lower() or ".max" in view.detail.lower()
    assert view.can_rescue is False
    assert view.facts == ()


# --------------------------------------------------------------------------
# a file has been surveyed
# --------------------------------------------------------------------------


def _surveyed(tmp_path, **kw):
    """A real XrayReport from a real file — never a stand-in, so the wording is
    tested against what the X-ray actually produces."""
    from maxrescue.xray.report import xray
    from tests.test_integration import _villa

    return AppState(path=str(_villa(tmp_path, **kw)), survey=xray(str(_villa(tmp_path, **kw))))


def test_a_surveyed_file_names_the_problem_in_plain_language(tmp_path):
    view = present(_surveyed(tmp_path))
    assert view.phase is Phase.SURVEYED
    # The user is an artist, not a parser author: no verdict codes on screen.
    assert "single-monster-object" not in view.headline
    assert "one object" in view.headline.lower()
    assert "Villa_Hero" in view.detail


def test_a_surveyed_file_can_be_rescued(tmp_path):
    view = present(_surveyed(tmp_path))
    assert view.can_rescue is True
    assert "rescue" in view.rescue_label.lower()


def test_the_facts_answer_what_an_artist_would_ask_first(tmp_path):
    view = present(_surveyed(tmp_path))
    labels = [f.label.lower() for f in view.facts]
    assert any("object" in label for label in labels)
    assert any("plugin" in label for label in labels)
    assert any("size" in label or "disk" in label for label in labels)


def test_required_plugins_are_listed_by_name(tmp_path):
    view = present(_surveyed(tmp_path))
    plugins = next(f for f in view.facts if "plugin" in f.label.lower())
    assert "ForestPackPro.dlo" in plugins.value


def test_a_file_with_a_known_payload_refuses_to_be_rescued(tmp_path):
    view = present(_surveyed(tmp_path, malware=True))
    assert view.can_rescue is False
    assert view.warnings
    assert "malicious" in " ".join(view.warnings).lower()
