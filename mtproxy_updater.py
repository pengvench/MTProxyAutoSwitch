from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mtproxy_net import create_verified_ssl_context

APP_PUBLIC_VERSION = "1.2"
GITHUB_REPO = "pengvench/MTProxyAutoSwitch"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
PUBLIC_WINDOWS_ASSET = "MTProxyAutoSwitchPublic.zip"
USER_AGENT = "MTProxyAutoSwitchUpdater/1.2"
URLLIB_SSL_CONTEXT = create_verified_ssl_context()


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class ReleaseInfo:
    tag_name: str
    name: str
    html_url: str
    published_at: str
    body: str
    asset: ReleaseAsset | None


def fetch_latest_release(timeout: float = 15.0) -> ReleaseInfo:
    try:
        return _fetch_latest_release_via_api(timeout)
    except Exception:
        return _fetch_latest_release_via_page(timeout)


def is_newer_version(current_version: str, latest_tag: str) -> bool:
    current_tuple = _version_key(current_version)
    latest_tuple = _version_key(latest_tag)
    if latest_tuple == current_tuple:
        return False
    return latest_tuple > current_tuple


def prepare_windows_update(
    *,
    install_dir: Path,
    state_dir: Path,
    current_version: str = APP_PUBLIC_VERSION,
    progress_sink: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    release = fetch_latest_release()
    if not release.tag_name:
        raise RuntimeError("release_tag_missing")
    if not is_newer_version(current_version, release.tag_name):
        return {"available": False, "release": release}
    if release.asset is None or not release.asset.url:
        raise RuntimeError("release_asset_missing")

    updates_dir = state_dir / "updates"
    stage_dir = updates_dir / release.tag_name.replace("/", "_")
    extract_dir = stage_dir / "extract"
    zip_path = stage_dir / release.asset.name
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    _log(progress_sink, "Скачивание обновления...")
    _download_file(release.asset.url, zip_path)
    _log(progress_sink, "Распаковка обновления...")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)

    source_dir = _resolve_update_root(extract_dir)
    exe_name = _detect_executable_name(install_dir)
    script_path = stage_dir / "apply_update.bat"
    script_path.write_text(
        _build_update_script(
            source_dir=source_dir,
            install_dir=install_dir,
            exe_name=exe_name,
        ),
        encoding="utf-8",
    )

    return {
        "available": True,
        "release": release,
        "script_path": str(script_path),
        "source_dir": str(source_dir),
        "asset_name": release.asset.name,
    }


def launch_windows_update(script_path: str) -> None:
    subprocess.Popen(
        ["cmd.exe", "/c", script_path],
        cwd=str(Path(script_path).resolve().parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _download_file(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=30.0, context=URLLIB_SSL_CONTEXT) as response, path.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _resolve_update_root(root: Path) -> Path:
    entries = [item for item in root.iterdir() if item.name not in {"__MACOSX"}]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def _detect_executable_name(install_dir: Path) -> str:
    preferred = ["MTProxyAutoSwitchPublic.exe", "MTProxyAutoSwitch.exe"]
    for name in preferred:
        if (install_dir / name).exists():
            return name
    for path in install_dir.glob("*.exe"):
        return path.name
    return "MTProxyAutoSwitchPublic.exe"


def _build_update_script(*, source_dir: Path, install_dir: Path, exe_name: str) -> str:
    src = str(source_dir.resolve())
    dst = str(install_dir.resolve())
    exe = str((install_dir / exe_name).resolve())
    return (
        "@echo off\n"
        "setlocal\n"
        "timeout /t 5 /nobreak >nul\n"
        f'robocopy "{src}" "{dst}" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul\n'
        f'if exist "{exe}" start "" "{exe}"\n'
        'del "%~f0"\n'
        "endlocal\n"
    )


def _version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value))
    if not numbers:
        return (0,)
    return tuple(int(item) for item in numbers)


def _log(progress_sink: Callable[[str], None] | None, message: str) -> None:
    if callable(progress_sink):
        progress_sink(message)


def _fetch_latest_release_via_api(timeout: float) -> ReleaseInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout, context=URLLIB_SSL_CONTEXT) as response:
        payload = json.loads(response.read().decode("utf-8"))

    assets = payload.get("assets", []) or []
    selected_asset: ReleaseAsset | None = None
    for item in assets:
        name = str(item.get("name") or "")
        if name == PUBLIC_WINDOWS_ASSET:
            selected_asset = ReleaseAsset(
                name=name,
                url=str(item.get("browser_download_url") or ""),
                size=int(item.get("size") or 0),
            )
            break
    if selected_asset is None:
        for item in assets:
            name = str(item.get("name") or "")
            if name.lower().endswith(".zip"):
                selected_asset = ReleaseAsset(
                    name=name,
                    url=str(item.get("browser_download_url") or ""),
                    size=int(item.get("size") or 0),
                )
                break

    return ReleaseInfo(
        tag_name=str(payload.get("tag_name") or ""),
        name=str(payload.get("name") or ""),
        html_url=str(payload.get("html_url") or ""),
        published_at=str(payload.get("published_at") or ""),
        body=str(payload.get("body") or ""),
        asset=selected_asset,
    )


def _fetch_latest_release_via_page(timeout: float) -> ReleaseInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_PAGE_URL,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout, context=URLLIB_SSL_CONTEXT) as response:
        final_url = str(response.geturl() or LATEST_RELEASE_PAGE_URL)
        html = response.read().decode("utf-8", errors="ignore")

    tag_name = ""
    marker = "/releases/tag/"
    if marker in final_url:
        tag_name = final_url.split(marker, 1)[1].strip().strip("/")
    if not tag_name:
        match = re.search(r"/releases/tag/([^\"'#?]+)", html)
        if match is not None:
            tag_name = match.group(1).strip().strip("/")

    asset = ReleaseAsset(
        name=PUBLIC_WINDOWS_ASSET,
        url=f"https://github.com/{GITHUB_REPO}/releases/latest/download/{PUBLIC_WINDOWS_ASSET}",
        size=0,
    )
    return ReleaseInfo(
        tag_name=tag_name,
        name=tag_name,
        html_url=final_url,
        published_at="",
        body="",
        asset=asset,
    )
