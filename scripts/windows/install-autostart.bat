@echo off
setlocal

rem ---------------------------------------------------------------------
rem  Start DCCore when you log on (#547, Proposal 6).
rem
rem  Creates a Task Scheduler entry, "DCCore", that runs start-dccore.bat
rem  at logon - the launcher, not oserve.py directly, because the launcher
rem  is what puts the working directory right (every data path is
rem  relative; see docs/WINDOWS.md, "Why there is a launcher at all").
rem  The bot's window opens as usual, so closing it still stops the bot.
rem  remove-autostart.bat deletes the entry again.
rem
rem  For the logged-on user only, no administrator needed, no password
rem  stored: an "on logon" task without /ru runs in your own session when
rem  you are the one logging on. It is not a service - a service would
rem  start before anyone logs on and needs the path anchoring the docs
rem  describe - and it does not try to be.
rem ---------------------------------------------------------------------

cd /d "%~dp0..\.."

rem  Autostart on a tree that has never been set up would ask the setup
rem  questions at every logon. Once by hand first.
if not exist "admin_config.py" if not exist "settings.conf" (
    echo.
    echo   DCCore is not set up yet. Run start-dccore.bat once first - it asks
    echo   the setup questions - then this file.
    echo.
    pause
    exit /b 1
)

rem  /f replaces an existing entry of the same name, so running this twice
rem  is fine. `call`, so a wrapper on PATH (the tests use one) returns here.
call schtasks /create /tn "DCCore" /sc onlogon /tr "\"%~dp0start-dccore.bat\" autostart" /f
if errorlevel 1 (
    echo.
    echo   Task Scheduler refused. The Task Scheduler service may be off, or
    echo   this account may not be allowed to create tasks.
    echo.
    pause
    exit /b 1
)

rem  schtasks creates the task with Task Scheduler's stock settings, which are
rem  for a maintenance job (#587): stop it after 72 hours, do not start it on
rem  battery, stop it when unplugged, run it below normal priority. For a bot
rem  that is meant to stay up that means it silently vanishes from IRC after
rem  three days, and never starts on a laptop that is unplugged. So they are
rem  replaced: no time limit, battery is fine, normal priority, and a restart
rem  (up to three times, a minute apart) if it fails. Not fatal if it cannot be
rem  done - the task exists and starts at logon either way.
call powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); Set-ScheduledTask -TaskName 'DCCore' -Settings $s | Out-Null"
if errorlevel 1 (
    echo.
    echo   The task was created, but Task Scheduler's defaults could not be
    echo   changed: it may be stopped after 3 days, and will not run on battery.
    echo   Open Task Scheduler, the DCCore task, Settings, and untick "Stop the
    echo   task if it runs longer than", and the two battery options.
)

echo.
echo   Done: DCCore starts the next time you log on, in its own window.
echo   Start it by hand now with start-dccore.bat if you want it running
echo   already - only one copy runs from this folder, so a second start
echo   is refused, not doubled. remove-autostart.bat undoes this.
echo.
pause
exit /b 0
