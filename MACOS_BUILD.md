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

The build produces:

```text
release-macos/MTProxyAutoSwitch.app
release-macos/MTProxyAutoSwitch.pkg
```

Use `MTProxyAutoSwitch.pkg` as the normal installer.
It installs the app to `/Applications`, so the app shows up in Applications and Launchpad.

On macOS the build now targets the current CPU architecture by default:

- Apple Silicon -> `arm64`
- Intel Mac -> `x86_64`

If you really need a different target, override it explicitly:

```bash
MTPROXY_TARGET_ARCH=universal2 ./build_release_macos.sh
```

The app stores mutable state in `~/Library/Application Support/MTProxyAutoSwitch/`.
Bundled seed data is embedded into the app bundle, so installed builds still have a startup proxy pool.

## Gatekeeper

If macOS blocks the app on first launch, remove quarantine attributes:

```bash
xattr -dr com.apple.quarantine release-macos/MTProxyAutoSwitch.app
```

## Telegram API credentials

The release build does not include embedded Telegram API credentials.

If the user wants Telegram API features, they need their own:

```text
https://my.telegram.org/apps
```
