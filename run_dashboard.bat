@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
  start "" pythonw -X utf8 "%~dp0fore_ai_dashboard.py"
  goto :eof
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  start "" py -3 "%~dp0fore_ai_dashboard.py"
  goto :eof
)
start "" python "%~dp0fore_ai_dashboard.py"
endlocal
