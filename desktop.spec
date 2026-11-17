# -*- mode: python ; coding: utf-8 -*-
"""Build a self-contained Windows desktop EXE (folder distribution)."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH)
rdkit_datas, rdkit_binaries, rdkit_hidden = collect_all("rdkit")

a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT), str(ROOT / "engine")],
    binaries=rdkit_binaries,
    # Engine modules are imported as top-level modules; their __file__ is
    # _MEIPASS/module.py in PyInstaller. The approved CSVs must be in data/.
    datas=[(str(ROOT / "engine" / "data"), "data")] + rdkit_datas + collect_data_files("tzdata"),
    hiddenimports=rdkit_hidden + ["tzdata"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["streamlit", "plotly", "pytest", "matplotlib", "torch", "tensorflow"],
    noarchive=False,
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ChemicalRegulatoryDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ChemicalRegulatoryDesktop",
)
