@echo off
setlocal

rem ---------------------------------------------------------------------
rem  DCCore launcher for Windows.
rem
rem  The important line is the cd below. Every data path in defaults.py is
rem  relative - ./data/bans.txt, ./lists - so they resolve against the
rem  working directory. Double-clicking this file from anywhere, or
rem  running it from a shortcut, would otherwise start the daemon with a
rem  working directory that is not the repository, and it would quietly
rem  create an empty data folder somewhere else and boot with no bans,
rem  no queue and no list.
rem
rem  Usage:
rem    scripts\windows\start-dccore.bat          first run: ask the setup questions;
rem                                              then check the setup and start the daemon
rem    scripts\windows\start-dccore.bat check    check the setup and stop
rem
rem  THE LAUNCHER IS THE INSTALL (#547, Proposal 1). A first-timer's whole
rem  install is: extract, double-click this file. With no config yet it
rem  runs configure.py right here rather than telling them to copy a
rem  sample; before every start it offers to install Flask if the
rem  dashboard is on and Flask is missing; and it finds Python where the
rem  python.org installer puts it even when the "Add to PATH" box was
rem  missed. Nothing an already-configured install does changes.
rem
rem  PYTHON MISSING: HELP, DO NOT FAIL (#547, Proposal 2). When there is no
rem  Python at all, the one wall left, this file offers to fetch python.org's
rem  own installer, checks it against the SHA-256 pinned below, and runs it
rem  unattended with both boxes ticked - per user, so no administrator
rem  prompt. It asks first, and a hash that does not match refuses to run
rem  the file, loudly. Moving to a newer Python is three lines here: the
rem  version and the two hashes, copied from the release page on python.org.
rem ---------------------------------------------------------------------

cd /d "%~dp0..\.."

rem --- the Python this file installs when there is none ------------------
rem  The release page (python.org/downloads/release/python-<ver>/) prints
rem  each installer's SHA-256 in four groups of sixteen; these are those,
rem  joined. Verified against the downloaded files when they were pinned.
set "PY_VERSION=3.14.7"
set "PY_SHA256_AMD64=9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649"
set "PY_SHA256_ARM64=9a3fe120cc81bc2cb099550f794d8356811f96a86c7f438519243c3485db928d"
set "PY_DOWNLOAD_PAGE=https://www.python.org/downloads/windows/"
set "PY_INSTALL_TRIED="

rem --- find an interpreter ----------------------------------------------
:find_python
rem  Found is not the same as works (#586). On a stock Windows 10/11 with no
rem  Python, `where python` finds the Microsoft Store's stub in WindowsApps -
rem  which is on PATH by default, opens the Store and exits 9009 - and `py`
rem  can exist with no Python behind it. So each candidate is RUN once: only
rem  one that answers becomes %PY%, and a machine that has only a stub falls
rem  through to the install offer below. `call`, because a shim (pyenv-win's
rem  python.bat) is a batch file, and running one without it never comes back.
set "PY="
where py >nul 2>&1 && call py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && call python -c "import sys" >nul 2>&1 && set "PY=python"

rem  Neither on PATH. The python.org installer puts a per-user install under
rem  %LOCALAPPDATA%\Programs\Python and an all-users one under %ProgramFiles%,
rem  and a missed "Add python.exe to PATH" box is the commonest reason `where`
rem  finds nothing - the interpreter is there, it just was not announced. The
rem  value keeps its own quotes because the path has spaces in it on most
rem  machines ("Program Files") and %PY% is used bare everywhere below.
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~D\python.exe" set "PY="%%~D\python.exe""
)
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%~D\python.exe" set "PY="%%~D\python.exe""
)

if defined PY goto :have_python
if defined PY_INSTALL_TRIED goto :python_by_hand
goto :offer_python

rem --- no Python: offer to install it ------------------------------------
:offer_python
echo.
echo   Python was not found.
echo.
echo   DCCore can download Python %PY_VERSION% from python.org and install it
echo   for you: about 32 MB, for your user only (no administrator prompt),
echo   with "Add python.exe to PATH" and "py launcher" both ticked. The
echo   download is checked against a fingerprint before it is run.
echo.
choice /c YN /n /m "  Download and install Python now? [Y/N] "
if errorlevel 2 goto :python_by_hand
set "PY_INSTALL_TRIED=1"

rem  Which installer. python.org ships one per processor; a 32-bit Windows
rem  gets the download page instead, since a 32-bit build is not pinned.
set "PY_ARCH="
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" (set "PY_ARCH=amd64" & set "PY_SHA256=%PY_SHA256_AMD64%")
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" (set "PY_ARCH=amd64" & set "PY_SHA256=%PY_SHA256_AMD64%")
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" (set "PY_ARCH=arm64" & set "PY_SHA256=%PY_SHA256_ARM64%")
if not defined PY_ARCH (
    echo.
    echo   This Windows is 32-bit, and python.org's 32-bit installer is not
    echo   pinned here. Opening the download page instead.
    goto :python_by_hand
)
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-%PY_ARCH%.exe"
set "PY_INSTALLER=%TEMP%\python-%PY_VERSION%-%PY_ARCH%.exe"

rem  curl has shipped with Windows since 10 1803; a machine without it is
rem  old enough that the page is the safer route anyway.
where curl >nul 2>&1
if errorlevel 1 (
    echo.
    echo   curl was not found, so the download cannot be made from here.
    goto :python_by_hand
)
echo.
echo   Downloading %PY_URL%
rem  `call`, so a curl that is a wrapper script returns here instead of
rem  taking over; a plain curl.exe is unaffected.
call curl -L --fail --progress-bar -o "%PY_INSTALLER%" "%PY_URL%"
if errorlevel 1 (
    echo.
    echo   The download did not complete. python.org may have retired this
    echo   build, or the network is not reachable right now.
    goto :python_by_hand
)

rem  The fingerprint. certutil prints a heading, the hash, then a footer;
rem  older Windows put spaces between the bytes, so those are removed
rem  before comparing. A mismatch means the file is not the one python.org
rem  published for this version, whatever the reason, and it is not run.
set "PY_HASH="
for /f "usebackq skip=1 delims=" %%H in (`certutil -hashfile "%PY_INSTALLER%" SHA256`) do if not defined PY_HASH set "PY_HASH=%%H"
set "PY_HASH=%PY_HASH: =%"
if /i not "%PY_HASH%"=="%PY_SHA256%" (
    echo.
    echo   The downloaded file does not match the fingerprint pinned in this
    echo   launcher, so it will NOT be run.
    echo       expected %PY_SHA256%
    echo       got      %PY_HASH%
    echo   Either python.org replaced the build or the download was altered
    echo   in transit. Install Python from the page instead.
    del /q "%PY_INSTALLER%" >nul 2>&1
    goto :python_by_hand
)

rem  python.org's own documented unattended options: a progress bar and no
rem  questions, per user, PATH and the py launcher on, the test suite off.
echo   Fingerprint matches. Installing Python %PY_VERSION% ...
start /wait "" "%PY_INSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
set "PY_INSTALL_RC=%errorlevel%"
del /q "%PY_INSTALLER%" >nul 2>&1
if "%PY_INSTALL_RC%"=="0" goto :installed_python
if "%PY_INSTALL_RC%"=="3010" goto :installed_python
echo.
echo   The Python installer exited with code %PY_INSTALL_RC% - it was cancelled
echo   or did not finish.
goto :python_by_hand

:installed_python
echo   Python %PY_VERSION% is installed.
echo.
rem  This window's PATH predates the install, so the search below finds it
rem  where the installer put it (%LOCALAPPDATA%\Programs\Python) rather
rem  than on PATH. The next window opened will have it on PATH.
goto :find_python

rem --- no Python and not installing it here ------------------------------
:python_by_hand
echo.
echo   Install Python 3.10 or newer from python.org and tick BOTH boxes
echo   in its installer - "Add python.exe to PATH" and "py launcher" -
echo   then run this file again.
echo.
echo       %PY_DOWNLOAD_PAGE%
echo.
rem  Opened in the browser as well as printed, unless a test says not to.
if not defined DCCORE_NO_BROWSER start "" "%PY_DOWNLOAD_PAGE%"
pause
exit /b 1

:have_python

rem --- check-only mode ---------------------------------------------------
rem  NOT written as `if ... ( ... exit /b %errorlevel% )`. cmd.exe expands
rem  %errorlevel% when it PARSES the parenthesised block, before anything
rem  inside it has run, so the value used is whatever it was beforehand -
rem  0 - whatever check-setup.py actually returned. Verified on Windows 11:
rem  the block form exits 0 where this form exits the real code.
rem
rem  It matters because `start-dccore.bat check` is the documented Windows
rem  pre-flight in README.md, docs/INSTALL.md and docs/WINDOWS.md. It would
rem  print "FAIL ..." and "1 problem(s) - fix these before starting", then
rem  exit 0 - so a wrapper, a scheduled task or a CI step gating on the
rem  exit code treated a broken config as verified.
rem
rem  The Linux twin was always right (`"$PY" ... ; exit $?`, outside any
rem  block), so the two launchers had drifted on the one thing this shim
rem  layer exists to keep identical.
if /i "%~1"=="check" goto :run_check
rem  Under the logon task (#710) there is nobody at the keyboard: the Flask
rem  offer below prints its command instead of asking, and a stop at the end
rem  does not wait for a key. install-autostart.bat passes this.
if /i "%~1"=="autostart" set "DCCORE_AUTOSTART=1"
goto :after_check

:run_check
%PY% scripts\windows\check-setup.py
set "CHECK_RC=%errorlevel%"
echo.
pause
exit /b %CHECK_RC%

:after_check

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
    echo       py oserve.py
    echo.
    echo   Do NOT copy admin_config.py.sample over the top: that leaves your
    echo   real settings stranded in local_config.py.
    echo.
    pause
    exit /b 1
)

rem --- first run: ask the questions here ---------------------------------
rem  No config at all means a first run, not a mistake. configure.py asks
rem  everything in the right order - nick, server, channels, admin nick,
rem  music folder, dashboard, password - and writes settings.conf and
rem  admin_config.py; it used to be a separate terminal step this file then
rem  told people to go and do. A tree without configure.py (a broken
rem  extract) still gets the old instruction, so nothing is worse than before.
if not exist "admin_config.py" if not exist "settings.conf" if exist "configure.py" goto :first_run
if not exist "admin_config.py" if not exist "settings.conf" (
    echo.
    echo   No admin_config.py and no settings.conf found, and no configure.py
    echo   to create them with - this does not look like a complete DCCore
    echo   folder. Extract the download again, then run this file.
    echo.
    pause
    exit /b 1
)
goto :configured

:first_run
echo.
echo   Welcome to DCCore. This looks like the first run - a few questions
echo   and it will be set up. You can change every answer later on the
echo   dashboard's Settings page.
echo.
rem  SET IT UP IN THE BROWSER (#547, Proposal 4). With Flask - installed
rem  here on a yes if it is missing - the daemon is started straight away
rem  and serves its own setup page on 127.0.0.1; it carries on into the
rem  real bot once the form is saved. The setup check is skipped on that
rem  path, since it would refuse the blank tree the page exists to fill in;
rem  the daemon runs its own checks after the form. A no, no pip, or nobody
rem  at the keyboard means the questions are asked here, as before.
%PY% configure.py --setup-in-browser
if not errorlevel 1 (
    set "BROWSER_SETUP=1"
    goto :go
)
:ask_here
%PY% configure.py
if errorlevel 1 (
    echo.
    echo   Setup did not finish, so DCCore was not started. Run this file
    echo   again to pick it up where it stopped.
    echo.
    pause
    exit /b 1
)
echo.

:configured

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

rem --- the dashboard's one dependency, offered before it is missed ---------
rem  Silent when the dashboard is off or Flask is already there; otherwise
rem  the same offer configure.py makes during setup. Never stops the start.
%PY% configure.py --flask

rem --- go ----------------------------------------------------------------
:go
echo.
echo   Starting DCCore.  Press Ctrl-C in this window to stop it.
echo   Closing this window stops the bot too - leave it open, or minimise it.
echo.
%PY% oserve.py
set "RC=%errorlevel%"
rem  4 is "another DCCore holds this folder" (#710): the logon task and a
rem  double-click, or two logon sessions, both ran the bot. Not a failure of
rem  this copy - the other is fine - so said as what it is.
if "%RC%"=="4" (
    echo.
    echo   DCCore is already running from this folder - in another window, or
    echo   started by the logon task. This copy did nothing. Stop that one first
    echo   if you meant to restart it.
    echo.
)

rem  THE PAGE COULD NOT BE SERVED (#617). oserve.py exits 3 when the setup
rem  page was this first run's path and could not finish - the port was
rem  taken (another DCCore in a minimised window, another program), or
rem  Ctrl-C. Flask being there sent every run down the same road, and the
rem  questions above were unreachable from here: ask them now, then check
rem  the setup and start, as the terminal path does. Once they are answered
rem  no later run comes this way.
if "%BROWSER_SETUP%"=="1" if "%RC%"=="3" (
    set "BROWSER_SETUP="
    echo.
    echo   The setup page could not be opened, so the questions follow here.
    echo.
    goto :ask_here
)

echo.
if "%RC%"=="0" (
    echo   DCCore exited normally.
) else (
    echo   DCCore exited with code %RC%.
)
echo.
pause
exit /b %RC%
