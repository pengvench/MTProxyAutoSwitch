# MTProxy AutoSwitch Public

Public-safe source tree for publishing to git.

This tree does not contain embedded Telegram `api_id` / `api_hash`. If the user wants Telegram-only features, they must enter their own API credentials inside the app. The app includes a button that opens:

```text
https://my.telegram.org/apps
```

## What is included

- Public source files only
- Windows public build script: `build_release_public.bat`
- macOS public build script: `build_release_public_macos.sh`
- Windows PyInstaller spec: `MTProxyAutoSwitchPublic.spec`
- macOS PyInstaller spec: `MTProxyAutoSwitchPublic.macos.spec`
- Default startup list: `list/proxy_list.txt`
- Public-safe `config.json`

## Telegram features that require user API credentials

- Parsing Telegram sources through Telegram API
- QR login
- Deep media check
- Sending proxy lists to Saved Messages

## Windows build

Requirements:

- Windows 10/11
- Python 3.11+

Build:

```bat
build_release_public.bat
```

Output:

```text
release-public\MTProxyAutoSwitchPublic
```

## macOS build

Requirements:

- macOS 12+ recommended
- Python 3.11 or 3.12
- Xcode Command Line Tools

Quick start:

```bash
xcode-select --install
chmod +x build_release_public_macos.sh
./build_release_public_macos.sh
```

Output:

```text
release-macos/MTProxyAutoSwitchPublic/MTProxyAutoSwitchPublic.app
```

Detailed macOS notes are in `MACOS_BUILD.md`.

## Notes

- This folder is intended for public publishing.
- Telegram API keys are intentionally not embedded here.
- `config.json` starts with empty Telegram API credentials.
- Build the macOS `.app` on macOS, not on Windows.
