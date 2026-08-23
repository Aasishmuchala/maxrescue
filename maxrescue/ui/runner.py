"""Everything between the button press and a finished scene.

Finds 3ds Max, builds the environment the on-box script reads, launches
`3dsmaxbatch`, and reads the verdict back.

The launcher is injected so all of this is testable without Windows or Max. The
subprocess call itself is deliberately the only untested line — faking it would
prove nothing but that the fake works.

The behaviour worth the most care is the unhappy one: a batch that dies without
writing a result must **never** surface as a finished rescue. That is the shape
of failure that wastes a whole session, because the app looks like it worked.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

from maxrescue.ui.presenter import Outcome

__all__ = [
    "EXIT_CODES",
    "MaxInstall",
    "RescueRequest",
    "RunnerError",
    "build_environment",
    "find_max_installs",
    "parse_result",
    "run_rescue",
]

RESULT_FILE = "_rescue_result.txt"
REPORT_FILE = "rescue_report.json"

DEFAULT_ROOTS = (
    r"C:\Program Files\Autodesk",
    r"C:\Program Files (x86)\Autodesk",
)

#: 3dsmaxbatch's documented exit codes, in words. A bare "-6" sends someone to a
#: search engine; "out of memory" tells them to lower the ceiling and re-run.
EXIT_CODES = {
    -1: "3ds Max hit a handled exception",
    -2: "3ds Max hit an unhandled exception",
    -6: "3ds Max ran OUT OF MEMORY — lower the ceiling and run it again; "
        "the governor will resize the batches from what it measured",
    -7: "3ds Max could not create a graphics device — this usually means it is "
        "running over Remote Desktop or without a display",
    -8: "a licence error — 3ds Max could not check out a licence",
    -100: "the batch process was given bad arguments",
    -110: "3ds Max could not be started",
    -120: "the batch process could not talk to 3ds Max",
    -130: "3ds Max reported an error — the listener log has the detail",
}


class RunnerError(RuntimeError):
    """The run could not be completed, and here is what to do about it."""


@dataclass(frozen=True)
class MaxInstall:
    version: str
    batch_path: str

    @property
    def label(self) -> str:
        return f"3ds Max {self.version}"


@dataclass(frozen=True)
class RescueRequest:
    scene: str
    output: str
    work_dir: str
    ceiling_gb: int = 70
    texture_floor_px: int = 4096
    reference: str = ""
    light_cache: str = ""
    camera: str = ""
    convert_bitmaps: bool = False


# ---------------------------------------------------------------------------
# finding 3ds Max
# ---------------------------------------------------------------------------


def repo_root() -> str:
    """Where `scripts/` lives, frozen or not.

    PyInstaller unpacks a one-file build to a temporary directory and points
    `sys._MEIPASS` at it. That matters here beyond tidiness: `3dsmaxbatch.exe` is
    a separate process opening these files itself, so they have to be real paths
    on disk rather than entries inside a bundle.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return str(bundled)
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def find_max_installs(roots: Sequence[str] | None = None) -> list[MaxInstall]:
    """Installed versions, newest first.

    An install is a folder that actually contains `3dsmaxbatch.exe` — a leftover
    directory from an uninstall would otherwise be offered and then fail.
    """
    found: list[MaxInstall] = []
    for root in roots or DEFAULT_ROOTS:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            match = re.match(r"3ds Max (\d{4})$", entry)
            if not match:
                continue
            batch = os.path.join(root, entry, "3dsmaxbatch.exe")
            if os.path.isfile(batch):
                found.append(MaxInstall(version=match.group(1), batch_path=batch))

    found.sort(key=lambda install: install.version, reverse=True)
    return found


# ---------------------------------------------------------------------------
# the environment
# ---------------------------------------------------------------------------


def build_environment(request: RescueRequest, repo: str) -> dict[str, str]:
    """The variables `scripts/rescue.py` reads.

    Refuses outright if the output resolves to the source. The source file is
    the only backup this design has — no per-batch copy is taken precisely
    because it is meant to stay untouched.
    """
    if os.path.abspath(os.path.normcase(request.output)) == os.path.abspath(
        os.path.normcase(request.scene)
    ):
        raise RunnerError(
            "the output file is the source file — that would destroy the "
            "original, and there is no other backup"
        )

    env = dict(os.environ)
    env.update(
        {
            "MAXRESCUE_REPO": repo,
            "MAXRESCUE_RESULT": os.path.join(request.work_dir, RESULT_FILE),
            "MAXRESCUE_TARGET": request.scene,
            "MAXRESCUE_OUTPUT": request.output,
            "MAXRESCUE_OUT": request.work_dir,
            "MAXRESCUE_CEILING_GB": str(request.ceiling_gb),
            "MAXRESCUE_TEXTURE_FLOOR_PX": str(request.texture_floor_px),
            "MAXRESCUE_CONVERT_BITMAPS": "1" if request.convert_bitmaps else "0",
        }
    )
    # Only passed when the file is really there: the script treats a set-but-
    # missing reference as "not verified", and saying so up front is clearer.
    if request.reference and os.path.isfile(request.reference):
        env["MAXRESCUE_REFERENCE"] = request.reference
    if request.light_cache:
        env["MAXRESCUE_LIGHTCACHE"] = request.light_cache
    if request.camera:
        env["MAXRESCUE_CAMERA"] = request.camera
    return env


# ---------------------------------------------------------------------------
# reading the verdict
# ---------------------------------------------------------------------------


def parse_result(verdict: str, report_path: str) -> Outcome:
    """Turn the result line and the JSON report into an :class:`Outcome`.

    The result line is the contract; the JSON is a nicety. A missing or corrupt
    report costs the numbers, never the verdict.
    """
    payload: dict = {}
    try:
        with open(report_path, encoding="utf-8") as handle:
            payload = json.load(handle) or {}
    except Exception:
        payload = {}

    outcome_data = payload.get("outcome") or {}
    problems: list[str] = []

    verified: bool | None = None
    if verdict.startswith("RESCUE_VERIFIED"):
        verified = True
    elif verdict.startswith("RESCUE_DIFFERENT"):
        verified = False
        problems.append(
            "The rescued scene does NOT render identically to the reference. "
            "Do not ship this file — keep both frames and the report."
        )
    if verdict.startswith("RESCUE_HALTED"):
        problems.append(
            "The run halted partway. Batches completed before that point are "
            "saved; the report names the one that stopped it."
        )
    if verdict.startswith("RESCUE_REFUSED"):
        problems.append(f"Refused: {verdict.split(' ', 1)[-1]}")
    if verdict.startswith("RESCUE_FAIL"):
        problems.append(verdict)

    return Outcome(
        output_path=str(outcome_data.get("output") or ""),
        open_rss_mb=float(outcome_data.get("rss_end_mb") or 0),
        peak_rss_mb=float(outcome_data.get("peak_rss_mb") or 0),
        vram_mb=outcome_data.get("vram_mb"),
        objects_merged=int(outcome_data.get("merged") or 0),
        objects_requested=int(outcome_data.get("requested") or 0),
        textures_reduced=int(payload.get("textures_reduced") or 0),
        verified=verified,
        problems=tuple(problems),
    )


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

Launcher = Callable[[Sequence[str], dict], int]


def _default_launcher(command: Sequence[str], env: dict) -> int:  # pragma: no cover
    import subprocess

    return subprocess.call(list(command), env=env)


def run_rescue(
    request: RescueRequest,
    install: MaxInstall,
    launcher: Launcher | None = None,
    repo: str | None = None,
) -> Outcome:
    """Run one rescue and return what it produced.

    Raises :class:`RunnerError` rather than returning a hopeful Outcome when the
    batch process dies. A crash that surfaces as a success screen is worse than
    a crash that surfaces as a crash.
    """
    repo = repo or repo_root()
    os.makedirs(request.work_dir, exist_ok=True)

    result_path = os.path.join(request.work_dir, RESULT_FILE)
    # A verdict left by a previous run would otherwise be read as this one's.
    try:
        os.remove(result_path)
    except OSError:
        pass

    env = build_environment(request, repo=repo)
    wrapper = os.path.join(repo, "scripts", "run_rescue.ms")
    log = os.path.join(request.work_dir, "listener.log")
    command = [install.batch_path, wrapper, "-listenerlog", log, "-v", "5"]

    code = (launcher or _default_launcher)(command, env)

    if not os.path.isfile(result_path):
        explanation = EXIT_CODES.get(code)
        detail = f" ({explanation})" if explanation else f" (exit code {code})"
        raise RunnerError(
            f"{install.label} stopped without finishing{detail}. "
            f"Nothing was written. The listener log is at {log}."
        )

    with open(result_path, encoding="utf-8") as handle:
        verdict = handle.read().strip()

    return parse_result(verdict, os.path.join(request.work_dir, REPORT_FILE))
