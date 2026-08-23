"""MAXScript lint.

Two mistakes in this language fail *silently* — the file simply never runs, and
nothing says why. Both cost a sibling project a session, so both are checked
here rather than discovered on the box.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _ms_files() -> list[pathlib.Path]:
    return sorted(SCRIPTS.rglob("*.ms"))


def test_there_are_maxscript_files_to_check():
    assert _ms_files(), "no .ms files found — has the layout moved?"


@pytest.mark.parametrize("path", _ms_files(), ids=lambda p: p.name)
def test_no_line_continuation_backslash(path: pathlib.Path):
    """MAXScript has NO backslash line continuation. One trailing backslash
    kills the entire file and the script silently never loads."""
    offenders = [
        (i, line)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.rstrip().endswith("\\")
    ]
    assert not offenders, (
        f"{path.name} has trailing backslashes at lines "
        f"{[i for i, _ in offenders]} — MAXScript has no line continuation, so "
        "the whole file will silently fail to load."
    )


@pytest.mark.parametrize("path", _ms_files(), ids=lambda p: p.name)
def test_embedded_paths_use_forward_slashes(path: pathlib.Path):
    """Paths are concatenated into Python calls; a Python raw string ending in a
    backslash is a SyntaxError, so forward slashes are the only safe form."""
    source = path.read_text(encoding="utf-8")
    for line in source.splitlines():
        if "python." in line and "\\" in line:
            pytest.fail(
                f"{path.name} passes a backslash path to python.*: {line.strip()!r}"
            )


@pytest.mark.parametrize("path", _ms_files(), ids=lambda p: p.name)
def test_failures_are_raised_not_printed(path: pathlib.Path):
    """A script that prints a problem and returns leaves 3dsmaxbatch exiting 0,
    so the harness reports success for a run that did nothing."""
    source = path.read_text(encoding="utf-8")
    if "doesFileExist" in source or "getEnvVariable" in source:
        assert "throw" in source, (
            f"{path.name} checks a precondition but never throws — a failed "
            "precondition would exit 0 and read as success."
        )


@pytest.mark.parametrize("path", _ms_files(), ids=lambda p: p.name)
def test_balanced_parentheses(path: pathlib.Path):
    """MAXScript blocks are parenthesised; an unbalanced file fails to parse
    with a message that points at the wrong line."""
    source = path.read_text(encoding="utf-8")
    depth = 0
    for char in source:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            assert depth >= 0, f"{path.name} closes a paren that was never opened"
    assert depth == 0, f"{path.name} has {depth} unclosed parenthesis/es"
