"""The 3ds Max bridge — the ONLY package permitted to import pymxs.

Nothing here is importable outside Max, which is exactly why `core/` and
`xray/` must never reach into it. `tests/test_no_max_imports.py` enforces that
in both directions.
"""
