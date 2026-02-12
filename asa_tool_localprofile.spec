# -*- mode: python ; coding: utf-8 -*-
# Build: pyinstaller asa_tool_localprofile.spec


a = Analysis(
    ['asa_tool_localprofile.py'],
    pathex=[],
    binaries=[],
    datas=[('asaplayerlocaldatafixer', 'asaplayerlocaldatafixer')],
    hiddenimports=[],
    hookspath=[],
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
    name='asa_tool_localprofile',
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
