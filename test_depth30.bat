@echo off
cd /d "%~dp0"
echo Running Duckish depth-30 smoke test...
echo.
(
  echo position startpos
  echo go depth 30
  ping -n 120 127.0.0.1 >nul
) | target\release\duckish.exe > result.txt 2>&1
type result.txt
echo.
echo === Output saved to result.txt - check for 'bestmove' and NO 'panic' ===
pause