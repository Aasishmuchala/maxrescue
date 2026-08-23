"""Undo transactions over `theHold`.

NOT `pymxs.undo`, which swallows exceptions — a failed operation would appear to
succeed and leave the scene half-modified. `theHold` is strict LIFO and re-raises,
which is what makes rollback real.

In batch mode Max's undo does **not** restore; it is GUI-only. The backup file is
the real safety net. This keeps in-session state coherent so a failed op does not
poison the ops after it.
"""

from __future__ import annotations

from contextlib import contextmanager

import pymxs

rt = pymxs.runtime


class MaxUndoTransaction:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class MaxUndoService:
    """Transactions over `theHold`.

    When a hold is ALREADY open — another tool, or a nested call — this cannot
    start its own, and therefore cannot roll back. Previously it silently did
    nothing in that case, so a failing operation stayed half-applied while the
    caller believed it had been undone. Now it records the fact, and the run
    report carries it.
    """

    def __init__(self) -> None:
        self.notes: list[str] = []

    @contextmanager
    def transaction(self, label: str):
        began = False
        try:
            if not rt.theHold.Holding():
                rt.theHold.Begin()
                began = True
        except Exception as exc:
            began = False
            self.notes.append(f"could not begin an undo transaction: {exc}")

        if not began:
            # Not a warning to swallow: without a hold of our own, a failure
            # inside this block will NOT be rolled back.
            self.notes.append(
                f"'{label}' ran without its own undo transaction — a hold was "
                "already open, so a failure here would not be rolled back. The "
                "backup file is the only recovery."
            )

        txn = MaxUndoTransaction()
        try:
            yield txn
        except Exception:
            if began:
                try:
                    rt.theHold.Cancel()
                except Exception:
                    pass
            raise

        if not began:
            return
        try:
            if txn.cancelled:
                rt.theHold.Cancel()
            else:
                rt.theHold.Accept(label)
        except Exception:
            pass
