@echo off
setlocal

rem ---------------------------------------------------------------------
rem  Let DCCore through Windows Defender Firewall (#547, Proposal 6).
rem
rem  The first time the bot listens for a DCC send, Windows asks whether
rem  to allow it. "Cancel" there means every send from then on times out
rem  with no hint why. This file adds the rule that dialog would have
rem  added - inbound TCP on the DCC port range from settings.conf, and the
rem  dashboard's port if the dashboard is on - and can be run any time.
rem  remove-firewall.bat takes both rules out again.
rem
rem  Adding a firewall rule needs an administrator's yes, so this file
rem  re-opens itself elevated (the usual Windows prompt) when it was not.
rem  Nothing else it does needs that - so nothing else is done there
rem  (#684). The elevated copy runs as whichever account answered UAC, and
rem  when that is a different account from the operator's (a standard user
rem  with a parent's admin password), its %LOCALAPPDATA% and `py -3` are
rem  that account's: the per-user Python start-dccore.bat installed is not
rem  there, and this file used to stop with "Python was not found" on a
rem  machine where the launcher works. So the interpreter is found and the
rem  ports are read HERE, unelevated, as the operator, and handed to the
rem  elevated copy as arguments; it only runs netsh.
rem
rem  The ports are read by scripts\ports.py from the same settings the
rem  daemon uses, never typed here, so a changed DCC_PORT_START is honoured.
rem ---------------------------------------------------------------------

cd /d "%~dp0..\.."

rem --- the elevated copy: everything was worked out for it -----------------
rem  Arguments, not a re-search: `elevated <dcc start> <dcc end> <web port>
rem  <web on> "<python.exe>"`, in that order, from the relaunch below.
if /i "%~1"=="elevated" (
    set "DCC_START=%~2"
    set "DCC_END=%~3"
    set "WEB_PORT=%~4"
    set "WEB_ON=%~5"
    set "PYEXE=%~6"
    goto :block_rule
)

rem --- administrator? ----------------------------------------------------
rem  `net session` only succeeds elevated. `call`, so a wrapper on PATH (the
rem  tests use one) returns here instead of taking over. Remembered, acted on
rem  once the ports are known.
set "ELEVATE="
call net session >nul 2>&1
if errorlevel 1 set "ELEVATE=1"

rem --- find an interpreter, as start-dccore.bat does ---------------------
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
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~D\python.exe" set "PY="%%~D\python.exe""
)
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%~D\python.exe" set "PY="%%~D\python.exe""
)
if not defined PY (
    echo.
    echo   Python was not found - run start-dccore.bat first, it installs it.
    echo.
    pause
    exit /b 1
)

rem --- the ports, from the settings ---------------------------------------
rem  Through a file rather than for /f's own command form: %PY% may carry
rem  quotes, and cmd /c's quote stripping makes that form unreliable.
set "PORTS_FILE=%TEMP%\dccore-ports.txt"
%PY% scripts\ports.py > "%PORTS_FILE%"
if errorlevel 1 (
    echo.
    echo   Could not read the ports from the settings. Run
    echo   start-dccore.bat check to see what is wrong.
    echo.
    pause
    exit /b 1
)
set "DCC_START=" & set "DCC_END=" & set "WEB_PORT=" & set "WEB_ON="
for /f "usebackq tokens=1-4" %%A in ("%PORTS_FILE%") do (
    set "DCC_START=%%A"
    set "DCC_END=%%B"
    set "WEB_PORT=%%C"
    set "WEB_ON=%%D"
)
del /q "%PORTS_FILE%" >nul 2>&1

rem --- which python.exe the bot runs on --------------------------------------
rem  sys.executable of %PY% - through a file, because %PY% may carry quotes
rem  and cmd's quote handling makes the for /f form unreliable. Needed by the
rem  Block-rule step below, and worked out here so the elevated copy (a
rem  different account, maybe) does not have to find Python at all.
set "PYEXE="
set "PYEXE_FILE=%TEMP%\dccore-pyexe.txt"
del /q "%PYEXE_FILE%" >nul 2>&1
%PY% -c "import sys; open(sys.argv[1], 'w').write(sys.executable)" "%PYEXE_FILE%" >nul 2>&1
if exist "%PYEXE_FILE%" for /f "usebackq delims=" %%P in ("%PYEXE_FILE%") do set "PYEXE=%%P"
del /q "%PYEXE_FILE%" >nul 2>&1

rem --- not an administrator: hand everything to an elevated copy -------------
rem  Through the environment, not the command line: a folder with an
rem  apostrophe in its name ("C:\Users\O'Brien\...") ended the PowerShell
rem  string early and the relaunch never happened. $env: reads both whole.
if not defined ELEVATE goto :block_rule
echo.
echo   Adding a firewall rule needs an administrator's yes - Windows will
echo   ask now. The rule is only ever for DCCore's own ports.
echo.
set "DCCORE_SELF=%~f0"
set "DCCORE_ARGS=elevated %DCC_START% %DCC_END% %WEB_PORT% %WEB_ON% "%PYEXE%""
call powershell -NoProfile -Command "Start-Process -FilePath $env:DCCORE_SELF -ArgumentList $env:DCCORE_ARGS -Verb RunAs"
exit /b

:block_rule
rem --- a Block rule that "Cancel" left behind (#589) -----------------------
rem  The dialog's Cancel does not just decline: it creates an inbound BLOCK
rem  rule for this interpreter, and Windows Defender Firewall evaluates Block
rem  rules before Allow rules - so the port rule below cannot help while it
rem  exists, and the file's own promise (fix "Cancel") would be false. Any
rem  inbound Block rule for THIS python.exe is removed first; nothing else is
rem  touched, and an Allow rule the dialog made stays. Not fatal if PowerShell
rem  cannot do it. No parenthesised block: a ")" in a PowerShell command
rem  would end it. The interpreter's path reaches PowerShell through the
rem  environment (#684): it may hold an apostrophe, and the elevated copy
rem  was handed it rather than finding it.
if not defined PYEXE goto :rules
set "DCCORE_PYEXE=%PYEXE%"
call powershell -NoProfile -Command "$exe = $env:DCCORE_PYEXE; $n = 0; Get-NetFirewallApplicationFilter | Where-Object { $_.Program -eq $exe } | ForEach-Object { $r = $_ | Get-NetFirewallRule; if ($r.Direction -eq 'Inbound' -and $r.Action -eq 'Block') { $r | Remove-NetFirewallRule; $n++ } }; if ($n -gt 0) { Write-Host ('  Removed ' + $n + ' inbound Block rule(s) for ' + $exe + ' - the one Cancel made.') }"
if errorlevel 1 echo   Could not look for a Block rule left by "Cancel"; if sends still time out, remove it in Windows Defender Firewall.

:rules
rem --- the rules ---------------------------------------------------------
rem  Deleted first so running this twice leaves one rule, not two.
echo.
echo   Allowing inbound TCP %DCC_START%-%DCC_END% (DCC sends, and the admin console
echo   when it has to listen) ...
call netsh advfirewall firewall delete rule name="DCCore DCC sends" >nul 2>&1
call netsh advfirewall firewall add rule name="DCCore DCC sends" dir=in action=allow protocol=TCP localport=%DCC_START%-%DCC_END%
if errorlevel 1 goto :failed

rem  No parenthesised block here: a ")" inside an echo would end it.
call netsh advfirewall firewall delete rule name="DCCore dashboard" >nul 2>&1
if not "%WEB_ON%"=="1" goto :no_dashboard
echo   Allowing inbound TCP %WEB_PORT% (the dashboard - reachable from other
echo   machines only if WEBUI_HOST is not 127.0.0.1) ...
call netsh advfirewall firewall add rule name="DCCore dashboard" dir=in action=allow protocol=TCP localport=%WEB_PORT%
if errorlevel 1 goto :failed
goto :done
:no_dashboard
echo   The dashboard is off, so no rule for it.

:done
echo.
echo   Done. Remember the same ports must also be forwarded on your router
echo   for anyone outside your network to download from you.
echo.
pause
exit /b 0

:failed
echo.
echo   netsh could not add the rule. Windows Defender Firewall may be managed
echo   by another product or by policy; add the rule in that product instead.
echo.
pause
exit /b 1
