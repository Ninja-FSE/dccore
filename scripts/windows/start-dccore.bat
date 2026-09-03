@echo off
setlocal

rem ---------------------------------------------------------------------
rem  DCCore launcher for Windows.
rem
rem  The important line is the cd below. Every data path in config.py is
rem  relative - ./data/bans.txt, ./lists - so they resolve against the
rem  working directory. Double-clicking this file from anywhere, or
rem  running it from a shortcut, would otherwise start the daemon with a
rem  working directory that is not the repository, and it would quietly
rem  create an empty data folder somewhere else and boot with no bans,
rem  no queue and no list.
rem
rem  Usage:
rem    scripts\windows\start-dccore.bat          check the setup, then start the daemon
rem    scripts\windows\start-dccore.bat check    check the setup and stop
rem ---------------------------------------------------------------------

cd /d "%~dp0..\.."

rem --- find an interpreter ----------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"

if not defined PY (
    echo.
    echo   Python was not found.
    echo.
    echo   Install Python 3.10 or newer from python.org and tick
    echo   "Add python.exe to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
)

rem --- check-only mode ---------------------------------------------------
if /i "%~1"=="check" (
    %PY% scripts\windows\check-setup.py
    echo.
    pause
    exit /b %errorlevel%
)

rem --- refuse to start without a local config ---------------------------
rem  settings.conf is fully first-class (see scripts/setup_check.py's own
rem  note) - the daemon starts fine from it alone, so this only refuses
rem  when NEITHER override exists.
rem  An upgrading install has neither, but is NOT unconfigured: #170 renamed
rem  local_config.py to admin_config.py, and that file is gitignored, so the
rem  pull renamed defaults.py for them and could not touch theirs. The daemon
rem  renames it at import time - but this check runs first, so without this
rem  branch the operator is told to copy the sample, and doing so is exactly
rem  the condition that makes the migration skip for good.
if not exist "admin_config.py" if not exist "settings.conf" if exist "local_config.py" (
    echo.
    echo   Found local_config.py, which #170 renamed to admin_config.py.
    echo.
    echo   Nothing to copy - start the daemon once and it renames the file
    echo   for you, keeping every setting in it:
    echo.
    echo       python oserve.py
    echo.
    echo   Do NOT copy admin_config.py.sample over the top: that leaves your
    echo   real settings stranded in local_config.py.
    echo.
    pause
    exit /b 1
)

if not exist "admin_config.py" if not exist "settings.conf" (
    echo.
    echo   No admin_config.py and no settings.conf found.
    echo.
    echo   Copy admin_config.py.sample to admin_config.py, or
    echo   settings.conf.sample to settings.conf, and fill one in.
    echo   Without either the daemon uses the defaults in defaults.py, which
    echo   point at somebody else's live bot and channels.
    echo.
    pause
    exit /b 1
)

rem --- refuse to start on a broken or dangerous config -------------------
rem  check-setup.py fails on a missing music directory, and on a config
rem  still pointing at the production bot's nick or channels. That second
rem  one is worth blocking: it would put a near-identical second bot into
rem  live trading channels, which can get the other operator banned too.
%PY% scripts\windows\check-setup.py >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Setup check failed - not starting. Details:
    echo.
    %PY% scripts\windows\check-setup.py
    echo.
    pause
    exit /b 1
)

rem --- go ----------------------------------------------------------------
echo.
echo   Starting DCCore.  Press Ctrl-C in this window to stop it.
echo.
%PY% oserve.py
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo   DCCore exited normally.
) else (
    echo   DCCore exited with code %RC%.
)
echo.
pause
exit /b %RC%
