@echo off
REM ============================================================================
REM run-live.bat — Single-cycle runner for Windows Task Scheduler
REM ----------------------------------------------------------------------------
REM Invoked every 5 minutes by Task Scheduler. Runs one analysis cycle and
REM exits. Stdout + stderr rotate into data\runner.log; bot.log (logger) is
REM written by the app itself.
REM ============================================================================

setlocal

REM Anchor to repo dir regardless of invocation context
cd /d "%~dp0"

REM ---- Config knobs ----------------------------------------------------------
set VENV=venv
set PY=python
set TIMEFRAME=1h
set MINCONF=0.72
set LOGFILE=data\runner.log

REM ---- Activate venv if present ---------------------------------------------
if exist "%VENV%\Scripts\activate.bat" (
    call "%VENV%\Scripts\activate.bat"
) else (
    echo [%DATE% %TIME%] WARN: no venv at %VENV%, using system Python >> %LOGFILE%
)

REM ---- Ensure data dir for logs ---------------------------------------------
if not exist data mkdir data

REM ---- Timestamped run header -----------------------------------------------
echo. >> %LOGFILE%
echo [%DATE% %TIME%] === cycle start === >> %LOGFILE%

REM ---- Run one cycle --------------------------------------------------------
%PY% -m src.main run --exchange hyperliquid --timeframe %TIMEFRAME% --min-confidence %MINCONF% --once >> %LOGFILE% 2>&1

set EXITCODE=%ERRORLEVEL%
echo [%DATE% %TIME%] === cycle end (exit %EXITCODE%) === >> %LOGFILE%

REM ---- Rotate runner.log if over ~5 MB --------------------------------------
for %%A in ("%LOGFILE%") do if %%~zA GTR 5242880 (
    move /Y "%LOGFILE%" "%LOGFILE%.1" >nul
    echo [%DATE% %TIME%] log rotated >> %LOGFILE%
)

exit /b %EXITCODE%
