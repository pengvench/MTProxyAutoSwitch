# -*- mode: python ; coding: utf-8 -*-
import os
import imageio_ffmpeg
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
os.environ['MTPROXY_PUBLIC_RELEASE'] = '1'

# Write a minimal runtime hook inline so no external file is needed
import tempfile, pathlib
_hook_code = "import os\nos.environ.setdefault('MTPROXY_PUBLIC_RELEASE', '1')\n"
_hook_path = pathlib.Path(tempfile.gettempdir()) / "_mtproxy_public_hook.py"
_hook_path.write_text(_hook_code, encoding="utf-8")

a = Analysis(
    ['mtproxy_gui.py'],
    pathex=[],
    binaries=[(ffmpeg_exe, '.')],
    datas=[
        ('img/icon.ico', 'img'),
        ('img/dancecardiscordrtc.mp4', 'img'),
    ],
    hiddenimports=[
        'customtkinter',
        'darkdetect',
        'imageio',
	'imageio.plugins.ffmpeg',
        'imageio_ffmpeg',
        'pystray',
        'qrcode',
        'TelethonFakeTLS',
        'telethon',
        'cryptography',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'win32crypt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(_hook_path)],
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
