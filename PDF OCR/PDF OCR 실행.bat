@echo off
chcp 65001 >nul
set PYTHONUTF8=1
title PDF OCR

rem 끌어다 놓은 파일은 %* 로 그대로 넘긴다. 처리는 파이썬이 한다.
rem goto/레이블을 쓰지 않는다 - UTF-8 배치에서 cmd 가 파일을 바이트로 되짚다가
rem 한글 중간에서 재개해 스크립트가 깨진다(실측).

set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo.
    echo  [오류] 파이썬을 찾을 수 없습니다.
    echo         이 폴더의 "환경 설치.bat" 을 먼저 실행하세요.
    echo.
    pause
    exit /b 1
  )
  set "PY=python"
)

"%PY%" "%~dp0..\scripts\pdf_ocr.py" %*

echo.
pause
