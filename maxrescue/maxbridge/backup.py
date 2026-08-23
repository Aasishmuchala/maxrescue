"""Backup before any mutation. A failed backup aborts the run.

`useNewFile:false` matters: without it the backup becomes the open document, and
the next save writes over the wrong file. That is a data-loss bug, not a
cosmetic one.
"""

from __future__ import annotations

import ntpath
import os

import pymxs

rt = pymxs.runtime

SUBDIR = "maxrescue_backups"


class MaxBackupService:
    def __init__(self, subdir: str = SUBDIR) -> None:
        self.subdir = subdir

    def _scene_folder(self) -> tuple[str, str]:
        # An empty MXS string wrapper is TRUTHY in Python, so `or` does not fall
        # through. Coerce with str() first.
        folder = str(rt.maxFilePath or "")
        name = str(rt.maxFileName or "")
        if not folder:
            folder = str(rt.getDir(rt.name("autoback")) or os.getcwd())
        if not name:
            name = "untitled.max"
        return folder, name

    def create(self) -> str:
        folder, name = self._scene_folder()
        stem = ntpath.splitext(name)[0] or "untitled"
        target_dir = ntpath.join(folder, self.subdir)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            raise IOError(f"cannot create backup folder {target_dir}: {exc}") from exc

        for index in range(1, 1000):
            path = ntpath.join(target_dir, f"{stem}_maxrescue_backup_{index:03d}.max")
            if not os.path.exists(path):
                break
        else:
            raise IOError("more than 999 backups already exist; clean the folder")

        ok = rt.saveMaxFile(path, useNewFile=False, quiet=True, clearNeedSaveFlag=False)
        if not ok or not os.path.exists(path):
            raise IOError(f"saveMaxFile reported failure for {path}")
        # A 0-byte or stub file passes an existence check, and the run then
        # proceeds believing it has a safety net it does not have.
        size = os.path.getsize(path)
        if size < 4096:
            raise IOError(
                f"backup at {path} is only {size} bytes — no .max file is that "
                "small, so this is not a usable backup"
            )
        return path
