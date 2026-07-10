@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

"%PYTHON_EXE%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 exit /b 1

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release-public rmdir /s /q release-public

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean MTProxyAutoSwitch.spec
if errorlevel 1 exit /b 1

mkdir release-public\portable\MTProxyAutoSwitch
xcopy /E /I /Y dist\MTProxyAutoSwitch release-public\portable\MTProxyAutoSwitch >nul
copy /Y README.md release-public\portable\MTProxyAutoSwitch\README.txt >nul
copy /Y config.template.json release-public\portable\MTProxyAutoSwitch\config.template.json >nul
if exist img\icon.ico copy /Y img\icon.ico release-public\portable\MTProxyAutoSwitch\icon.ico >nul

if exist release-public\MTProxyAutoSwitch.zip del /f /q release-public\MTProxyAutoSwitch.zip
where tar >nul 2>nul
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Compress-Archive -Path 'release-public\\portable\\MTProxyAutoSwitch\\*' -DestinationPath 'release-public\\MTProxyAutoSwitch.zip' -Force"
) else (
    tar -a -c -f release-public\MTProxyAutoSwitch.zip -C release-public\portable\MTProxyAutoSwitch .
)
if errorlevel 1 exit /b 1
if not exist release-public\MTProxyAutoSwitch.zip exit /b 1

set "ISCC_EXE="
for %%I in (iscc.exe) do set "ISCC_EXE=%%~$PATH:I"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
    echo Inno Setup not found. Skipping installer build and keeping portable artifacts only.
    goto :portable_only
)

"%ISCC_EXE%" /Qp MTProxyAutoSwitch.iss
if errorlevel 1 exit /b 1

attrib +h release-public\portable\MTProxyAutoSwitch\_internal >nul 2>nul

:portable_only

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

echo Build complete:
echo   release-public\MTProxyAutoSwitch.zip
if exist release-public\MTProxyAutoSwitch-Setup.exe echo   release-public\MTProxyAutoSwitch-Setup.exe
endlocal
