# PyInstaller runtime hook: sets MTPROXY_PUBLIC_RELEASE for frozen builds.
import os
os.environ.setdefault("MTPROXY_PUBLIC_RELEASE", "1")
