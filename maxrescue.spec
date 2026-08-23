# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the MaxRescue Windows app.

    pyinstaller maxrescue.spec

`scripts/` is bundled as DATA rather than code. `3dsmaxbatch.exe` is a separate
process that opens `run_rescue.ms` and `rescue.py` itself, so they must land as
real files on disk — PyInstaller unpacks them beside the executable at runtime
and `runner.repo_root()` resolves to that directory.

The research reports and the .venv are excluded: they are for reading, not
running, and would multiply the download for no benefit.
"""

block_cipher = None

a = Analysis(
    ["maxrescue/ui/app.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("scripts/rescue.py", "scripts"),
        ("scripts/run_rescue.ms", "scripts"),
        ("scripts/reference.py", "scripts"),
        ("scripts/run_reference.ms", "scripts"),
        ("scripts/spikes.py", "scripts"),
        ("scripts/run_spikes.ms", "scripts"),
    ],
    hiddenimports=["olefile"],
    hookspath=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MaxRescue",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # No console window: this is a desktop app, and a black box appearing
    # behind it looks like something went wrong.
    console=False,
)
