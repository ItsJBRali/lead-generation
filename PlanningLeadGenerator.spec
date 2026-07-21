# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

SELENIUM_BROWSER_IMPORTS = [
    'selenium.webdriver.chrome.options',
    'selenium.webdriver.chrome.service',
    'selenium.webdriver.chrome.webdriver',
    'selenium.webdriver.edge.options',
    'selenium.webdriver.edge.service',
    'selenium.webdriver.edge.webdriver',
]

RAPID_OCR_DATAS, RAPID_OCR_BINARIES, RAPID_OCR_IMPORTS = collect_all('rapidocr')


a = Analysis(
    ['src\\lead_generator\\planning\\gui.py'],
    pathex=['src'],
    binaries=RAPID_OCR_BINARIES,
    datas=[
        ('src\\lead_generator\\planning\\data\\planning_authorities.geojson', 'lead_generator\\planning\\data'),
        *RAPID_OCR_DATAS,
    ],
    hiddenimports=SELENIUM_BROWSER_IMPORTS + RAPID_OCR_IMPORTS,
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
