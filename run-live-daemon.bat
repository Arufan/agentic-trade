@echo off
REM ============================================================================
REM run-live-daemon.bat — Persistent daemon with auto-restart on crash.
REM ----------------------------------------------------------------------------
REM Runs `src.main run --interval 300` in an infinite loop. If the bot exits
REM (crash, Ctrl-C handled, kill-switch trip), waits RESTART_DELAY seconds and
REM relaunches. Logs go to data\daemon.log (rotated at ~5 MB).
REM
REM Usage:
REM   run-live-daemon.bat            (foreground — close terminal = stop)
REM   start "bot" /min run-live-daemon.bat   (minimized background window)
REM
REM To stop cleanly: Ctrl-C in the window, or close the window.
REM ============================================================================

setlocal
cd /d "%~dp0"

set VENV=venv
set PY=python
set EXCHANGE=hyperliquid
set TIMEFRAME=1h
set INTERVAL=300
set MINCONF=0.72
set RESTART_DELAY=30
set LOGFILE=data\daemon.log

if exist "%VENV%\Scripts\activate.bat" (
    call "%VENV%\Scripts\activate.bat"
)

if not exist data mkdir data

:loop
    echo. >> %LOGFILE%
    echo [%DATE% %TIME%] === daemon start === >> %LOGFILE%

    %PY% -m src.main run --exchange %EXCHANGE% --timeframe %TIMEFRAME% --interval %INTERVAL% --min-confidence %MINCONF% >> %LOGFILE% 2>&1

    set EXITCODE=%ERRORLEVEL%
    echo [%DATE% %TIME%] === daemon exited (code %EXITCODE%) — restart in %RESTART_DELAY%s === >> %LOGFILE%

    REM Rotate log if > 5 MB
    for %%A in ("%LOGFILE%") do if %%~zA GTR 5242880 (
        move /Y "%LOGFILE%" "%LOGFILE%.1" >nul
        echo [%DATE% %TIME%] log rotated >> %LOGFILE%
    )

    timeout /t %RESTART_DELAY% /nobreak >nul
goto loop
