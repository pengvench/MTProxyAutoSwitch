# -*- mode: python ; coding: utf-8 -*-
# Правильная сборка: collect_all() копирует imageio как data-файлы
# (v2.py, plugins/*.py, config/*.py) и ffmpeg в imageio_ffmpeg/binaries/
import tempfile
import pathlib

from PyInstaller.utils.hooks import collect_all

# Собираем imageio полностью: datas + hiddenimports
imageio_datas, imageio_binaries, imageio_hiddenimports = collect_all('imageio')

# Собираем imageio_ffmpeg: ffmpeg-бинарник попадёт в imageio_ffmpeg/binaries/
imageio_ffmpeg_datas, imageio_ffmpeg_binaries, imageio_ffmpeg_hidden = collect_all('imageio_ffmpeg')

# Runtime hook
_hook_code = (
    "import os, sys, pathlib\n"
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
            'win32crypt',
        ]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(_hook_path)],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MTProxyAutoSwitch',
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
    icon='img/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MTProxyAutoSwitch',
)
