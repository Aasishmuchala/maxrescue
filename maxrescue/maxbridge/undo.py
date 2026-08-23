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
    @contextmanager
    def transaction(self, label: str):
        began = False
        try:
            if not rt.theHold.Holding():
                rt.theHold.Begin()
                began = True
        except Exception:
            began = False

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
