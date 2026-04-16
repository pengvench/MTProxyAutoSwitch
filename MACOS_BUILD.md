# macOS Build Guide

## Requirements

- macOS 12 or newer recommended
- Python 3.11 or 3.12
- Xcode Command Line Tools

Install Xcode tools:

```bash
xcode-select --install
```

## Build steps

Open Terminal in the project folder and run:

```bash
chmod +x build_release_macos.sh
./build_release_macos.sh
```

The build script installs these dependencies automatically:

```text
pyinstaller
customtkinter
darkdetect
pystray
qrcode
TelethonFakeTLS
telethon
certifi
cryptography
pillow
imageio
imageio-ffmpeg
pyobjc-core
pyobjc-framework-Cocoa
pyobjc-framework-Quartz
```

## Output

The application bundle will be created at:

```text
release-macos/MTProxyAutoSwitch/MTProxyAutoSwitch.app
```

The script also copies:

- `README.txt`
- `config.json`
- `mtproxy_seed.json`
- `list/proxy_list.txt`

On macOS the build now targets the current CPU architecture by default:

- Apple Silicon -> `arm64`
- Intel Mac -> `x86_64`

If you really need a different target, override it explicitly:

```bash
MTPROXY_TARGET_ARCH=universal2 ./build_release_macos.sh
```

When the app runs from a `.app` bundle, it first looks for `config.json`, `.env` and `list/` next to the bundle in `release-macos/MTProxyAutoSwitch/`.
The app stores mutable state in `~/Library/Application Support/MTProxyAutoSwitch/`.

## Gatekeeper

If macOS blocks the app on first launch, remove quarantine attributes:

```bash
xattr -dr com.apple.quarantine release-macos/MTProxyAutoSwitch/MTProxyAutoSwitch.app
```

## Telegram API credentials

The release build does not include embedded Telegram API credentials.

If the user wants Telegram API features, they need their own:

```text
https://my.telegram.org/apps
```
