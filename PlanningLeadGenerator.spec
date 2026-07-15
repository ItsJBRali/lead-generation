# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\lead_generator\\planning\\gui.py'],
    pathex=['src'],
    binaries=[],
    datas=[('src\\lead_generator\\planning\\data\\planning_authorities.geojson', 'lead_generator\\planning\\data')],
    hiddenimports=[],
    hookspath=['tools\\pyinstaller_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PlanningLeadGenerator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
