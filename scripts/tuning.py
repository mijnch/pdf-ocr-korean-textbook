"""조정값 로더 — 'PDF Editor\\설정.toml'에서 읽고, 없거나 잘못되면 기본값을 쓴다.

여기 적힌 기본값은 전부 코퍼스 실측으로 정해진 것이다. 각 값 옆의 근거를
같이 두어, 설정 파일로 빼내도 '왜 그 값인지'가 값과 함께 남게 한다.

설정 실수가 변환 자체를 막지 않도록 설계했다:
  - 파일이 없으면 기본값으로 조용히 동작한다.
  - 값이 빠져 있으면 그 항목만 기본값을 쓴다.
  - 형이 다르거나 범위를 벗어나면 그 항목만 버리고 경고를 출력한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "설정.toml"

# (기본값, 허용 범위 또는 None) — 범위는 [최소, 최대] 이며 양끝 포함.
DEFAULTS: dict[str, dict[str, tuple]] = {
    "recognition": {           # 본문 인식
        "render_dpi": (200, [72, 600]),          # 기준 렌더 해상도(좌표 공간)
        "hires_max_dpi": (400, [200, 1200]),     # 원본 해상도 활용 상한
        "tess_lang": ("kor+eng", None),          # Tesseract 언어 데이터
        "min_line_conf": (35, [0, 100]),         # 줄 평균 신뢰도 하한
        "rescue_min_conf": (60, [0, 100]),       # 보충 줄은 더 엄격(실측 86~94 vs 36~39)
        "rescue_min_wordish": (2, [0, 20]),      # 보충 줄 최소 실질 글자
        "page_ocr_timeout": (180, [10, 3600]),   # 병적 페이지 차단(정상 1~3초)
        "mask_margin": (4, [0, 40]),             # 그림·수식 가림 여백
    },
    "layout": {                # 영역 판정
        "imgsz": (896, [320, 1536]),             # 레이아웃 입력 크기(1024 대비 2배 빠름)
        "conf": (0.2, [0.01, 0.9]),              # 레이아웃 신뢰도 하한
        "header_band_ratio": (0.072, [0.0, 0.3]),   # 머리말 띠(실측 6.8% vs 본문 7.3%)
        "header_ext_ratio": (0.12, [0.0, 0.4]),     # 확장 띠(내용 판정 병행)
        "foot_band_ratio": (0.94, [0.5, 1.0]),      # 내장책 하단 쪽번호 띠
        "min_sane_char_ratio": (0.5, [0.0, 1.0]),   # 정상 글자율 하한(내장 5권 최저 0.875)
        "para_join_margin": (40, [0, 300]),         # 양끝맞춤 판정 여유(픽셀)
        "callout_color_ratio": (0.18, [0.0, 1.0]),  # 색 박스 판정
        "caption_max_chars": (45, [0, 300]),        # 캡션으로 볼 최대 길이
    },
    "figure": {                # 그림 저장
        "margin": (6, [0, 60]),
        "max_px": (2000, [200, 8000]),           # 최장변 상한
        "colors": (256, [2, 256]),               # 양자화 색 수(용량 약 절반)
    },
    "formula": {               # 수식 인식
        "mfd_resized_shape": (1024, [320, 2048]),
        "mfr_batch_size": (8, [1, 64]),          # 16/32는 실측 역효과
        "min_mfr_score": (0.35, [0.0, 1.0]),     # 정상 수식은 0.7 이상
        "mfr_max_tokens": (768, [64, 4096]),     # 최장 정상 수식 ~450토큰
        "retry_pad": (6, [0, 60]),               # 퇴화 재시도 크롭 확장
    },
    "table": {                 # 표 추출
        "ink_thr": (200, [0, 255]),              # 교과서 괘선은 회색(150은 놓침)
        "h_line_ink": (0.55, [0.0, 1.0]),
        "v_line_ink": (0.50, [0.0, 1.0]),
        "min_fill": (0.5, [0.0, 1.0]),
        "max_cell_chars": (90, [10, 1000]),
        "min_col_fill": (0.3, [0.0, 1.0]),
        "max_junk_ratio": (0.2, [0.0, 1.0]),
        "max_eq_ratio": (0.15, [0.0, 1.0]),      # 수식 표는 PNG로만
        "scan_cell_min_conf": (80, [0, 100]),    # 스캔 셀 합의 신뢰도
    },
}


def _load_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        import tomllib

        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as e:                       # 문법 오류 등 — 전부 기본값으로
        print(f"[설정] '{CONFIG_PATH.name}'을 읽지 못해 기본값으로 실행합니다: {e}",
              file=sys.stderr)
        return {}


def _coerce(section: str, key: str, raw, default, bounds):
    """형·범위를 검사해 통과하면 값을, 아니면 기본값을 돌려준다."""
    if isinstance(default, str):
        if isinstance(raw, str) and raw.strip():
            return raw
    elif isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
    elif isinstance(default, int):
        if isinstance(raw, int) and not isinstance(raw, bool):
            if bounds is None or bounds[0] <= raw <= bounds[1]:
                return raw
    elif isinstance(default, float):
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if bounds is None or bounds[0] <= raw <= bounds[1]:
                return float(raw)
    print(f"[설정] [{section}] {key} 값이 올바르지 않아 기본값 {default!r}을 씁니다"
          f" (받은 값: {raw!r})", file=sys.stderr)
    return default


def _build() -> dict[str, dict]:
    data = _load_file()
    out: dict[str, dict] = {}
    for section, items in DEFAULTS.items():
        given = data.get(section) or {}
        if not isinstance(given, dict):
            given = {}
        out[section] = {
            key: (_coerce(section, key, given[key], default, bounds)
                  if key in given else default)
            for key, (default, bounds) in items.items()
        }
    return out


_CFG = _build()


def get(section: str, key: str):
    """조정값 하나를 읽는다(없는 이름이면 즉시 오류 — 오타를 조용히 넘기지 않는다)."""
    return _CFG[section][key]
