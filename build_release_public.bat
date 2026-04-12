@echo off
setlocal

cd /d "%~dp0"

python -m pip install --upgrade pyinstaller customtkinter darkdetect pystray qrcode TelethonFakeTLS cryptography pillow imageio imageio-ffmpeg
python -m pip install telethon==1.42.0

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release-public rmdir /s /q release-public

pyinstaller --noconfirm --clean MTProxyAutoSwitchPublic.spec
if errorlevel 1 exit /b 1

mkdir release-public\MTProxyAutoSwitchPublic
xcopy /E /I /Y dist\MTProxyAutoSwitchPublic release-public\MTProxyAutoSwitchPublic >nul
copy /Y README.md release-public\MTProxyAutoSwitchPublic\README.txt >nul
copy /Y config.json release-public\MTProxyAutoSwitchPublic\config.json >nul
if not exist release-public\MTProxyAutoSwitchPublic\list mkdir release-public\MTProxyAutoSwitchPublic\list
if exist list\proxy_list.txt copy /Y list\proxy_list.txt release-public\MTProxyAutoSwitchPublic\list\proxy_list.txt >nul
if exist release-public\MTProxyAutoSwitchPublic.zip del /f /q release-public\MTProxyAutoSwitchPublic.zip
powershell -NoProfile -Command "Compress-Archive -Path 'release-public\\MTProxyAutoSwitchPublic\\*' -DestinationPath 'release-public\\MTProxyAutoSwitchPublic.zip' -Force"

attrib +h release-public\MTProxyAutoSwitchPublic\_internal >nul 2>nul

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

echo Build complete: release-public\MTProxyAutoSwitchPublic
endlocal
