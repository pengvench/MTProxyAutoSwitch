# PyInstaller runtime hook: patch imageio_ffmpeg to find bundled ffmpeg.
import sys
import pathlib

if getattr(sys, "frozen", False):
    _meipass = pathlib.Path(sys._MEIPASS)
    # ffmpeg binary is bundled in the root of _MEIPASS
    for _pattern in ("ffmpeg*.exe", "ffmpeg*"):
        _candidates = sorted(_meipass.glob(_pattern))
        if _candidates:
            os.environ.setdefault("IMAGEIO_FFMPEG_EXE", str(_candidates[0]))
            break
