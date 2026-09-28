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

rem venv 의 python.exe 는 있어도, 폴더째 다른 PC로 옮겨 와 연결된 Python 본체가 없으면
rem 기동하지 못한다. 원인 불명 오류 대신 무엇을 하면 되는지 알린다.
rem 블록 안 echo 에는 괄호를 쓰지 않는다 - 닫는 괄호가 블록을 끝내 버린다.
"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  [오류] 파이썬을 실행할 수 없습니다.
  echo         venv 는 만들어진 PC의 Python 3.14 를 가리키므로 다른 PC로 옮기면 그대로 쓸 수 없습니다.
  echo         도구 폴더의 "환경 설치.bat" 을 실행하면 이 PC에 맞게 새로 만들어집니다.
  echo.
  pause
  exit /b 1
)

"%PY%" "%~dp0..\scripts\pdf_ocr.py" %*

echo.
pause
