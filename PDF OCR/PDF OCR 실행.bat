@echo off
chcp 65001 >nul
set PYTHONUTF8=1
where python >nul 2>nul
if errorlevel 1 (
  echo [오류] Python을 찾을 수 없습니다. python.org에서 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
python "%~dp0..\scripts\pdf_ocr.py"
echo.
pause
