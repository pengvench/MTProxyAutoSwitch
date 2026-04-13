# PyInstaller runtime hook: patch imageio_ffmpeg to find bundled ffmpeg
# and ensure MTPROXY_PUBLIC_RELEASE is set.
import os
import sys
import pathlib

os.environ.setdefault("MTPROXY_PUBLIC_RELEASE", "1")

if getattr(sys, "frozen", False):
    _meipass = pathlib.Path(sys._MEIPASS)
    # ffmpeg binary is bundled in the root of _MEIPASS
    for _pattern in ("ffmpeg*.exe", "ffmpeg*"):
        _candidates = sorted(_meipass.glob(_pattern))
        if _candidates:
            os.environ.setdefault("IMAGEIO_FFMPEG_EXE", str(_candidates[0]))
            break
