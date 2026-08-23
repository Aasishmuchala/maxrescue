"""Settings, deliberately hard to break.

Loading is garbage-tolerant by design: a corrupt or hand-edited settings file
must never stop a rescue. Every field is validated individually and anything
unusable falls back to its default, because a tool that refuses to run because
of its own config file is worse than one that runs with sensible numbers.

JSON only, no UI. The file is written on first load so the thresholds are
discoverable without documentation.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from maxrescue.core.plan import PlanConfig

APP_DIR = "MaxRescue"


@dataclass
class Settings:
    ram_budget_gb: float = 70.0
    per_node_face_threshold: int = 30_000
    proxy_out_dir: str = "proxies"
    nitrous_texture_limit: int = 512
    delete_hidden: bool = True
    collapse_stacks: bool = True
    convert_bitmaps: bool = False
    reduce_each_batch: bool = True

    # -- location ---------------------------------------------------------

    @staticmethod
    def directory() -> str:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return os.path.join(base, APP_DIR)
        return os.path.join(os.path.expanduser("~"), f".{APP_DIR.lower()}")

    @classmethod
    def path(cls) -> str:
        return os.path.join(cls.directory(), "settings.json")

    # -- io ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        target = path or cls.path()
        defaults = cls()
        try:
            with open(target, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return defaults
        # A JSON file whose top level is a list or a string is not a config.
        if not isinstance(data, dict):
            return defaults

        out = cls()
        for field_name, default in asdict(defaults).items():
            if field_name not in data:
                continue
            value = data[field_name]
            if isinstance(default, bool):
                if isinstance(value, bool):
                    setattr(out, field_name, value)
            elif isinstance(default, int) and not isinstance(default, bool):
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    setattr(out, field_name, value)
            elif isinstance(default, float):
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    setattr(out, field_name, float(value))
            elif isinstance(default, str):
                if isinstance(value, str) and value.strip():
                    setattr(out, field_name, value.strip())
        return out

    def save(self, path: str | None = None) -> None:
        """Never raises. A settings file that cannot be written is an
        inconvenience, not a reason to abandon a rescue."""
        target = path or self.path()
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(asdict(self), handle, indent=2)
        except Exception:
            pass

    # -- projection -------------------------------------------------------

    def plan_config(self) -> PlanConfig:
        return PlanConfig(
            per_node_face_threshold=self.per_node_face_threshold,
            proxy_out_dir=self.proxy_out_dir,
            nitrous_texture_limit=self.nitrous_texture_limit,
            delete_hidden=self.delete_hidden,
            collapse_stacks=self.collapse_stacks,
            convert_bitmaps=self.convert_bitmaps,
        )

    @property
    def ram_budget_bytes(self) -> int:
        return int(self.ram_budget_gb * (1 << 30))
