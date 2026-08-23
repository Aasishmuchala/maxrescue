"""The render-identity gate.

The gate's value is entirely in what it refuses. A gate that passes a changed
render is worse than no gate, because the contract then reads as verified when
it is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxrescue.core.verify import Tolerance, compare


@dataclass
class FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def runner_returning(code: int, out: str = ""):
    captured: dict = {}

    def run(command):
        captured["command"] = list(command)
        return FakeCompleted(code, out)

    run.captured = captured  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# bit-exact: the automatic stages
# ---------------------------------------------------------------------------


def test_identical_renders_pass():
    result = compare("a.exr", "b.exr", runner=runner_returning(0), tool="idiff")
    assert result.identical and result.passed
    assert "bit-identical" in result.describe()


def test_any_difference_fails_an_automatic_stage():
    """Exit 1 is idiff's 'within the warning threshold'. For a stage that
    claims identity, a warning is a failure."""
    result = compare("a.exr", "b.exr", runner=runner_returning(1), tool="idiff")
    assert not result.identical
    assert not result.passed
    assert "DIFFER" in result.describe()


def test_a_real_difference_fails():
    result = compare("a.exr", "b.exr", runner=runner_returning(2), tool="idiff")
    assert not result.passed


def test_different_sizes_fail_with_a_clear_reason():
    result = compare("a.exr", "b.exr", runner=runner_returning(3), tool="idiff")
    assert not result.passed
    assert "different sizes" in result.summary


def test_an_unreadable_file_fails_rather_than_passing_quietly():
    result = compare("a.exr", "b.exr", runner=runner_returning(4), tool="idiff")
    assert not result.passed
    assert "read" in result.summary


def test_bit_exact_comparison_passes_no_tolerance_flags():
    run = runner_returning(0)
    compare("a.exr", "b.exr", runner=run, tool="idiff")
    command = run.captured["command"]
    for flag in ("-fail", "-failpercent", "-hardfail", "-p"):
        assert flag not in command


# ---------------------------------------------------------------------------
# tolerant: the opt-in bitmap stage only
# ---------------------------------------------------------------------------


def test_a_small_difference_passes_only_where_a_tolerance_was_allowed():
    result = compare(
        "a.exr", "b.exr",
        tolerance=Tolerance.filtering_change(),
        runner=runner_returning(1),
        tool="idiff",
    )
    assert result.passed
    assert not result.identical


def test_a_tolerant_pass_says_it_still_needs_a_human():
    result = compare(
        "a.exr", "b.exr",
        tolerance=Tolerance.filtering_change(),
        runner=runner_returning(1),
        tool="idiff",
    )
    assert "review" in result.describe().lower()


def test_a_large_difference_fails_even_with_a_tolerance():
    result = compare(
        "a.exr", "b.exr",
        tolerance=Tolerance.filtering_change(),
        runner=runner_returning(2),
        tool="idiff",
    )
    assert not result.passed


def test_tolerant_comparison_passes_the_thresholds_through():
    run = runner_returning(0)
    compare("a.exr", "b.exr", tolerance=Tolerance.filtering_change(),
            runner=run, tool="idiff")
    command = run.captured["command"]
    assert "-fail" in command and "-failpercent" in command and "-p" in command


# ---------------------------------------------------------------------------
# missing tooling is 'unverified', never 'passed'
# ---------------------------------------------------------------------------


def test_a_missing_comparison_tool_is_reported_as_unverified_not_as_a_pass():
    """A rescue that produced a good scene should not be called a failure
    because OpenImageIO is absent — but it must never be called verified."""
    result = compare("a.exr", "b.exr", runner=runner_returning(0), tool=None)
    if result.exit_code == -1:  # no idiff on this machine
        assert not result.passed
        assert "NOT compared" in result.summary


def test_a_crashing_comparison_is_reported_not_swallowed():
    def explode(command):
        raise OSError("idiff segfaulted")

    result = compare("a.exr", "b.exr", runner=explode, tool="idiff")
    assert not result.passed
    assert "segfaulted" in result.summary


def test_the_command_used_is_recorded_so_it_can_be_re_run_by_hand():
    run = runner_returning(0)
    result = compare("before.exr", "after.exr", runner=run, tool="idiff")
    assert result.command[0] == "idiff"
    assert result.command[-2:] == ("before.exr", "after.exr")
