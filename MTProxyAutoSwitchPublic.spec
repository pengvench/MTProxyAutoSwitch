# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['mtproxy_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('img/icon.ico', 'img'),
        ('img/dancecardiscordrtc.mp4', 'img'),
    ],
    hiddenimports=[
        'customtkinter',
        'darkdetect',
        'imageio',
        'imageio_ffmpeg',
        'pystray',
        'qrcode',
        'TelethonFakeTLS',
        'win32crypt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

icon_path = 'img/icon.ico'

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MTProxyAutoSwitchPublic',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MTProxyAutoSwitchPublic',
)
