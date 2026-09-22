@echo off
setlocal

rem ---------------------------------------------------------------------
rem  The twin of install-autostart.bat: deletes the "DCCore" Task Scheduler
rem  entry, so the bot no longer starts at logon. A bot already running is
rem  not touched - close its window to stop it.
rem ---------------------------------------------------------------------

echo.
call schtasks /delete /tn "DCCore" /f
if errorlevel 1 (
    echo.
    echo   There was no "DCCore" entry to remove - nothing changed.
    echo.
    pause
    exit /b 0
)
echo.
echo   Done: DCCore no longer starts at logon.
echo.
pause
exit /b 0
