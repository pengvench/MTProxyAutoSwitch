@echo off
setlocal

cd /d "%~dp0"

python -m pip install --upgrade ^
    pyinstaller ^
    customtkinter ^
    darkdetect ^
    pystray ^
    qrcode ^
    TelethonFakeTLS ^
    cryptography ^
    pillow ^
    imageio ^
    imageio-ffmpeg ^
    pywin32

python -m pip install telethon==1.42.0

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release-public rmdir /s /q release-public

pyinstaller --noconfirm --clean MTProxyAutoSwitch.spec
if errorlevel 1 exit /b 1

mkdir release-public\MTProxyAutoSwitch
xcopy /E /I /Y dist\MTProxyAutoSwitch release-public\MTProxyAutoSwitch >nul
copy /Y README.mtproxy.md release-public\MTProxyAutoSwitch\README.txt >nul
if exist list mkdir release-public\MTProxyAutoSwitch\list
if exist list\proxy_list.txt copy /Y list\proxy_list.txt release-public\MTProxyAutoSwitch\list\proxy_list.txt >nul
if exist list\report.json copy /Y list\report.json release-public\MTProxyAutoSwitch\list\report.json >nul
if exist img\icon.ico copy /Y img\icon.ico release-public\MTProxyAutoSwitch\icon.ico >nul
copy /Y config.template.json release-public\MTProxyAutoSwitch\config.json >nul
if exist release-public\MTProxyAutoSwitch.zip del /f /q release-public\MTProxyAutoSwitch.zip
powershell -NoProfile -Command "Compress-Archive -Path 'release-public\\MTProxyAutoSwitch\\*' -DestinationPath 'release-public\\MTProxyAutoSwitch.zip' -Force"

attrib +h release-public\MTProxyAutoSwitch\_internal >nul 2>nul

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

echo Build complete: release-public\MTProxyAutoSwitch
endlocal
