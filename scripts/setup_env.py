# -*- coding: utf-8 -*-
"""다른 PC에서 이 도구를 쓸 수 있게 폴더 안에 파이썬 환경을 만든다.

    python scripts\\setup_env.py

무엇을 하는가
  1. 쓸 만한 Python 3.14 를 찾는다
  2. <PDF Editor>\\venv 를 만든다  (폴더 안이므로 도구를 옮기면 같이 간다)
  3. scripts\\requirements.txt 로 패키지를 설치한다
  4. 저장소에 담기엔 큰 모델·언어데이터(252MB)를 릴리스에서 받는다
  5. iGPU 가속(DirectML)을 복원하고 전부 검증한다

이것만 실행하면 새 PC에서 바로 쓸 수 있다.

왜 venv 를 폴더 안에 두는가
  기존에는 시스템 파이썬의 site-packages 에 의존해서, 폴더만 복사하면
  다른 PC에서 동작하지 않았다. 폴더 안에 두면 폴더가 곧 환경이 된다.

★ 이 스크립트가 못 하는 것 (별도 프로그램이라 폴더에 넣을 성격이 아니다)
  - Tesseract-OCR : 대상 PC에 설치돼 있어야 한다 (common.TESSERACT_DIR)
  - Python 본체   : venv 를 만들려면 대상 PC에 Python 3.14 가 필요하다

★ DirectML 주의 (과거 실제 사고)
  pix2text 가 cnocr[ort-cpu]·optimum[onnxruntime] 을 하드 의존으로 요구해서
  무엇을 고정하든 일반 onnxruntime 이 따라 들어오고, 같은 폴더를 덮어써
  iGPU 가속을 죽인다. 그래서 설치 맨 끝에 DirectML 을 다시 씌우고 확인한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = BASE_DIR / "venv"
# requirements.lock 은 빌드 불가능한 이력용 스냅숏이다(그 파일 주석 참조).
# 실제 설치는 소스의 import 에서 뽑은 requirements.txt 를 쓴다.
REQ = Path(__file__).resolve().parent / "requirements.txt"
VENV_PY = VENV_DIR / "Scripts" / "python.exe"

# 저장소에 담기엔 큰 자산(pix2text 모델 215MB + tessdata 37MB)의 배포 위치.
# GitHub 는 파일당 100MB 한계가 있고 여기엔 87MB 짜리가 있어, 저장소가 아니라
# 릴리스 자산으로 둔다. fetch_assets() 가 받아서 scripts\ 아래에 푼다.
ASSETS_URL = ("https://github.com/mijnch/pdf-ocr-korean-textbook/releases/"
              "download/assets-v1/pdf-editor-assets.zip")

# 설치 후 반드시 되어야 하는 것들 (도구가 실제로 쓰는 최상위 패키지)
REQUIRED_IMPORTS = ["PIL", "numpy", "pypdfium2", "pix2text", "transformers", "onnxruntime"]


def say(msg: str) -> None:
    print(msg, flush=True)


def find_python() -> str | None:
    """venv 의 바탕이 될 Python 3.14 를 찾는다."""
    # 이 스크립트를 돌린 파이썬이 이미 3.14 면 그것을 쓴다
    if sys.version_info[:2] == (3, 14):
        return sys.executable
    for cand in ("python", "python3", "py"):
        exe = shutil.which(cand)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=30)
            if out.stdout.strip() == "3.14":
                return exe
        except Exception:
            pass
    return None


def run(args: list[str], desc: str) -> bool:
    say(f"  → {desc}")
    try:
        p = subprocess.run(args, timeout=3600)
        return p.returncode == 0
    except Exception as e:
        say(f"    실패: {type(e).__name__}: {e}")
        return False


def main() -> int:
    say("=" * 62)
    say(" PDF Editor 환경 설치")
    say("=" * 62)
    say(f" 도구 폴더 : {BASE_DIR}")

    if not REQ.is_file():
        say(f"\n[오류] 패키지 목록이 없습니다: {REQ}")
        return 1

    if VENV_PY.is_file():
        say(f"\n이미 환경이 있습니다: {VENV_DIR}")
        say("다시 만들려면 그 폴더를 지우고 이 스크립트를 다시 실행하세요.")
        return verify()

    base = find_python()
    if not base:
        say("\n[오류] Python 3.14 를 찾지 못했습니다.")
        say("  python.org 에서 3.14 를 설치한 뒤 다시 실행하세요.")
        say(f"  (현재 이 스크립트를 돌린 파이썬: {sys.version.split()[0]})")
        return 1
    say(f" 바탕 파이썬: {base}")

    say("\n[1/5] 가상환경 생성")
    if not run([base, "-m", "venv", str(VENV_DIR)], f"venv → {VENV_DIR}"):
        say("    실패했습니다.")
        return 1

    say("\n[2/5] 패키지 설치 (수 분 걸립니다. 약 2GB를 받습니다)")
    run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "-q"], "pip 갱신")
    if not run([str(VENV_PY), "-m", "pip", "install", "-r", str(REQ)], "requirements.txt 설치"):
        say("    설치에 실패했습니다. 위의 오류를 확인하세요.")
        return 1

    if not fetch_assets():
        return 1

    # ★★ 이 단계를 빼면 iGPU 가속이 죽는다.
    # pix2text 가 cnocr[ort-cpu] 와 optimum[onnxruntime] 을 하드 의존으로 요구해서,
    # 무엇을 어떻게 고정하든 일반 onnxruntime 이 따라 들어온다(dry-run 으로 확인).
    # 둘은 같은 onnxruntime/ 폴더에 설치되므로 나중에 깔린 쪽의 DLL 이 이긴다.
    # 그래서 맨 마지막에 DirectML 판을 덮어씌운다. --no-deps 로 의존성 재해결을
    # 막아야 일반판이 다시 딸려오지 않는다.
    say("\n[4/5] iGPU 가속 복원 (DirectML 덮어쓰기)")
    run([str(VENV_PY), "-m", "pip", "install", "--force-reinstall", "--no-deps",
         "onnxruntime-directml==1.24.4"], "onnxruntime-directml 강제 재설치")

    say("\n[5/5] 검증")
    return verify()


def fetch_assets() -> bool:
    """저장소에 담을 수 없는 큰 자산(모델·언어데이터)을 받아 온다.

    왜 저장소에 없는가
      pix2text 모델 215MB + tessdata 37MB = 252MB 다. GitHub 는 파일당 100MB
      한계가 있고(여기 87MB 짜리가 있다) 저장소를 그만큼 불리는 것도 옳지 않다.
      그래서 릴리스 자산으로 따로 두고 여기서 받는다.

    ★ 왜 pix2text 에게 맡기지 않는가
      layout-docyolo(Apache-2.0)와 mfd-1.5-onnx(MIT)는 pix2text 가 스스로
      HuggingFace 에서 받을 수 있다. 그러나 mfr-1.5-onnx-kvint8 은 **받을 수
      없다** — 원본 ONNX 에 KV캐시가 없어 생성이 O(n^2)이던 것을, initializer 를
      PyTorch 로 이식해 with-past 로 재수출하고 int8 양자화한 자체 제작물이다
      (실측 인식 4.16배). 그래서 셋을 한 묶음으로 릴리스에 올려 한 번에 받는다.
    """
    import urllib.request
    import zipfile

    need_models = not (BASE_DIR / "scripts" / "models" / "pix2text" / "1.1"
                       / "mfr-1.5-onnx-kvint8" / "encoder_model.onnx").is_file()
    need_tess = not (BASE_DIR / "scripts" / "tessdata" / "best" / "kor.traineddata").is_file()
    if not (need_models or need_tess):
        say("\n[3/5] 모델·언어데이터: 이미 있습니다 — 건너뜁니다")
        return True

    say("\n[3/5] 모델·언어데이터 내려받기 (약 252MB)")
    zpath = BASE_DIR / "_assets.zip"
    try:
        def hook(blocks, bs, total):
            if total > 0 and blocks % 200 == 0:
                pct = min(100, blocks * bs * 100 // total)
                print(f"\r    {pct:3d}%  ({blocks*bs//1048576}MB / {total//1048576}MB)",
                      end="", flush=True)

        urllib.request.urlretrieve(ASSETS_URL, zpath, reporthook=hook)
        print()
        say(f"    받음: {zpath.stat().st_size/1048576:.0f}MB — 푸는 중")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(BASE_DIR / "scripts")
        zpath.unlink(missing_ok=True)
        say("    완료")
        return True
    except Exception as e:
        zpath.unlink(missing_ok=True)
        say(f"    [실패] {type(e).__name__}: {e}")
        say(f"    {ASSETS_URL} 를 직접 받아 scripts\\ 아래에 풀어도 됩니다.")
        return False


def verify() -> int:
    """핵심 import 와 DirectML 을 확인한다."""
    if not VENV_PY.is_file():
        say(f"  [오류] {VENV_PY} 가 없습니다.")
        return 1

    ok = True
    for mod in REQUIRED_IMPORTS:
        p = subprocess.run([str(VENV_PY), "-c", f"import {mod}"], capture_output=True)
        mark = "OK " if p.returncode == 0 else "실패"
        if p.returncode != 0:
            ok = False
        say(f"  {mark}  import {mod}")

    # iGPU 가속 여부 — 일반 onnxruntime 이 깔리면 여기서 드러난다
    p = subprocess.run(
        [str(VENV_PY), "-c",
         "import onnxruntime as o;print(','.join(o.get_available_providers()))"],
        capture_output=True, text=True)
    providers = p.stdout.strip()
    say(f"\n  onnxruntime providers: {providers or '(조회 실패)'}")
    if "DmlExecutionProvider" in providers:
        say("  → DirectML 있음: iGPU 가속이 살아 있습니다.")
    else:
        say("  → ★ DirectML 이 없습니다. iGPU 가속 없이 CPU로만 돕니다.")
        say("     고치려면:  venv\\Scripts\\python -m pip install --force-reinstall onnxruntime-directml")

    # Tesseract — 폴더 밖 의존물이라 여기서 만들어 줄 수 없다
    say("")
    tess = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if tess.is_file():
        say(f"  OK   Tesseract: {tess}")
    else:
        say(f"  ★    Tesseract 가 없습니다: {tess}")
        say("       별도 프로그램이라 이 스크립트가 설치하지 못합니다. 직접 설치하세요.")
        ok = False

    say("")
    say("=" * 62)
    if ok:
        say(" 준비 완료 — 'PDF OCR 실행.bat' 으로 시작하세요.")
    else:
        say(" 위의 ★ 항목을 해결한 뒤 다시 실행하세요.")
    say("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
