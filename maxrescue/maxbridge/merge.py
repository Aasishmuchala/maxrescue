"""Reading a scene in pieces, without ever opening it whole.

`getMAXFileObjectNames` lists a file's objects without loading the scene, and
`mergeMaxFile` brings in only the named ones. That pair is the whole premise of
this tool.

The single most important detail here is the **explicit flags**. The interactive
defaults are `#promptMtlDups` and `#promptReparent`, both of which open a dialog.
In `3dsmaxbatch` there is nobody to click it, so the run hangs until the harness
timeout kills it — and the log says nothing useful.
"""

from __future__ import annotations

from typing import Sequence

import pymxs

rt = pymxs.runtime


class MaxMergeServices:
    def object_names(self, path: str) -> tuple[list[str], list[str]]:
        missing = []
        names = rt.getMAXFileObjectNames(
            path,
            quiet=True,
            missingDLLsAction=rt.name("logmsg"),
            missingDLLsList=pymxs.byref(missing),
        )
        return (
            [str(n) for n in names] if names else [],
            [str(d) for d in missing] if missing else [],
        )

    def merge(self, path: str, names: Sequence[str]) -> tuple[list[str], list[str]]:
        wanted = [str(n) for n in names]
        if not wanted:
            return [], []

        merged = []
        missing_files, missing_dlls, missing_xrefs = [], [], []

        ok = rt.mergeMaxFile(
            path,
            wanted,
            # Every policy stated explicitly. Omitting any of these falls back to
            # a prompting default that hangs a headless run.
            rt.name("noRedraw"),
            rt.name("mergeDups"),
            rt.name("useSceneMtlDups"),
            rt.name("neverReparent"),
            quiet=True,
            mergedNodes=pymxs.byref(merged),
            missingExtFilesAction=rt.name("logmsg"),
            missingExtFilesList=pymxs.byref(missing_files),
            missingDLLsAction=rt.name("logmsg"),
            missingDLLsList=pymxs.byref(missing_dlls),
            missingXRefsAction=rt.name("logmsg"),
            missingXRefsList=pymxs.byref(missing_xrefs),
        )

        arrived = []
        for node in merged or []:
            try:
                arrived.append(str(node.name))
            except Exception:
                continue

        problems: list[str] = []
        if not ok:
            problems.append("mergeMaxFile returned false")
        for label, items in (
            ("missing plugin", missing_dlls),
            ("missing external file", missing_files),
            ("missing xref", missing_xrefs),
        ):
            for item in items or []:
                problems.append(f"{label}: {item}")

        # Name-by-name accounting. Merge is case-sensitive on names, and a
        # shortfall reported as a total hides WHICH object never arrived.
        landed = set(arrived)
        for name in wanted:
            if name not in landed:
                problems.append(f"{name}: requested but not merged")

        return arrived, problems

    def reset_scene(self) -> None:
        rt.resetMaxFile(rt.name("noPrompt"))

    def save_as(self, path: str) -> bool:
        try:
            return bool(rt.saveMaxFile(path, useNewFile=False, quiet=True))
        except Exception:
            return False
