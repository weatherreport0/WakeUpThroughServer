@echo off
:: Run as Administrator!
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting admin rights...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo === WakeBot SSH Setup ===
echo.

echo [1/3] Installing OpenSSH Server...
powershell -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0" 2>nul
echo Done.

echo [2/3] Starting SSH service...
powershell -Command "Start-Service sshd; Set-Service -Name sshd -StartupType Automatic"
echo Done.

echo [3/3] Adding server SSH key...
set KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPua9Q5ZvTCqnzsKgJbRPV8jO0YbnmOH/SfqUzM/AVP6 egor@egornetbookserver
set KEYFILE=%ProgramData%\ssh\administrators_authorized_keys

:: Create file if not exists
if not exist "%KEYFILE%" type nul > "%KEYFILE%"

:: Check if key already added
findstr /C:"egornetbookserver" "%KEYFILE%" >nul 2>&1
if %errorlevel% equ 0 (
    echo Key already exists, skipping.
) else (
    echo %KEY% >> "%KEYFILE%"
    echo Key added.
)

:: Fix permissions (required by OpenSSH on Windows)
icacls "%KEYFILE%" /inheritance:r /grant "SYSTEM:(F)" /grant "Administrators:(F)" >nul

echo.
echo === Setup complete! SSH is ready. ===
pause
