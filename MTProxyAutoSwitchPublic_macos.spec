# -*- mode: python ; coding: utf-8 -*-
import os
import tempfile
import pathlib

os.environ['MTPROXY_PUBLIC_RELEASE'] = '1'

from PyInstaller.utils.hooks import collect_all

imageio_datas, imageio_binaries, imageio_hiddenimports = collect_all('imageio')
imageio_ffmpeg_datas, imageio_ffmpeg_binaries, imageio_ffmpeg_hidden = collect_all('imageio_ffmpeg')

_hook_code = (
    "import os, sys, pathlib\n"
    "os.environ.setdefault('MTPROXY_PUBLIC_RELEASE', '1')\n"
    "if getattr(sys, 'frozen', False):\n"
    "    _d = pathlib.Path(sys._MEIPASS)\n"
    "    for _p in ('imageio_ffmpeg/binaries/ffmpeg*.exe',\n"
    "               'imageio_ffmpeg/binaries/ffmpeg*',\n"
    "               'ffmpeg*.exe', 'ffmpeg*'):\n"
    "        _c = sorted(_d.glob(_p))\n"
    "        if _c:\n"
    "            os.environ.setdefault('IMAGEIO_FFMPEG_EXE', str(_c[0]))\n"
    "            break\n"
)
_hook_path = pathlib.Path(tempfile.gettempdir()) / "_mtproxy_public_hook.py"
_hook_path.write_text(_hook_code, encoding="utf-8")

a = Analysis(
    ['mtproxy_gui.py'],
    pathex=[],
    binaries=imageio_binaries + imageio_ffmpeg_binaries,
    datas=(
        imageio_datas
        + imageio_ffmpeg_datas
        + [
            ('img/icon.ico', 'img'),
            ('img/dancecardiscordrtc.mp4', 'img'),
        ]
    ),
    hiddenimports=(
        imageio_hiddenimports
        + imageio_ffmpeg_hidden
        + [
            'customtkinter',
            'darkdetect',
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
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(_hook_path)],
    excludes=['win32crypt'],
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
