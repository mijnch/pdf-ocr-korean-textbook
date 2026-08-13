@echo off
chcp 65001 >nul
set PYTHONUTF8=1

rem 폴더 안의 venv 를 먼저 쓴다 — 도구를 다른 PC로 옮겨도 그대로 동작한다.
rem venv 가 없으면 예전처럼 시스템 Python 으로 물러선다.
set "PY=%~dp0..\venv\Scripts\python.exe"
if exist "%PY%" goto run

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] 파이썬을 찾을 수 없습니다.
  echo        이 폴더의 "환경 설치.bat" 을 먼저 실행하세요.
  pause
  exit /b 1
)
set "PY=python"

:run
"%PY%" "%~dp0..\scripts\pdf_ocr.py"
echo.
pause
