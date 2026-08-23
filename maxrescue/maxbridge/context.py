"""Wire the real bridge into a context the pure engine can drive.

The only place the concrete Max implementations are named. Everything upstream
talks to the ports, which is what lets the entire engine be tested on a machine
with no 3ds Max on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxrescue.maxbridge.backup import MaxBackupService
from maxrescue.maxbridge.fix_services import MaxFixServices
from maxrescue.maxbridge.guard_bridge import MaxGuardServices
from maxrescue.maxbridge.memory import MaxMemoryProbe
from maxrescue.maxbridge.merge import MaxMergeServices
from maxrescue.maxbridge.render_services import MaxRenderServices
from maxrescue.maxbridge.scene_query import MaxSceneQuery
from maxrescue.maxbridge.undo import MaxUndoService


@dataclass
class MaxContext:
    query: MaxSceneQuery
    fixes: MaxFixServices
    merge: MaxMergeServices
    guard: MaxGuardServices
    undo: MaxUndoService
    backup: MaxBackupService
    memory: MaxMemoryProbe
    render: MaxRenderServices


def build_context() -> MaxContext:
    memory = MaxMemoryProbe()
    return MaxContext(
        query=MaxSceneQuery(memory_probe=memory),
        fixes=MaxFixServices(),
        merge=MaxMergeServices(),
        guard=MaxGuardServices(),
        undo=MaxUndoService(),
        backup=MaxBackupService(),
        memory=memory,
        render=MaxRenderServices(),
    )
