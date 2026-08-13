"""페이지 레이아웃 분석 모듈 — 본문/제목/수식/그림/표/여백 영역을 분리한다.

pix2text에 동봉된 DocYoloLayoutParser(문서 레이아웃 전용 YOLO)를 사용한다.
이 영역 정보를 바탕으로 OCR 파이프라인이:
  - 그림/표는 OCR하지 않고 이미지로 추출하고,
  - 수식은 LaTeX 인식으로 보내고,
  - 본문/제목은 칼럼 번호(col_number)에 따라 읽기 순서대로 텍스트 인식으로 보내며,
  - 페이지 머리말·쪽번호는 버린다.

성능: 모델은 첫 사용 시 한 번만 로드해 모든 페이지에 재사용한다.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# ★ common 이 PIX2TEXT_HOME 을 설정한다 — pix2text 가 import 되기 전에 정해져
# 있어야 하므로 모듈 최상단에서 못 박는다. common 은 프로젝트 모듈을 하나도
# import 하지 않아 순환 위험이 없다.
import common  # noqa: E402

# 레이아웃 추론 입력 크기. 재측정(5권 13쪽, 검토단 4 지적 반영): 896과 1024의 영역
# 회수는 사실상 동일하고(text 147=147, image 33/31, formula 30/31 — 차이는 양방향
# 노이즈 수준), 오히려 표 밀집 페이지(응용수학 p841)에서는 1024가 그림을 덜 찾았다.
# 속도는 896이 약 1.15배 빠르다(예전 주석의 '약 절반'은 과장이었다 — 정정). 품질이
# 동등하므로 더 빠른 896을 유지한다. (체크포인트 학습 해상도는 1024다.)
import tuning

LAYOUT_IMGSZ = tuning.get("layout", "imgsz")
LAYOUT_CONF = tuning.get("layout", "conf")

# DocYoloLayoutParser가 돌려주는 ElementType 이름 → 우리 분류
TEXT_TYPES = {"TEXT", "TITLE", "PLAIN_TEXT", "ABANDON_TEXT"}
FORMULA_TYPES = {"FORMULA", "ISOLATE_FORMULA", "ISOLATED"}
IMAGE_TYPES = {"FIGURE", "TABLE"}
DROP_TYPES = {"IGNORED", "ABANDON"}  # 머리말/꼬리말/쪽번호 등

_parser = None


def load_parser():
    """레이아웃 분석 모델을 lazy-load한다. 미설치 시 RuntimeError."""
    global _parser
    if _parser is None:
        try:
            import logging

            logging.disable(logging.INFO)
            from pix2text.doc_yolo_layout_parser import DocYoloLayoutParser
        except ImportError as e:
            # pip으로 재설치하라고 안내하면 안 된다 — pix2text는 onnxruntime(CPU판)을
            # 끌어와 iGPU 가속용 onnxruntime-directml의 DLL을 덮어쓴다(실제로 겪음).
            raise RuntimeError(
                "레이아웃 분석 라이브러리(pix2text)를 불러올 수 없습니다.\n"
                "백업에서 복원하세요: Documents\\PDF_Editor_백업\\\n"
                "pip으로 재설치하면 iGPU 가속 런타임이 깨질 수 있습니다."
            ) from e
        # 로컬 모델이 없으면 pix2text가 네트워크 다운로드를 시도한다 —
        # '완전 오프라인' 보증을 지키기 위해 여기서 확인하고 명확히 알린다.
        # 폴더 존재만 보면 부분 손상(중단된 복원·동기화 미완)을 놓쳐 실제로
        # 외부 접속을 시도한다 — 필요한 파일 이름까지 확인한다(검토단 관측).
        model_dir = common.P2T_MODEL_DIR / "layout-docyolo"
        weight = model_dir / "doclayout_yolo_docstructbench_imgsz1024.pt"
        if not weight.is_file():
            raise RuntimeError(
                f"레이아웃 모델 파일이 없습니다: {weight}\n"
                "백업에서 복원하거나, 인터넷이 되는 환경에서 1회 실행해 받으세요."
            )
        _parser = DocYoloLayoutParser()
    return _parser


def analyze(page_image) -> list[dict]:
    """페이지 이미지를 분석해 영역 목록을 반환한다.

    각 영역: {"kind": "text"|"formula"|"image"|"drop", "type": 원본타입,
              "x0","y0","x1","y1"}  (좌표는 page_image 픽셀 공간)
    """
    parser = load_parser()
    parsed = parser.parse(page_image, imgsz=LAYOUT_IMGSZ, conf=LAYOUT_CONF)
    elements = parsed[0] if isinstance(parsed, tuple) else parsed

    regions: list[dict] = []
    for el in elements:
        type_name = str(el["type"])  # ElementType enum → 이름 문자열
        pts = el["position"]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if type_name in IMAGE_TYPES:
            kind = "image"
        elif type_name in FORMULA_TYPES:
            kind = "formula"
        elif type_name in TEXT_TYPES:
            kind = "text"
        else:
            kind = "drop"
        regions.append({
            "kind": kind,
            "type": type_name,
            "col": el.get("col_number", 1),  # 다단 페이지의 칼럼 번호(1,2,...; 머리말은 -1)
            "x0": int(min(xs)), "y0": int(min(ys)),
            "x1": int(max(xs)), "y1": int(max(ys)),
        })
    return regions
