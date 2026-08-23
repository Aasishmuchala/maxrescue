"""The render-identity gate.

Every automatic stage claims to be bit-identical. This is where that claim is
checked rather than asserted, by rendering the same frame before and after and
comparing the pixels with OpenImageIO's `idiff`.

Two thresholds, and the difference between them is the whole point:

* **Automatic stages must be bit-exact.** Any non-zero pixel difference is a
  failure. There is no "close enough" — the contract is identity, and a stage
  that shifts a pixel is a stage that was mis-specified.
* **The opt-in bitmap stage is compared with a tolerance**, because
  Bitmap→VRayBitmap changes the filtering kernel by design. Chaos document this.
  A tolerant pass here is a prompt for a human to look, never an approval.

Determinism is forced on the render side (locked noise, fixed seed, bucket
sampler, light cache from file). Without that, two renders of an *unmodified*
scene differ and this gate would reject everything, which is worse than useless
because it would quickly be switched off.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

__all__ = ["VerifyResult", "Tolerance", "compare", "IDIFF_EXIT"]

#: idiff's documented exit codes.
IDIFF_EXIT = {
    0: "identical",
    1: "differences within the warning threshold",
    2: "differences beyond the failure threshold",
    3: "images are different sizes",
    4: "file could not be read",
}


@dataclass(frozen=True)
class Tolerance:
    fail: float = 0.0
    fail_percent: float = 0.0
    hard_fail: float = 0.0
    perceptual: bool = False

    @classmethod
    def bit_exact(cls) -> Tolerance:
        """For every automatic stage. Anything above zero is a failure."""
        return cls()

    @classmethod
    def filtering_change(cls) -> Tolerance:
        """For the opt-in bitmap stage only, where the kernel legitimately
        changes. A pass here means 'worth a human looking', not 'approved'."""
        return cls(fail=0.004, fail_percent=0.5, hard_fail=0.05, perceptual=True)


@dataclass(frozen=True)
class VerifyResult:
    identical: bool
    passed: bool
    exit_code: int
    summary: str
    command: tuple[str, ...] = ()
    output: str = ""
    tolerant: bool = False

    def describe(self) -> str:
        if self.identical:
            return "renders are bit-identical"
        if self.passed and self.tolerant:
            return (
                f"renders differ within the allowed tolerance — {self.summary}. "
                "This stage changes filtering by design; review the diff before "
                "accepting it."
            )
        return f"RENDERS DIFFER — {self.summary}"


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


def _run(command: Sequence[str]):
    return subprocess.run(
        list(command), capture_output=True, text=True, timeout=600, check=False
    )


def compare(
    before: str,
    after: str,
    tolerance: Tolerance | None = None,
    runner: Runner | None = None,
    tool: str | None = None,
) -> VerifyResult:
    """Compare two rendered frames.

    Returns rather than raises when `idiff` is missing: a rescue that produced a
    good scene should not be reported as a failure because a comparison tool is
    not installed. But it is never reported as *passed* either — an unverified
    result is its own state.
    """
    tolerance = tolerance or Tolerance.bit_exact()
    executable = tool or shutil.which("idiff") or shutil.which("oiiotool")
    if not executable:
        return VerifyResult(
            identical=False,
            passed=False,
            exit_code=-1,
            summary=(
                "idiff not found — install OpenImageIO to verify. The render was "
                "NOT compared; treat this as unverified, not as a pass."
            ),
        )

    command = [executable]
    if tolerance.fail:
        command += ["-fail", str(tolerance.fail)]
    if tolerance.fail_percent:
        command += ["-failpercent", str(tolerance.fail_percent)]
    if tolerance.hard_fail:
        command += ["-hardfail", str(tolerance.hard_fail)]
    if tolerance.perceptual:
        command += ["-p"]
    command += [before, after]

    try:
        completed = (runner or _run)(command)
    except Exception as exc:
        return VerifyResult(
            identical=False,
            passed=False,
            exit_code=-1,
            summary=f"comparison could not run: {exc}",
            command=tuple(command),
        )

    code = completed.returncode
    output = (getattr(completed, "stdout", "") or "") + (
        getattr(completed, "stderr", "") or ""
    )
    summary = IDIFF_EXIT.get(code, f"unexpected exit code {code}")
    tolerant = tolerance != Tolerance.bit_exact()

    identical = code == 0
    # Exit 1 is "within the warning threshold" — acceptable only where a
    # tolerance was deliberately allowed.
    passed = identical or (code == 1 and tolerant)

    return VerifyResult(
        identical=identical,
        passed=passed,
        exit_code=code,
        summary=summary,
        command=tuple(command),
        output=output.strip()[:2000],
        tolerant=tolerant,
    )
