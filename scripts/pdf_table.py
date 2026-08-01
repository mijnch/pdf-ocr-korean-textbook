"""표(TABLE) 구조 추출 모듈 — 괘선 격자 기반, 추가 모델 없음.

DocYolo가 TABLE로 분류한 영역의 크롭에서:
  1) 수평 괘선(잉크가 폭의 55% 이상인 가로줄)으로 행 밴드를 얻고,
  2) 수직 괘선이 있으면 그것으로, 없으면 백색 세로 홈(잉크 공백 골)으로
     열 밴드를 얻어,
  3) 셀 사각형마다 호출측이 주입한 cell_text_fn(크롭 좌표 박스)->str 로
     텍스트를 채워 Markdown 표를 만든다.
셀 텍스트 추출은 호출측 책임이다 — 내장 텍스트층 책은 pdfium의
get_text_bounded(정밀), 스캔책은 셀별 Tesseract를 쓴다. 격자가 표답지
않으면(행<2, 열<2, 채움 부족) None을 돌려주고 호출측은 기존대로 그림만 남긴다.
"""

from __future__ import annotations

import re
from typing import Callable

# 격자 판정 파라미터 (200dpi 픽셀 기준)
import tuning

INK_THR = tuning.get("table", "ink_thr")        # 교과서 괘선은 회색(150이면 놓침)
H_LINE_INK = tuning.get("table", "h_line_ink")  # 가로 괘선: 폭 대비 잉크 비율
V_LINE_INK = tuning.get("table", "v_line_ink")  # 세로 괘선: 높이 대비 잉크 비율
MIN_ROW_H = 8          # 행 밴드 최소 높이
MIN_COL_W = 12         # 열 밴드 최소 폭
GUTTER_INK = 0.015     # 백색 홈: 잉크 비율이 이 이하인 열
MIN_GUTTER_W = 6       # 백색 홈 최소 폭
MAX_ROWS = 40
MAX_COLS = 8
MIN_FILL = tuning.get("table", "min_fill")              # 내용 있는 셀 비율 하한
MAX_CELL_CHARS = tuning.get("table", "max_cell_chars")  # 초과 시 행 분할 실패로 판정
MIN_COL_FILL = tuning.get("table", "min_col_fill")      # 유령 열 병합 기준
MAX_JUNK_RATIO = tuning.get("table", "max_junk_ratio")  # 깨진 셀 비율 상한
MAX_EQ_RATIO = tuning.get("table", "max_eq_ratio")      # 수식 표는 PNG로만
# 한글 교재 표에 나올 수 없는 문자 — 수식 셀의 깨진 텍스트층 신호
_JUNK = re.compile(r"[一-鿿（）☆□◇◎]")

Box = tuple[int, int, int, int]


def _runs(mask) -> list[tuple[int, int]]:
    """불리언 1차원 배열에서 연속 True 구간 [(시작, 끝+1)...]"""
    runs = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def _gutter_cols(white_mask, w: int) -> list[tuple[int, int]]:
    """세로 백색 홈 마스크에서 열 밴드를 만든다(홈 중앙을 경계로)."""
    gutters = [(g0, g1) for g0, g1 in _runs(white_mask)
               if g1 - g0 >= MIN_GUTTER_W and g0 > 0 and g1 < w]
    bounds = [0] + [(g0 + g1) // 2 for g0, g1 in gutters] + [w]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
            if bounds[i + 1] - bounds[i] >= MIN_COL_W]


def detect_grid(crop) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    """크롭에서 (행 밴드, 열 밴드)를 찾는다. 표답지 않으면 None."""
    import numpy as np

    a = np.asarray(crop.convert("L")) < INK_THR  # 잉크 마스크
    h, w = a.shape
    if h < 40 or w < 60:
        return None

    # 1) 행 밴드: 수평 괘선이 충분하면 괘선으로, 아니면(헤더 밑줄만 있는
    #    관행 표) 괘선 사이 내부를 가로 백색 골로 나눈다.
    h_line = a.mean(axis=1) > H_LINE_INK
    h_runs = _runs(h_line)
    if len(h_runs) < 2:  # 최소한 상단·하단 테두리는 있어야 표로 본다
        return None
    edges = [(r0 + r1) // 2 for r0, r1 in h_runs]
    rows = [(edges[i] + 2, edges[i + 1] - 2)
            for i in range(len(edges) - 1)
            if edges[i + 1] - edges[i] >= MIN_ROW_H]
    if len(rows) < 3:
        # 여백 기반 행 분할: 테두리 안쪽에서 잉크 있는 가로 밴드들을 행으로
        top, bot = edges[0] + 2, edges[-1] - 2
        if bot - top < 3 * MIN_ROW_H:
            return None
        band_ink = a[top:bot, :].mean(axis=1) > 0.005
        bands = [(top + b0, top + b1) for b0, b1 in _runs(band_ink)
                 if b1 - b0 >= MIN_ROW_H]
        if len(bands) >= 3:
            rows = bands
    if not 3 <= len(rows) <= MAX_ROWS:
        return None

    # 2) 열 밴드: 수직 괘선 우선, 없으면 백색 홈
    interior = a[rows[0][0]:rows[-1][1], :]
    v_line = interior.mean(axis=0) > V_LINE_INK
    v_runs = _runs(v_line)
    if len(v_runs) >= 3:
        vedges = [(r0 + r1) // 2 for r0, r1 in v_runs]
        cols = [(vedges[i] + 2, vedges[i + 1] - 2)
                for i in range(len(vedges) - 1)
                if vedges[i + 1] - vedges[i] >= MIN_COL_W]
    else:
        cols = _gutter_cols(interior.mean(axis=0) <= GUTTER_INK, w)
        if not 2 <= len(cols) <= MAX_COLS:
            # 낱말 사이 공백까지 홈으로 세면 열이 폭주한다(실측: 전기회로 p848에서
            # 21열 → 탈락). 진짜 열 구분자는 '모든 행 밴드에서 완전히 빈 세로줄'
            # 이므로, 행 밴드 화소만 모아 그 기준으로 다시 나눠 본다. 위 방식이
            # 이미 성사된 표는 건드리지 않는다(추가 기회일 뿐 — 회귀 없음).
            import numpy as _np

            band = _np.concatenate([a[r0:r1] for r0, r1 in rows], axis=0)
            strict = _gutter_cols(~band.any(axis=0), w)
            if 2 <= len(strict) <= MAX_COLS:
                cols = strict
    if not 2 <= len(cols) <= MAX_COLS:
        return None
    return rows, cols


def extract(crop, cell_text_fn: Callable[[Box], str]) -> str | None:
    """크롭에서 Markdown 표를 만든다. 표답지 않으면 None.

    cell_text_fn은 크롭 좌표계의 셀 박스를 받아 텍스트를 돌려준다.
    """
    grid = detect_grid(crop)
    if grid is None:
        return None
    rows, cols = grid
    cells = []
    junked = 0
    eq_cells = 0
    for ry0, ry1 in rows:
        line = []
        for cx0, cx1 in cols:
            txt = " ".join(cell_text_fn((cx0, ry0, cx1, ry1)).split())
            if "=" in txt:  # 잡음 소거 전(원시 기준)에 세야 수식 표가 안 숨는다
                eq_cells += 1
            if _JUNK.search(txt):  # 깨진 셀은 비우고 개수만 센다
                txt = ""
                junked += 1
            line.append(txt.replace("|", r"\|"))
        cells.append(line)
    n_cells = len(rows) * len(cols)
    if junked / n_cells > MAX_JUNK_RATIO:
        return None
    # 수식 표(셀에 등호가 흔함)는 텍스트층/셀 OCR로 정확히 못 옮긴다 — 포기
    if eq_cells >= 2 or eq_cells / n_cells > MAX_EQ_RATIO:
        return None
    # 유령 열 병합: 채움이 낮은 열(이름칸 속 여백을 홈으로 오인한 빈 열,
    # 줄바꿈 조각 열)은 내용을 왼쪽의 실제 열에 이어 붙인다.
    n = len(cols)
    col_fill = [sum(1 for line in cells if line[ci]) / len(cells) for ci in range(n)]
    keep = [ci for ci in range(n) if col_fill[ci] >= MIN_COL_FILL]
    if len(keep) < 2:
        return None
    merged = []
    for line in cells:
        out = []
        pending = ""
        for ci in range(n):
            frag = line[ci]
            if ci in keep:
                out.append((pending + " " + frag).strip() if pending else frag)
                pending = ""
            elif frag:
                if out:
                    out[-1] = (out[-1] + " " + frag).strip()
                else:
                    pending = (pending + " " + frag).strip()
        if pending and out:
            out[0] = (pending + " " + out[0]).strip()
        merged.append(out)
    cells = merged
    # 병합 후에도 완전히 빈 행이 있으면 잡음 소거로 무너진 표다 — 포기
    if any(all(not c for c in line) for line in cells):
        return None
    # 빈 셀이 섞인 행이 1/4을 넘으면 격자와 실제 배치가 어긋난 표(2벌 병렬
    # 목록 등) — 이름-값 결속이 틀어진 표는 유해하므로 포기
    holey = sum(1 for line in cells if any(not c for c in line))
    if holey / len(cells) > 0.25:
        return None
    filled = sum(1 for line in cells for c in line if c)
    if filled / (len(cells) * len(keep)) < MIN_FILL:
        return None
    # 구조 건전성: 셀이 문단처럼 길면 행 분할 실패(뭉개짐)로 보고 포기하고,
    # 채워진 셀이 2개 이상인 행이 과반이어야 표라고 본다(줄바꿈 조각 행 허용).
    if any(len(c) > MAX_CELL_CHARS for line in cells for c in line):
        return None
    rows_ok = sum(1 for line in cells if sum(1 for c in line if c) >= 2)
    if rows_ok / len(cells) < 0.6:
        return None
    header = "| " + " | ".join(cells[0]) + " |"
    sep = "|" + "|".join([" --- "] * len(keep)) + "|"
    body = ["| " + " | ".join(line) + " |" for line in cells[1:]]
    return "\n".join([header, sep] + body)
