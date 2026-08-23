"""Static validation of on-box scripts.

These run inside 3ds Max on Windows and cannot be imported here — `import pymxs`
fails everywhere else. That is exactly why they need checking statically:
a sibling project shipped an on-box script importing a symbol that had been
renamed, the ImportError was swallowed by a guard, and the box printed
"must run INSIDE 3ds Max" — a lie that cost a whole session.

So: every on-box script must parse, every first-party import must resolve
against the real package, and the specific mistakes that make `3dsmaxbatch`
fail opaquely are checked by name.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _script_files() -> list[pathlib.Path]:
    return sorted(SCRIPTS.glob("*.py"))


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_there_are_on_box_scripts_to_check():
    assert _script_files(), "no scripts/*.py found — has the layout moved?"


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_script_compiles(path: pathlib.Path):
    """A syntax error only shows up as a 45-second Max launch that does nothing."""
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")


def _module_defines(module_name: str) -> set[str]:
    """Top-level names a first-party module defines.

    Pure modules are imported. `maxbridge` modules cannot be — they import
    pymxs — so those are parsed instead. Skipping them entirely would drop
    exactly the imports most likely to rot, since the bridge is the code that
    never runs here.
    """
    if "maxbridge" in module_name:
        path = ROOT / pathlib.Path(*module_name.split(".")).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(a.asname or a.name.split(".")[0] for a in node.names)
        return names
    return set(dir(importlib.import_module(module_name)))


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_first_party_imports_resolve(path: pathlib.Path):
    """`from maxrescue.x import Y` must name a Y that exists.

    This is the check that would have caught the renamed-symbol failure: a
    sibling project shipped an on-box script importing a renamed symbol, the
    ImportError was swallowed, and the box printed a misleading message.
    """
    missing: list[str] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("maxrescue"):
            continue
        defined = _module_defines(node.module)
        for alias in node.names:
            if alias.name != "*" and alias.name not in defined:
                missing.append(f"{node.module}.{alias.name}")
    assert not missing, f"{path.name} imports names that do not exist: {missing}"


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_on_box_script_imports_pymxs(path: pathlib.Path):
    """These scripts are only meaningful inside Max. One that never touches
    pymxs is either dead or in the wrong directory."""
    assert "pymxs" in path.read_text(encoding="utf-8"), (
        f"{path.name} never imports pymxs — is it really an on-box script?"
    )


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_no_stdout_stream_handler(path: pathlib.Path):
    """A `logging.StreamHandler` writing to stdout is on its own enough to make
    3dsmaxbatch return exit -130. Log to a file."""
    used = [
        node
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Attribute) and node.attr == "StreamHandler"
    ] + [
        alias
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "StreamHandler"
    ]
    assert not used, (
        f"{path.name} uses logging.StreamHandler — this alone causes exit -130 "
        "from 3dsmaxbatch. Write to a file instead."
    )


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_work_is_not_hidden_behind_an_import_guard(path: pathlib.Path):
    """If the entry work sits inside `try: import pymxs / except: print(...)`,
    a real failure is reported as 'not running inside Max' and the session is
    wasted chasing a phantom."""
    tree = _tree(path)
    for node in tree.body:
        if isinstance(node, ast.Try):
            has_import = any(
                isinstance(inner, (ast.Import, ast.ImportFrom))
                for inner in ast.walk(node)
            )
            substantial = len(node.body) > 3
            assert not (has_import and substantial), (
                f"{path.name} runs its work inside an import guard — a genuine "
                "error will be misreported as a missing-Max message."
            )


def test_spikes_writes_a_result_file_at_module_level():
    """The harness reads a result file, never stdout. If the script can finish
    without writing one, a silent no-op looks identical to a clean run."""
    source = (SCRIPTS / "spikes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "write_result" in top_level_calls, (
        "spikes.py must call write_result() at module level, or the harness has "
        "nothing to read."
    )


def test_spikes_declares_every_environment_variable_it_reads():
    """An undocumented env var is a spike that silently skips itself."""
    source = (SCRIPTS / "spikes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    read: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "environ")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("MAXRESCUE_")
        ):
            read.add(node.args[0].value)

    docstring = ast.get_docstring(tree) or ""
    undocumented = sorted(name for name in read if name not in docstring)
    assert not undocumented, (
        f"spikes.py reads undocumented environment variables: {undocumented}"
    )


def test_the_launcher_names_the_documented_batch_exit_codes():
    """A bare 'exit -6' sends someone to a search engine; 'OUT OF MEMORY' does
    not."""
    launcher = (SCRIPTS / "run_spikes.ps1").read_text(encoding="utf-8")
    for code in ("-6", "-7", "-8", "-130"):
        assert code in launcher, f"launcher does not explain exit {code}"


def test_the_launcher_clears_a_stale_result_before_running():
    launcher = (SCRIPTS / "run_spikes.ps1").read_text(encoding="utf-8")
    assert "Remove-Item $resultFile" in launcher, (
        "a stale result file would be read as this run's verdict"
    )
