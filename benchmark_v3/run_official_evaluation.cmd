@echo off
setlocal
set "REPO_ROOT=%~dp0.."
set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto run

set "PYTHON_EXE=%REPO_ROOT%\..\..\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto run

echo ERROR: No project virtualenv Python was found.
echo Expected either "%REPO_ROOT%\.venv\Scripts\python.exe"
echo or the repository-root virtualenv when launched from a worktree.
set "EXIT_CODE=1"
goto finish

:run
cd /d "%REPO_ROOT%"
if "%~1"=="" (
  "%PYTHON_EXE%" -m benchmark_v3.run_evaluation --workers 2 --repetitions 5
) else (
  "%PYTHON_EXE%" -m benchmark_v3.run_evaluation --workers 2 --repetitions 5 --campaign-id "%~1"
)
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
echo Evaluation runner exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
