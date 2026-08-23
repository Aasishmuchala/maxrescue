"""The worker that turns one button press into a finished scene.

Everything between the click and the result: finding 3ds Max, building the
environment, launching `3dsmaxbatch`, following progress, and reading the
verdict back.

The launcher is injected, so all of this is testable without Windows or Max.
What is NOT tested here is the subprocess call itself — that is one line, and
faking it would only prove the fake works.

The behaviour that matters most is the unhappy path: a batch that dies without
writing a result must never be reported as a finished rescue.
"""

from __future__ import annotations

import json
import os

import pytest

from maxrescue.ui.presenter import Outcome
from maxrescue.ui.runner import (
    MaxInstall,
    RescueRequest,
    RunnerError,
    build_environment,
    find_max_installs,
    parse_result,
    run_rescue,
)


# ---------------------------------------------------------------------------
# finding 3ds Max
# ---------------------------------------------------------------------------


def test_it_finds_installed_max_versions_newest_first(tmp_path):
    for version in ("2024", "2026", "2025"):
        folder = tmp_path / f"3ds Max {version}"
        folder.mkdir()
        (folder / "3dsmaxbatch.exe").write_text("")
    installs = find_max_installs(roots=[str(tmp_path)])
    assert [i.version for i in installs] == ["2026", "2025", "2024"]


def test_a_folder_without_the_batch_executable_is_not_an_install(tmp_path):
    (tmp_path / "3ds Max 2026").mkdir()
    assert find_max_installs(roots=[str(tmp_path)]) == []


def test_no_install_found_is_an_empty_list_not_a_crash(tmp_path):
    assert find_max_installs(roots=[str(tmp_path / "nowhere")]) == []


# ---------------------------------------------------------------------------
# the environment handed to the batch process
# ---------------------------------------------------------------------------


def _request(tmp_path, **kw) -> RescueRequest:
    scene = tmp_path / "villa.max"
    scene.write_bytes(b"x")
    defaults = dict(
        scene=str(scene),
        output=str(tmp_path / "villa_rescued.max"),
        work_dir=str(tmp_path / "work"),
        ceiling_gb=70,
        texture_floor_px=4096,
    )
    defaults.update(kw)
    return RescueRequest(**defaults)


def test_the_environment_carries_everything_the_on_box_script_reads(tmp_path):
    env = build_environment(_request(tmp_path), repo="C:/maxrescue")
    for key in (
        "MAXRESCUE_REPO",
        "MAXRESCUE_RESULT",
        "MAXRESCUE_TARGET",
        "MAXRESCUE_OUTPUT",
        "MAXRESCUE_OUT",
        "MAXRESCUE_CEILING_GB",
    ):
        assert key in env, key


def test_the_texture_floor_reaches_the_script(tmp_path):
    env = build_environment(_request(tmp_path, texture_floor_px=8192), repo=".")
    assert env["MAXRESCUE_TEXTURE_FLOOR_PX"] == "8192"


def test_a_reference_is_only_passed_when_one_exists(tmp_path):
    plain = build_environment(_request(tmp_path), repo=".")
    assert not plain.get("MAXRESCUE_REFERENCE")

    reference = tmp_path / "reference.exr"
    reference.write_bytes(b"x")
    with_ref = build_environment(
        _request(tmp_path, reference=str(reference)), repo="."
    )
    assert with_ref["MAXRESCUE_REFERENCE"] == str(reference)


def test_it_refuses_to_write_the_output_over_the_source(tmp_path):
    """The source is the only backup this design has."""
    scene = tmp_path / "villa.max"
    scene.write_bytes(b"x")
    with pytest.raises(RunnerError, match="source"):
        build_environment(
            RescueRequest(
                scene=str(scene),
                output=str(scene),
                work_dir=str(tmp_path),
                ceiling_gb=70,
                texture_floor_px=4096,
            ),
            repo=".",
        )


# ---------------------------------------------------------------------------
# reading the verdict back
# ---------------------------------------------------------------------------


def _report(tmp_path, **kw) -> str:
    payload = {
        "outcome": {
            "output": r"D:\villa_rescued.max",
            "merged": 47_213,
            "requested": 47_213,
            "peak_rss_mb": 51_200,
            "rss_end_mb": 43_008,
        },
        "textures_reduced": 118,
    }
    payload.update(kw)
    path = tmp_path / "rescue_report.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_a_verified_run_is_reported_as_verified(tmp_path):
    outcome = parse_result("RESCUE_VERIFIED renders are identical", _report(tmp_path))
    assert isinstance(outcome, Outcome)
    assert outcome.verified is True
    assert outcome.objects_merged == 47_213


def test_a_run_with_no_reference_is_unverified_rather_than_fine(tmp_path):
    outcome = parse_result("RESCUE_OK merged=47213", _report(tmp_path))
    assert outcome.verified is None, "'not checked' must never read as 'passed'"


def test_a_differing_render_is_reported_as_failed_verification(tmp_path):
    outcome = parse_result("RESCUE_DIFFERENT pixels moved", _report(tmp_path))
    assert outcome.verified is False
    assert outcome.problems


def test_a_partial_run_keeps_its_shortfall(tmp_path):
    report = _report(tmp_path, outcome={
        "output": "x.max", "merged": 40_000, "requested": 47_213,
        "peak_rss_mb": 51_200, "rss_end_mb": 43_008,
    })
    outcome = parse_result("RESCUE_PARTIAL merged=40000/47213", report)
    assert outcome.shortfall == 7_213


def test_a_halted_run_says_what_stopped_it(tmp_path):
    outcome = parse_result("RESCUE_HALTED a batch failed", _report(tmp_path))
    assert outcome.problems
    assert "halt" in " ".join(outcome.problems).lower()


def test_a_refusal_is_surfaced_rather_than_swallowed(tmp_path):
    outcome = parse_result(
        "RESCUE_REFUSED known payload signature present", _report(tmp_path)
    )
    assert outcome.problems
    assert "payload" in " ".join(outcome.problems).lower()


def test_measured_memory_reaches_the_outcome(tmp_path):
    outcome = parse_result("RESCUE_OK", _report(tmp_path))
    assert outcome.open_rss_mb == 43_008
    assert outcome.peak_rss_mb == 51_200


def test_a_missing_report_still_yields_the_verdict_line(tmp_path):
    """The JSON is a nicety; the result file is the contract."""
    outcome = parse_result("RESCUE_OK", str(tmp_path / "absent.json"))
    assert outcome.problems == () or isinstance(outcome.problems, tuple)
    assert outcome.objects_merged == 0


def test_a_corrupt_report_does_not_lose_the_verdict(tmp_path):
    path = tmp_path / "rescue_report.json"
    path.write_text("{not json")
    outcome = parse_result("RESCUE_VERIFIED", str(path))
    assert outcome.verified is True


# ---------------------------------------------------------------------------
# the run itself
# ---------------------------------------------------------------------------


def _install(tmp_path) -> MaxInstall:
    folder = tmp_path / "3ds Max 2026"
    folder.mkdir(exist_ok=True)
    batch = folder / "3dsmaxbatch.exe"
    batch.write_text("")
    return MaxInstall(version="2026", batch_path=str(batch))


def test_a_successful_run_reports_the_outcome(tmp_path):
    request = _request(tmp_path)

    def launcher(command, env):
        work = env["MAXRESCUE_OUT"]
        os.makedirs(work, exist_ok=True)
        with open(env["MAXRESCUE_RESULT"], "w") as handle:
            handle.write("RESCUE_VERIFIED renders are identical")
        with open(os.path.join(work, "rescue_report.json"), "w") as handle:
            json.dump(
                {"outcome": {"merged": 10, "requested": 10, "rss_end_mb": 1024,
                             "peak_rss_mb": 2048, "output": "out.max"}},
                handle,
            )
        return 0

    outcome = run_rescue(request, _install(tmp_path), launcher=launcher, repo=".")
    assert outcome.verified is True
    assert outcome.objects_merged == 10


def test_a_batch_that_dies_without_a_result_is_never_called_finished(tmp_path):
    """The failure this exists to prevent: 3dsmaxbatch crashes, no result file
    is written, and the app cheerfully shows a success screen."""
    request = _request(tmp_path)

    def dies(command, env):
        return -6  # out of memory

    with pytest.raises(RunnerError) as exc:
        run_rescue(request, _install(tmp_path), launcher=dies, repo=".")
    assert "memory" in str(exc.value).lower()


def test_documented_exit_codes_are_explained_not_just_numbered(tmp_path):
    request = _request(tmp_path)
    for code, expected in [
        (-6, "memory"),
        (-7, "graphics"),
        (-8, "licence"),
        (-130, "reported an error"),
    ]:
        with pytest.raises(RunnerError) as exc:
            run_rescue(
                request, _install(tmp_path), launcher=lambda c, e, _c=code: _c, repo="."
            )
        assert expected in str(exc.value).lower(), code


def test_a_stale_result_from_a_previous_run_is_never_read(tmp_path):
    """Reading yesterday's verdict as today's is the worst kind of green."""
    request = _request(tmp_path)
    os.makedirs(request.work_dir, exist_ok=True)
    with open(os.path.join(request.work_dir, "_rescue_result.txt"), "w") as handle:
        handle.write("RESCUE_VERIFIED from a run that happened last week")

    with pytest.raises(RunnerError):
        run_rescue(request, _install(tmp_path), launcher=lambda c, e: -6, repo=".")


# ---------------------------------------------------------------------------
# packaged as an .exe
# ---------------------------------------------------------------------------


def test_the_repo_root_follows_the_bundle_when_frozen(monkeypatch, tmp_path):
    """PyInstaller unpacks a one-file build to a temp directory. 3dsmaxbatch is
    a separate process opening these scripts itself, so it needs the real path
    there rather than the source tree that is not on the machine."""
    import sys

    from maxrescue.ui.runner import repo_root

    unfrozen = repo_root()
    assert os.path.isdir(os.path.join(unfrozen, "scripts"))

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert repo_root() == str(tmp_path)


def test_the_spec_ships_the_scripts_the_runner_launches():
    """A build that omits them produces an app that diagnoses perfectly and can
    never rescue anything."""
    import pathlib

    spec = pathlib.Path(__file__).resolve().parent.parent / "maxrescue.spec"
    text = spec.read_text(encoding="utf-8")
    for required in ("scripts/rescue.py", "scripts/run_rescue.ms"):
        assert required in text, f"{required} is not bundled"
    assert "console=False" in text, "a console window behind the app looks like a fault"
