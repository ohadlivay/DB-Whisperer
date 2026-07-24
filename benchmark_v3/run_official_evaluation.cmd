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
if defined OPENROUTER_API_KEY goto launch

echo OPENROUTER_API_KEY is not set. Enter it in the masked prompt.
for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "$s=Read-Host 'OpenRouter API key' -AsSecureString; $b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)}"`) do set "OPENROUTER_API_KEY=%%K"
if defined OPENROUTER_API_KEY goto launch

echo ERROR: No OpenRouter API key was supplied.
set "EXIT_CODE=1"
goto finish

:launch
set "REPETITIONS=5"
if "%~2"=="1" set "REPETITIONS=1"
if "%~1"=="" (
  "%PYTHON_EXE%" -m benchmark_v3.run_evaluation --workers 2 --repetitions %REPETITIONS% --interactive-progress
) else (
  "%PYTHON_EXE%" -m benchmark_v3.run_evaluation --workers 2 --repetitions %REPETITIONS% --interactive-progress --campaign-id "%~1"
)
set "EXIT_CODE=%ERRORLEVEL%"
set "OPENROUTER_API_KEY="

:finish
echo.
echo Evaluation runner exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
