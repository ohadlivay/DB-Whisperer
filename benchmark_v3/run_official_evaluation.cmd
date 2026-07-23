@echo off
setlocal
cd /d "%~dp0.."

if "%~1"=="" (
  python -m benchmark_v3.run_evaluation --workers 2 --repetitions 5
) else (
  python -m benchmark_v3.run_evaluation --workers 2 --repetitions 5 --campaign-id "%~1"
)

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Evaluation runner exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
