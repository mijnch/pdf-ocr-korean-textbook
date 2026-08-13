"""PDF OCR 공통 유틸리티.

- 입력/출력 폴더 경로 계산
- 입력 폴더의 PDF 파일 탐색
- 외부 도구(Tesseract) 경로 설정
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 폴더 구조: <PDF Editor>/scripts/common.py → BASE_DIR = <PDF Editor>
BASE_DIR = Path(__file__).resolve().parent.parent

TESSERACT_DIR = Path(r"C:\Program Files\Tesseract-OCR")
# 고정밀(tessdata_best) 한국어+영어 모델
TESSDATA_DIR = Path(__file__).resolve().parent / "tessdata" / "best"

# pix2text 모델(레이아웃·수식 검출/인식, 약 215MB)도 이 폴더 안에 둔다.
# 라이브러리 기본값은 %APPDATA%\pix2text 라, 그대로 두면 도구가 자기 폴더 밖에
# 215MB를 남기고 폴더만 복사해서는 동작하지 않는다. pix2text 는 PIX2TEXT_HOME
# 을 존중하므로(pix2text/utils.py: os.getenv('PIX2TEXT_HOME', ...)) 그것으로
# 폴더 안을 가리킨다 — 라이브러리가 공식 지원하는 경로이지 우회가 아니다.
# ★ pix2text 가 import 되기 전에 설정돼야 하므로 모듈 최상단에 둔다.
P2T_HOME = Path(__file__).resolve().parent / "models" / "pix2text"
P2T_MODEL_DIR = P2T_HOME / "1.1"
os.environ["PIX2TEXT_HOME"] = str(P2T_HOME)

# 작업용 임시 폴더도 이 폴더 안에 둔다 — 기본값(%TEMP%)을 쓰면 변환 중 수백 MB의
# 페이지 이미지가 폴더 밖에 생긴다. 정상 종료 시에는 자동 정리되지만, 중단·강제
# 종료가 나면 %TEMP% 에 잔재가 남는다. 여기 두면 흔적이 폴더 안에서만 생긴다.
# 실제 사용은 tempfile.TemporaryDirectory(dir=TMP_ROOT) 형태다.
TMP_ROOT = BASE_DIR / ".tmp"


def tmp_root() -> Path:
    """임시 폴더의 부모를 돌려준다(없으면 만든다).

    쓰기가 막힌 위치에 도구가 놓였을 때까지 실패시키지는 않는다 —
    그 경우에만 시스템 기본 임시 폴더로 물러선다(None 반환 시 tempfile 기본).
    """
    try:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        return TMP_ROOT
    except OSError:
        return None

# ultralytics의 자동 pip 설치를 차단한다 — onnxruntime-directml(iGPU 가속)을 배포명이
# 다르다는 이유로 "onnxruntime 없음"으로 오판해 CPU판을 재설치하며 DirectML 런타임
# DLL을 덮어써 버린다(검토단이 실제로 유발). 이 가드는 setup_external_tools()가
# 아니라 모듈 import 시점에 둔다 — main()을 거치지 않는 경로(pdf_audit·pdf_splice
# 직접 실행, 테스트 하네스 등)에서도 오프라인 보증이 깨지지 않게 한다.
os.environ["YOLO_AUTOINSTALL"] = "false"
# 모델 캐시가 부분 손상되면(중단된 복원, 동기화 미완) 라이브러리가 스스로
# huggingface·api.github.com으로 나가려 든다 — 폴더 존재만 확인하는 가드로는
# 막히지 않는다(검토단이 실제 외부 호출을 관측). 오프라인을 환경변수로 못 박아
# 네트워크 시도 자체를 차단하고, 한국어 안내로 끝나게 한다.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def feature_dirs(feature: str) -> tuple[Path, Path]:
    """기능 이름("PDF OCR" 등)에 해당하는 (입력 폴더, 출력 폴더)를 반환한다.

    폴더가 없으면 생성해서라도 항상 유효한 경로를 돌려준다. 같은 이름의
    '파일'이 자리를 차지하고 있거나 권한이 없으면 OSError가 그대로 오르는데,
    호출부(main)에서 한국어 안내로 감싼다 — 생 트레이스백이 사용자에게
    보이던 유일한 경로였다(검토단 지적).
    """
    input_dir = BASE_DIR / feature / f"{feature} 입력 폴더"
    output_dir = BASE_DIR / feature / f"{feature} 출력 폴더"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return input_dir, output_dir


def find_skipped_subfolders(input_dir: Path) -> list[str]:
    """PDF가 든 하위 폴더 이름 목록 — 탐색하지 않으므로 알려 주기 위해서다."""
    out = []
    for d in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        if any(f.suffix.lower() == ".pdf" for f in d.rglob("*") if f.is_file()):
            out.append(d.name)
    return out


def find_stale_outputs(output_dir: Path, marker: str = "> [변환 완료]") -> list[str]:
    """완료 표식이 없는 잘린 산출물 목록 — 이전 실행이 중단된 잔해다.

    잔해가 정식 이름('책_OCR.md')을 차지한 채 남으면 다음 실행의 완성본이
    '책_OCR (1).md'로 밀려나고, 사람도 AI도 자연스럽게 잔해를 먼저 연다
    (검토단 실증). 실행 시작 때 알려 주어 지우고 다시 돌리게 한다.
    """
    out = []
    for f in sorted(output_dir.glob("*_OCR.md")):
        try:
            if marker not in f.read_text(encoding="utf-8", errors="replace"):
                out.append(f.name)
        except OSError:
            continue
    return out


def find_pdfs(input_dir: Path) -> list[Path]:
    """입력 폴더의 PDF 파일 목록을 이름순으로 반환한다. 없으면 빈 리스트."""
    return sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    )


def human_size(num_bytes: float) -> str:
    """바이트 수를 'x.x MB' 형태의 읽기 좋은 문자열로 변환한다."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:,.1f} {unit}"
        num_bytes /= 1024


def setup_external_tools() -> None:
    """Tesseract를 PATH에 추가하고 한국어 언어 데이터를 지정한다.

    또한 ultralytics의 자동 pip 설치를 차단한다 — onnxruntime-directml(iGPU 가속)을
    배포명이 다르다는 이유로 "onnxruntime 없음"으로 오판해 CPU판을 재설치하려 든다.

    도구가 없으면 RuntimeError를 던진다.
    (자동 pip 설치 차단은 모듈 import 시점에 이미 걸어 둔다 — 위 os.environ 참조.)
    """
    if not (TESSERACT_DIR / "tesseract.exe").exists():
        raise RuntimeError(
            "Tesseract가 설치되어 있지 않습니다.\n"
            "https://github.com/UB-Mannheim/tesseract/wiki 에서 설치하세요."
        )

    for lang in ("kor", "eng"):  # 하나만 없어도 모든 페이지가 원인 불명으로 실패한다
        if not (TESSDATA_DIR / f"{lang}.traineddata").exists():
            raise RuntimeError(
                f"언어 데이터가 없습니다: {TESSDATA_DIR / (lang + '.traineddata')}\n"
                "https://github.com/tesseract-ocr/tessdata_best 에서 kor.traineddata와 "
                "eng.traineddata를 받아 위 경로에 넣으세요."
            )

    os.environ["PATH"] = os.pathsep.join(
        [str(TESSERACT_DIR), os.environ.get("PATH", "")])
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)

    # 장시간 변환 중 백그라운드 작업에 CPU를 뺏기지 않도록 HIGH 우선순위로 실행
    try:
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x80)  # HIGH_PRIORITY_CLASS
    except Exception:
        pass


def exit_with_message(message: str, code: int = 1) -> None:
    """오류 메시지를 출력하고 종료한다. (배치파일의 pause가 창을 유지해 준다.)"""
    print(f"\n[오류] {message}", file=sys.stderr)
    sys.exit(code)
