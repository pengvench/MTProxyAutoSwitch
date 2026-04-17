@echo off
setlocal

cd /d "%~dp0"

python -m pip install --upgrade ^
    pyinstaller ^
    customtkinter ^
    darkdetect ^
    pystray ^
    qrcode ^
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

mkdir release-public\portable\MTProxyAutoSwitch
xcopy /E /I /Y dist\MTProxyAutoSwitch release-public\portable\MTProxyAutoSwitch >nul
copy /Y README.md release-public\portable\MTProxyAutoSwitch\README.txt >nul
copy /Y config.template.json release-public\portable\MTProxyAutoSwitch\config.json >nul
if exist list mkdir release-public\portable\MTProxyAutoSwitch\list
if exist list\proxy_list.txt copy /Y list\proxy_list.txt release-public\portable\MTProxyAutoSwitch\list\proxy_list.txt >nul
if exist list\report.json copy /Y list\report.json release-public\portable\MTProxyAutoSwitch\list\report.json >nul
if exist img\icon.ico copy /Y img\icon.ico release-public\portable\MTProxyAutoSwitch\icon.ico >nul

if exist release-public\MTProxyAutoSwitch.zip del /f /q release-public\MTProxyAutoSwitch.zip
powershell -NoProfile -Command "Compress-Archive -Path 'release-public\\portable\\MTProxyAutoSwitch\\*' -DestinationPath 'release-public\\MTProxyAutoSwitch.zip' -Force"

set "ISCC_EXE="
for %%I in (iscc.exe) do set "ISCC_EXE=%%~$PATH:I"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
    echo Inno Setup 6 not found. Install ISCC.exe and rerun build_release.bat.
    exit /b 1
)

"%ISCC_EXE%" /Qp MTProxyAutoSwitch.iss
if errorlevel 1 exit /b 1

attrib +h release-public\portable\MTProxyAutoSwitch\_internal >nul 2>nul

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

echo Build complete:
echo   release-public\MTProxyAutoSwitch-Setup.exe
echo   release-public\MTProxyAutoSwitch.zip
endlocal
