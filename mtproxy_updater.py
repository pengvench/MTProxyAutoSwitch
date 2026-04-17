from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mtproxy_net import create_verified_ssl_context

APP_VERSION = "1.2"
APP_PUBLIC_VERSION = APP_VERSION
GITHUB_REPO = "pengvench/MTProxyAutoSwitch"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
WINDOWS_INSTALLER_ASSET_CANDIDATES = ("MTProxyAutoSwitch-Setup.exe",)
WINDOWS_ARCHIVE_ASSET_CANDIDATES = ("MTProxyAutoSwitch.zip", "MTProxyAutoSwitchPublic.zip")
MACOS_INSTALLER_ASSET_CANDIDATES = ("MTProxyAutoSwitch.pkg",)
MACOS_ARCHIVE_ASSET_CANDIDATES = ("MTProxyAutoSwitch.dmg",)
USER_AGENT = "MTProxyAutoSwitchUpdater/1.2"
URLLIB_SSL_CONTEXT = create_verified_ssl_context()
WINDOWS_DEFAULT_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Programs" / "MTProxy AutoSwitch"


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
    assets: tuple[ReleaseAsset, ...]


@dataclass(frozen=True)
class PreparedUpdate:
    platform: str
    release: ReleaseInfo
    asset: ReleaseAsset
    launch_path: str
    kind: str
    asset_path: str


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


def is_update_available(
    current_version: str,
    release: ReleaseInfo,
    *,
    platform_name: str | None = None,
    install_dir: Path | None = None,
) -> bool:
    if is_newer_version(current_version, release.tag_name):
        return True
    if install_dir is None:
        return False
    return _can_offer_installer_migration(
        release,
        platform_name=(platform_name or sys.platform),
        install_dir=install_dir,
    )


def prepare_update(
    *,
    install_dir: Path,
    state_dir: Path,
    current_version: str = APP_VERSION,
    platform_name: str | None = None,
    progress_sink: Callable[[str], None] | None = None,
) -> PreparedUpdate:
    current_platform = (platform_name or sys.platform).strip().lower()
    release = fetch_latest_release()
    if not release.tag_name:
        raise RuntimeError("release_tag_missing")
    if not is_update_available(
        current_version,
        release,
        platform_name=current_platform,
        install_dir=install_dir,
    ):
        raise RuntimeError("release_is_current")

    candidate_assets = _candidate_assets_for_platform(release, platform_name=current_platform)
    if not candidate_assets:
        raise RuntimeError("release_asset_missing")

    updates_dir = state_dir / "updates"
    stage_dir = updates_dir / release.tag_name.replace("/", "_")
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    asset: ReleaseAsset | None = None
    asset_path: Path | None = None
    download_error: Exception | None = None
    for candidate in candidate_assets:
        try:
            candidate_path = stage_dir / candidate.name
            _log(progress_sink, "Скачивание обновления...")
            _download_file(candidate.url, candidate_path)
            asset = candidate
            asset_path = candidate_path
            download_error = None
            break
        except Exception as exc:
            download_error = exc
    if asset is None or asset_path is None:
        if download_error is not None:
            raise download_error
        raise RuntimeError("release_asset_missing")

    if current_platform == "win32":
        launch_path, kind = _prepare_windows_update(
            asset=asset,
            asset_path=asset_path,
            install_dir=install_dir,
            state_dir=state_dir,
            progress_sink=progress_sink,
        )
    elif current_platform == "darwin":
        launch_path, kind = _prepare_macos_update(asset=asset, asset_path=asset_path)
    else:
        raise RuntimeError("update_install_not_supported")

    return PreparedUpdate(
        platform=current_platform,
        release=release,
        asset=asset,
        launch_path=str(launch_path),
        kind=kind,
        asset_path=str(asset_path),
    )


def prepare_windows_update(
    *,
    install_dir: Path,
    state_dir: Path,
    current_version: str = APP_VERSION,
    progress_sink: Callable[[str], None] | None = None,
) -> dict[str, object]:
    prepared = prepare_update(
        install_dir=install_dir,
        state_dir=state_dir,
        current_version=current_version,
        platform_name="win32",
        progress_sink=progress_sink,
    )
    return {
        "available": True,
        "release": prepared.release,
        "script_path": prepared.launch_path,
        "asset_name": prepared.asset.name,
        "kind": prepared.kind,
    }


def launch_prepared_update(update: PreparedUpdate | str) -> None:
    if isinstance(update, str):
        launch_path = update
        platform_name = sys.platform
    else:
        launch_path = update.launch_path
        platform_name = update.platform
    if platform_name == "win32":
        launch_windows_update(launch_path)
        return
    if platform_name == "darwin":
        launch_macos_update(launch_path)
        return
    raise RuntimeError("update_install_not_supported")


def launch_windows_update(script_path: str) -> None:
    subprocess.Popen(
        ["cmd.exe", "/c", script_path],
        cwd=str(Path(script_path).resolve().parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def launch_macos_update(script_path: str) -> None:
    subprocess.Popen(
        ["/bin/bash", script_path],
        cwd=str(Path(script_path).resolve().parent),
        start_new_session=True,
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
    preferred = ["MTProxyAutoSwitch.exe", "MTProxyAutoSwitchPublic.exe"]
    for name in preferred:
        if (install_dir / name).exists():
            return name
    for path in install_dir.glob("*.exe"):
        return path.name
    return "MTProxyAutoSwitch.exe"


def _prepare_windows_update(
    *,
    asset: ReleaseAsset,
    asset_path: Path,
    install_dir: Path,
    state_dir: Path,
    progress_sink: Callable[[str], None] | None = None,
) -> tuple[Path, str]:
    lower_name = asset.name.lower()
    if lower_name.endswith(".zip"):
        extract_dir = asset_path.parent / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _log(progress_sink, "Распаковка обновления...")
        with zipfile.ZipFile(asset_path) as archive:
            archive.extractall(extract_dir)
        source_dir = _resolve_update_root(extract_dir)
        exe_name = _detect_executable_name(install_dir)
        script_path = asset_path.parent / "apply_update.bat"
        script_path.write_text(
            _build_archive_update_script(
                source_dir=source_dir,
                install_dir=install_dir,
                exe_name=exe_name,
            ),
            encoding="utf-8",
        )
        return script_path, "archive"

    if lower_name.endswith(".exe"):
        _log(progress_sink, "Подготовка установщика...")
        script_path = asset_path.parent / "apply_installer_update.bat"
        script_path.write_text(
            _build_windows_installer_update_script(
                installer_path=asset_path,
                install_dir=install_dir,
                state_dir=state_dir,
            ),
            encoding="utf-8",
        )
        return script_path, "installer"

    raise RuntimeError("unsupported_windows_asset")


def _prepare_macos_update(*, asset: ReleaseAsset, asset_path: Path) -> tuple[Path, str]:
    script_path = asset_path.parent / "apply_update.command"
    script_path.write_text(_build_macos_update_script(asset_path=asset_path), encoding="utf-8")
    script_path.chmod(0o755)
    kind = "installer" if asset.name.lower().endswith(".pkg") else "archive"
    return script_path, kind


def _build_archive_update_script(*, source_dir: Path, install_dir: Path, exe_name: str) -> str:
    src = str(source_dir.resolve())
    dst = str(install_dir.resolve())
    exe = str((install_dir / exe_name).resolve())
    internal_dir = str((install_dir / "_internal").resolve())
    return (
        "@echo off\n"
        "setlocal\n"
        "timeout /t 3 /nobreak >nul\n"
        f'taskkill /f /im "{exe_name}" /t >nul 2>&1\n'
        "timeout /t 2 /nobreak >nul\n"
        f'robocopy "{src}" "{dst}" /E /R:10 /W:3 /NFL /NDL /NJH /NJS /NP /XD "updates" >nul\n'
        "if %errorlevel% leq 7 (\n"
        f'    if not exist "{exe}" echo Update failed: missing executable >> "%~dp0update_error.log"\n'
        f'    if not exist "{internal_dir}" echo Update failed: missing _internal dir >> "%~dp0update_error.log"\n'
        f'    if exist "{exe}" if exist "{internal_dir}" start "" /d "{dst}" "{exe}"\n'
        ") else (\n"
        '    echo Update failed with robocopy error %errorlevel% >> "%~dp0update_error.log"\n'
        ")\n"
        'del "%~f0"\n'
        "endlocal\n"
    )


def _build_windows_installer_update_script(*, installer_path: Path, install_dir: Path, state_dir: Path) -> str:
    installer = str(installer_path.resolve())
    current_install = str(install_dir.resolve())
    state_root = str(state_dir.resolve().parent)
    target_dir = str(_resolve_windows_target_install_dir(install_dir))
    exe_name = _detect_executable_name(install_dir)
    target_exe = str((Path(target_dir) / exe_name).resolve())
    return (
        "@echo off\n"
        "setlocal\n"
        f'set "INSTALLER={installer}"\n'
        f'set "CURRENT_INSTALL={current_install}"\n'
        f'set "STATE_ROOT={state_root}"\n'
        f'set "TARGET_DIR={target_dir}"\n'
        f'set "TARGET_EXE={target_exe}"\n'
        "timeout /t 2 /nobreak >nul\n"
        f'taskkill /f /im "{exe_name}" /t >nul 2>&1\n'
        "timeout /t 1 /nobreak >nul\n"
        'if not exist "%STATE_ROOT%" mkdir "%STATE_ROOT%" >nul 2>&1\n'
        'if not exist "%STATE_ROOT%\\config.json" if exist "%CURRENT_INSTALL%\\config.json" copy /Y "%CURRENT_INSTALL%\\config.json" "%STATE_ROOT%\\config.json" >nul 2>&1\n'
        'if not exist "%STATE_ROOT%\\.env" if exist "%CURRENT_INSTALL%\\.env" copy /Y "%CURRENT_INSTALL%\\.env" "%STATE_ROOT%\\.env" >nul 2>&1\n'
        'if exist "%CURRENT_INSTALL%\\list" if not exist "%STATE_ROOT%\\list" robocopy "%CURRENT_INSTALL%\\list" "%STATE_ROOT%\\list" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul\n'
        'if exist "%CURRENT_INSTALL%\\data" if not exist "%STATE_ROOT%\\data" robocopy "%CURRENT_INSTALL%\\data" "%STATE_ROOT%\\data" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul\n'
        'if exist "%CURRENT_INSTALL%\\mtproxy_output" if not exist "%STATE_ROOT%\\mtproxy_output" robocopy "%CURRENT_INSTALL%\\mtproxy_output" "%STATE_ROOT%\\mtproxy_output" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul\n'
        'if exist "%CURRENT_INSTALL%\\app_state" if not exist "%STATE_ROOT%\\app_state" robocopy "%CURRENT_INSTALL%\\app_state" "%STATE_ROOT%\\app_state" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul\n'
        'start /wait "" "%INSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CURRENTUSER /DIR="%TARGET_DIR%"\n'
        'if exist "%TARGET_EXE%" start "" /d "%TARGET_DIR%" "%TARGET_EXE%"\n'
        'del "%~f0"\n'
        "endlocal\n"
    )


def _build_macos_update_script(*, asset_path: Path) -> str:
    asset = str(asset_path.resolve()).replace('"', '\\"')
    return (
        "#!/bin/bash\n"
        "set -e\n"
        "sleep 1\n"
        f'open "{asset}"\n'
        'rm -- "$0"\n'
    )


def _resolve_windows_target_install_dir(install_dir: Path) -> Path:
    normalized = install_dir.resolve()
    default_dir = WINDOWS_DEFAULT_INSTALL_DIR.resolve()
    if normalized == default_dir:
        return normalized
    local_programs = default_dir.parent.resolve()
    if _is_relative_to(normalized, local_programs):
        return normalized
    return default_dir


def _can_offer_installer_migration(release: ReleaseInfo, *, platform_name: str, install_dir: Path) -> bool:
    asset = _select_release_asset(release, platform_name=platform_name)
    if asset is None:
        return False
    lower_name = asset.name.lower()
    if platform_name == "win32":
        return lower_name.endswith(".exe") and _resolve_windows_target_install_dir(install_dir) != install_dir.resolve()
    if platform_name == "darwin":
        support_dir = (Path.home() / "Library" / "Application Support" / "MTProxyAutoSwitch").resolve()
        return lower_name.endswith(".pkg") and install_dir.resolve() != support_dir
    return False


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value))
    if not numbers:
        return (0,)
    return tuple(int(item) for item in numbers)


def _log(progress_sink: Callable[[str], None] | None, message: str) -> None:
    if callable(progress_sink):
        progress_sink(message)


def _preferred_asset_names(platform_name: str) -> list[tuple[str, tuple[str, ...]]]:
    if platform_name == "win32":
        return [
            ("installer", WINDOWS_INSTALLER_ASSET_CANDIDATES),
            ("archive", WINDOWS_ARCHIVE_ASSET_CANDIDATES),
        ]
    if platform_name == "darwin":
        return [
            ("installer", MACOS_INSTALLER_ASSET_CANDIDATES),
            ("archive", MACOS_ARCHIVE_ASSET_CANDIDATES),
        ]
    return [("archive", WINDOWS_ARCHIVE_ASSET_CANDIDATES)]


def _select_release_asset(release: ReleaseInfo, *, platform_name: str) -> ReleaseAsset | None:
    assets = _candidate_assets_for_platform(release, platform_name=platform_name)
    return assets[0] if assets else None


def _candidate_assets_for_platform(release: ReleaseInfo, *, platform_name: str) -> list[ReleaseAsset]:
    assets = list(release.assets)
    ordered: list[ReleaseAsset] = []
    seen: set[str] = set()
    for _kind, names in _preferred_asset_names(platform_name):
        for preferred_name in names:
            for asset in assets:
                if asset.name != preferred_name:
                    continue
                if asset.name in seen:
                    continue
                seen.add(asset.name)
                ordered.append(asset)
    if platform_name == "win32":
        suffixes = (".exe", ".zip")
    elif platform_name == "darwin":
        suffixes = (".pkg", ".dmg")
    else:
        suffixes = (".zip",)
    for suffix in suffixes:
        for asset in assets:
            if not asset.name.lower().endswith(suffix):
                continue
            if asset.name in seen:
                continue
            seen.add(asset.name)
            ordered.append(asset)
    for asset in assets:
        if asset.name in seen:
            continue
        seen.add(asset.name)
        ordered.append(asset)
    return ordered


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

    assets_payload = payload.get("assets", []) or []
    assets = tuple(
        ReleaseAsset(
            name=str(item.get("name") or ""),
            url=str(item.get("browser_download_url") or ""),
            size=int(item.get("size") or 0),
        )
        for item in assets_payload
        if str(item.get("name") or "").strip()
    )

    return ReleaseInfo(
        tag_name=str(payload.get("tag_name") or ""),
        name=str(payload.get("name") or ""),
        html_url=str(payload.get("html_url") or ""),
        published_at=str(payload.get("published_at") or ""),
        body=str(payload.get("body") or ""),
        assets=assets,
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

    asset_names = (
        *WINDOWS_INSTALLER_ASSET_CANDIDATES,
        *WINDOWS_ARCHIVE_ASSET_CANDIDATES,
        *MACOS_INSTALLER_ASSET_CANDIDATES,
        *MACOS_ARCHIVE_ASSET_CANDIDATES,
    )
    assets = tuple(
        ReleaseAsset(
            name=name,
            url=f"https://github.com/{GITHUB_REPO}/releases/latest/download/{name}",
            size=0,
        )
        for name in asset_names
    )

    return ReleaseInfo(
        tag_name=tag_name,
        name=tag_name,
        html_url=final_url,
        published_at="",
        body="",
        assets=assets,
    )
