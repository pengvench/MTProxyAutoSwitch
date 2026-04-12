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
        'telethon',
        'cryptography',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'objc',
        'Foundation',
        'AppKit',
        'Quartz',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'win32crypt',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

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
    target_arch='universal2',
    codesign_identity=None,
    entitlements_file=None,
    icon=[],
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

app = BUNDLE(
    coll,
    name='MTProxyAutoSwitchPublic.app',
    icon=None,
    bundle_identifier='com.mtproxyautoswitch.public',
    info_plist={
        'CFBundleName': 'MTProxy AutoSwitch Public',
        'CFBundleDisplayName': 'MTProxy AutoSwitch Public',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'LSMinimumSystemVersion': '10.15',
        'NSHighResolutionCapable': True,
        'NSAppleEventsUsageDescription':
            'MTProxy AutoSwitch Public may open Telegram proxy links in the Telegram app.',
    },
)
