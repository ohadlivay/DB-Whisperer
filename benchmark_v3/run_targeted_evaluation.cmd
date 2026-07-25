@echo off
setlocal
set "REPO_ROOT=%~dp0.."
set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto key
set "PYTHON_EXE=%REPO_ROOT%\..\..\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto key
echo ERROR: No project virtualenv Python was found.
set "EXIT_CODE=1"
goto finish

:key
cd /d "%REPO_ROOT%"
if defined OPENROUTER_API_KEY goto launch
echo OPENROUTER_API_KEY is not set. Enter it in the masked prompt.
for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "$s=Read-Host 'OpenRouter API key' -AsSecureString; $b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)}"`) do set "OPENROUTER_API_KEY=%%K"
if defined OPENROUTER_API_KEY goto launch
echo ERROR: No OpenRouter API key was supplied.
set "EXIT_CODE=1"
goto finish

:launch
set "RUN_ID=%~1"
if "%RUN_ID%"=="" set "RUN_ID=targeted-semantic-regression"
"%PYTHON_EXE%" -m benchmark_v3.run_targeted_evaluation --campaign-id "%RUN_ID%" --workers 2 --repetitions 1 --arm semantic_only --arm full --case-id from_2024_birth --case-id from_2024_admission --case-id ctl_from_2024_birth --case-id ctl_from_2024_admission --case-id diagnoses_occurrences --case-id diagnoses_distinct_patients --case-id ctl_diagnoses_occurrences --case-id ctl_diagnoses_distinct_patients --case-id stay_hospital --case-id stay_icu --case-id ctl_stay_hospital --case-id ctl_stay_icu --case-id icu_mortality_by_first_careunit
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto clear
)
"%PYTHON_EXE%" -m benchmark_v3.run_targeted_evaluation --campaign-id "%RUN_ID%-admission-control" --workers 2 --repetitions 1 --arm baseline --arm candidate_only --arm semantic_only --arm full --case-id ctl_from_2024_admission
set "EXIT_CODE=%ERRORLEVEL%"

:clear
set "OPENROUTER_API_KEY="
:finish
echo.
echo Targeted evaluation runner exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
