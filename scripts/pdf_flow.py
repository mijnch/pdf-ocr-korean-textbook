"""영역 분류와 읽기 순서 — 레이아웃 영역을 가르고, 읽는 순서로 줄 세우고, 줄을 배정한다.

색 상자(예제 상자)·캡션·수식 번호 라벨을 판정하고, 단(칼럼) 경계를 좌표로 정해
읽기 순서를 만든 뒤, OCR·텍스트층 줄을 영역에 배정해 문장 속 수식을 제자리에 끼운다.

pdf_ocr에서 떼어 낸 모듈이다 — pdf_ocr가 이름을 재수출하므로 pdf_ocr.X 호출은 그대로 된다.
"""

from __future__ import annotations

import re

import pdf_text
import tuning
from pdf_text import (
    _WORDISH,
    clean_text,
    join_lines,
    _squash,
)


CAPTION_MAX_CHARS = tuning.get("layout", "caption_max_chars")      # 캡션 최대 길이


# ─────────────────────────── 영역 분류 ───────────────────────────

CALLOUT_COLOR_RATIO = tuning.get("layout", "callout_color_ratio")  # 색 박스 판정
# 색 상자 안 글이 '망가진 티'를 내는 표식 — 한글 사이에 낀 3자 이상 영숫자 덩어리.
# 정상 콜아웃의 'RC'(2자)·'7.9'는 걸리지 않고, 오독 '[ize 2810]'은 걸린다.
_CALLOUT_JUNK = re.compile(r"[A-Za-z0-9]{3,}")
CALLOUT_REDO_GAIN = 1.15   # 크롭 재인식이 이만큼 더 많이 읽어야 교체한다


def _region_pixels(page_image, region: dict, size: int) -> list:
    """영역을 size×size로 줄인 RGB 픽셀 목록. 한 픽셀도 안 되는 영역이면 빈 목록.

    고해상 판독의 좌표를 기준 공간으로 나누면 1픽셀 미만 높이의 줄 상자가 생긴다
    (실측 전자기학 p176) — 그대로 줄이면 PIL이 예외를 내 쪽 전체가 실패한다.
    상자는 쪽 경계로 자른다: 텍스트층 글자 상자가 망가진 PDF는 줄 좌표가 쪽 밖
    수만 픽셀로 튀고(실측 강의록: 51억 화소 크롭), PIL이 압축 폭탄으로 보고 막는다.
    """
    x0, y0 = max(0, round(region["x0"])), max(0, round(region["y0"]))
    x1 = min(page_image.width, round(region["x1"]))
    y1 = min(page_image.height, round(region["y1"]))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return []
    crop = page_image.crop((x0, y0, x1, y1))
    return list(crop.convert("RGB").resize((size, size)).getdata())


def colored_ratio(page_image, region: dict) -> float:
    """영역 배경의 유색(채도 있는) 픽셀 비율. 흰/검/회색이면 0에 가깝다.

    색칠된 강조·예제 박스(콜아웃)를 일반 본문과 구분하는 데 쓴다.
    """
    pix = _region_pixels(page_image, region, 40)
    colored = sum(1 for r, g, b in pix if max(r, g, b) - min(r, g, b) > 40)
    return colored / len(pix) if pix else 0.0


def tinted_ratio(page_image, region: dict) -> float:
    """영역 배경이 '밝지만 희지 않은' 픽셀의 비율 — 옅은 색 상자를 가려낸다.

    colored_ratio(채도 40 초과)는 진한 색만 잡는다. 교재의 예제 상자는 아주
    옅은 하늘색인 경우가 많아 그 문턱을 넘지 못한다(전자회로 p500 상자:
    채도 기준 3.2%, 이 기준 59.9%). 흰 바탕의 도표는 0~5%다(실측).
    """
    pix = _region_pixels(page_image, region, 48)
    tint = sum(1 for r, g, b in pix
               if min(r, g, b) > 170 and max(r, g, b) - min(r, g, b) > 6)
    return tint / len(pix) if pix else 0.0


def is_callout(region: dict, page_image) -> bool:
    """색칠된 강조/예제 박스인지 판정한다(소제목 TITLE 제외).

    실질 글자(한글·영문)가 너무 적은 영역(번호 '25.', 한 글자, 스캔 잡음 등)은
    콜아웃으로 만들지 않는다 — 색만 보고 '> [참고]'를 지어내는 것을 막는다.
    """
    if region.get("type") == "TITLE":
        return False
    if len(_WORDISH.findall(region.get("text", ""))) < 4:
        return False
    return colored_ratio(page_image, region) > CALLOUT_COLOR_RATIO


def is_eq_label(region: dict, page_w: int) -> bool:
    """영역이 우측 여백의 수식 번호('(2-33)' 등)인지 판정한다."""
    width = region["x1"] - region["x0"]
    center = (region["x0"] + region["x1"]) / 2
    return width < 0.15 * page_w and center > 0.78 * page_w


def _overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    """두 구간의 겹침을 짧은 쪽 길이로 나눈 비율."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    return inter / max(1.0, min(a1 - a0, b1 - b0))


# 그림 캡션 표지(이 말로 시작하면 캡션으로 본다). OCR 변형('그럼')도 포함.
_CAPTION_RE = re.compile(r"^\s*(?:그림|그럼|\[?그림|표|Fig\.?|Figure|Table|사진|도표)\b", re.I)
# 문제 번호·항목 표지로 시작하는 글(연습문제 본문) — 캡션이 아니다.
_PROBLEM_RE = re.compile(r"^\s*(?:\d+\.\d+|\d+\s|\[\d|[□O◯▸•])")
# 캡션 '라벨'로 시작하는 글: 표지 + 번호로 시작하고 번호 뒤에 조사가 붙지 않는다.
# '그림 1.28 문제 1.17.'은 라벨이지만 '그림 1.28은 5개의 소자를…'은 본문 참조다 —
# 이 경계가 없으면 본문 문장이 캡션으로 둔갑한다(실측: p53의 문제 1.17 문장).
# 라벨로 시작하는 글에만 넓은 캡션 띠를 허용한다.
_CAPTION_REF = re.compile(
    r"^\s*(?:그림|그럼|기림|표|Fig\.?|Figure|Table|사진|도표)"
    r"\s*[0-9]+[.．\-][0-9]+[가-힣]", re.I)
# 완결된 한국어 문장의 끝 — 표지 없는 글이 이렇게 끝나면 본문이다.
_SENTENCE_END = re.compile(r"(?:다|라|자|요|오)[.。]\s*$|[?？]\s*$")
_HANGUL_CH = re.compile(r"[가-힣]")


def is_caption_like(text: str) -> bool:
    """캡션 조각인지 판정한다.

    - 캡션 표지(그림/표/Fig…)로 시작하면 길이 무관 캡션.
    - 문제 번호(4.18 등)·항목 표지로 시작하면 본문이므로 캡션 아님.
    - 표지가 없으면 짧은 설명 조각(≤45자)만 캡션으로 보되, 완결된 문장은
      제외한다 — 예제 문제 문장이 캡션으로 둔갑하던 경로다(실측: 대학물리
      p55에서 '전투기가 63 m/s의 속력으로 항공모함에 착륙하려고 한다.'가
      그림 2.11의 캡션이 되어, 그림 2.11을 물으면 엉뚱한 답이 나왔다).
      표지가 붙은 캡션은 문장으로 끝나도 그대로 둔다 — 원본 캡션이 실제로
      '…잴 수 있다.'처럼 문장인 경우가 많다.
    """
    t = text.strip()
    if not t:
        return False
    if _CAPTION_RE.match(t):
        return True
    if _PROBLEM_RE.match(t):
        return False
    if _SENTENCE_END.search(t):
        return False
    # 표지도 없고 한글도 거의 없는 조각은 OCR 잡음이다('^ 1.18.', '.18 1.29에서').
    # 캡션으로 삼으면 그 그림의 진짜 캡션 회수까지 막는다(실측 p53).
    if len(_HANGUL_CH.findall(t)) < 2:
        return False
    return len(t) <= CAPTION_MAX_CHARS


GUTTER_LO, GUTTER_HI = 0.25, 0.75   # 거터가 있을 수 있는 x 구간(페이지 폭 대비)
GUTTER_PAD = 0.015                  # 이만큼은 걸쳐도 '가로지른다'고 보지 않는다
GUTTER_MIN_SIDE = 2                 # 양쪽에 각각 이만큼의 영역이 있어야 한다
GUTTER_MIN_WIDTH = 0.15             # 양쪽 덩어리가 각각 이만큼은 넓어야 한다


def geometric_gutter(regions: list[dict], page_w: int) -> float | None:
    """좌표만 보고 칼럼 경계를 찾는다 — 라벨을 전혀 쓰지 않는다.

    조건: 그 x를 가로지르는 본문 영역이 하나도 없고, 양쪽에 영역이 둘 이상,
    양쪽 덩어리가 각각 페이지 폭의 15% 이상. 후보가 여럿이면 양쪽 개수가
    가장 고르게 갈리는 x를 고른다.

    단일 칼럼 페이지에서는 본문 문단이 어느 x든 가로지르므로 자연히 None이
    나온다(실측: 8권 표본 16쪽 중 단단 12쪽 전부 None). 여백 주석·그림
    칼럼이 있는 쪽에서만 경계가 잡힌다 — 대학물리 p647 x=458,
    전기회로이론 p314 x=409, 전자회로 p293 x=830.
    """
    texts = [r for r in regions if r.get("kind", "text") == "text"] or regions
    if len(texts) < 2 * GUTTER_MIN_SIDE:
        return None
    pad = GUTTER_PAD * page_w
    best: tuple[int, float] | None = None
    for x in range(int(GUTTER_LO * page_w), int(GUTTER_HI * page_w), 4):
        if any(r["x0"] + pad < x < r["x1"] - pad for r in texts):
            continue
        left = [r for r in texts if (r["x0"] + r["x1"]) / 2 < x]
        right = [r for r in texts if (r["x0"] + r["x1"]) / 2 >= x]
        if len(left) < GUTTER_MIN_SIDE or len(right) < GUTTER_MIN_SIDE:
            continue
        lw = max(r["x1"] for r in left) - min(r["x0"] for r in left)
        rw = max(r["x1"] for r in right) - min(r["x0"] for r in right)
        if lw < GUTTER_MIN_WIDTH * page_w or rw < GUTTER_MIN_WIDTH * page_w:
            continue
        bal = min(len(left), len(right))
        if best is None or bal > best[0]:
            best = (bal, float(x))
    return best[1] if best else None


def clean_column_bands(regions: list[dict], page_w: int) -> dict[int, tuple[float, float]]:
    """칼럼별 x 범위 — 단, 제 칼럼 중앙값에서 크게 벗어난 영역은 빼고 구한다.

    레이아웃 모델이 오른쪽 단의 문단에 왼쪽 칼럼 번호를 붙이는 일이 있다.
    그 한 영역 때문에 밴드가 페이지를 뒤덮으면 거터가 사라져 읽기 순서가
    행 단위로 무너진다. 판정 기준은 pdf_text.detect_columns와 같은 값을 쓴다.
    """
    import statistics

    cols: dict[int, list[dict]] = {}
    for r in regions:
        c = r.get("col", 1)
        if c >= 1:
            cols.setdefault(c, []).append(r)
    bands: dict[int, tuple[float, float]] = {}
    for c, rs in cols.items():
        med = statistics.median((r["x0"] + r["x1"]) / 2 for r in rs)
        keep = [r for r in rs
                if abs((r["x0"] + r["x1"]) / 2 - med)
                <= pdf_text.COL_OUTLIER_RATIO * page_w] or rs
        bands[c] = (min(r["x0"] for r in keep), max(r["x1"] for r in keep))
    return bands


def infer_column(cx: float, bands: dict[int, tuple[float, float]]) -> int:
    """중심 x좌표가 속하는 칼럼 번호를 추정한다(레이아웃에 없는 수식용)."""
    if not bands:
        return 1
    inside = [c for c, (x0, x1) in bands.items() if x0 <= cx <= x1]
    if inside:
        return min(inside)
    # 어느 칼럼에도 안 들어가면 중심이 가장 가까운 칼럼
    return min(bands, key=lambda c: abs(cx - (bands[c][0] + bands[c][1]) / 2))


def _row_order(blocks: list[dict]) -> list[dict]:
    """세로로 겹치는 블록들을 한 행으로 묶고 행 안에서 좌→우로 읽는다.

    y0만으로 정렬하면 같은 줄에 나란히 놓인 두 항목(공식표의 좌·우 항, 나란한
    그림 등)이 몇 픽셀 y 차이로 뒤바뀐다 — 실측: 대학수학 표 5.4가
    (2)(1)(4)(3) 순으로 나왔다. 세로 범위가 겹치지 않는 통상 문단은 각자
    한 행이 되어 순서가 그대로 유지된다.
    """
    items = sorted(blocks, key=lambda b: (b["y0"] + b.get("y1", b["y0"])) / 2)
    rows: list[list[dict]] = []
    for it in items:
        cy = (it["y0"] + it.get("y1", it["y0"])) / 2
        if rows:
            ry0 = min(x["y0"] for x in rows[-1])
            ry1 = max(x.get("y1", x["y0"]) for x in rows[-1])
            if ry0 <= cy <= ry1:      # 앞 행과 세로로 겹치면 같은 행
                rows[-1].append(it)
                continue
        rows.append([it])
    out: list[dict] = []
    for row in rows:
        row.sort(key=lambda b: b["x0"])
        out.extend(row)
    return out


def _by_col_then_row(blocks: list[dict]) -> list[dict]:
    """칼럼 번호 순으로 묶고, 각 칼럼 안에서는 행 단위 좌→우로 읽는다."""
    out: list[dict] = []
    for col in sorted({b["col"] for b in blocks}):
        out.extend(_row_order([b for b in blocks if b["col"] == col]))
    return out


def order_flow(flow: list[dict], layout_texts: list[dict], page_w: int) -> list[dict]:
    """읽기 순서 결정: 기하학적 칼럼 정규화 + 세그먼트별 좌→우 읽기.

    DocYolo의 col 라벨은 본문·여백이 섞인 혼합 폭 페이지에서 자의적일 수 있어
    (예: 예제 블록 둘이 서로 다른 칼럼 번호를 받아 순서가 뒤바뀜) 그대로 믿지
    않는다. 라벨은 거터(칼럼 사이 경계 x) 추정에만 쓰고, 각 블록의 소속
    (좌/우/전폭)은 좌표로 재판정한다. 전폭 블록은 세로 구분자가 되어 페이지를
    세그먼트로 나누고, 세그먼트 안에서만 좌단을 다 읽은 뒤 우단을 읽는다.
    진짜 2단 페이지는 전폭 블록이 없어 기존(칼럼→y) 순서가 그대로 유지되고,
    단일 칼럼 페이지는 거터가 없어 순수 위→아래가 된다.
    """
    # 밴드는 '제 칼럼에서 멀리 떨어진 오라벨 영역'을 뺀 뒤 구한다 — 그러지 않으면
    # 오른쪽 문단 하나에 왼쪽 칼럼 번호가 붙은 것만으로 밴드가 페이지를 뒤덮어
    # 거터가 사라지고, 2단 페이지가 행 단위 좌→우로 읽혀 좌·우 문제가 번갈아
    # 나온다(실측 전자회로 p293: 5.48→5.52→5.49→5.53 순).
    bands = sorted(clean_column_bands(layout_texts, page_w).values())
    gutter = geometric_gutter(layout_texts, page_w)
    if gutter is None and len(bands) == 2 and bands[1][0] - bands[0][1] > -0.05 * page_w:
        cand = (bands[0][1] + bands[1][0]) / 2
        # 라벨에서 나온 거터는 페이지 한가운데 언저리일 때만 받는다. 수식 번호
        # 라벨 두어 개가 오른쪽 끝에서 제 칼럼 번호를 받으면 거터가 폭의 89%
        # 지점에 잡히고(실측 대학물리 p647: x=1494/1674), 그러면 페이지 전체가
        # 한 칼럼이 되어 행 단위로 읽힌다 — 왼쪽 여백 상자가 본문 문단 사이로
        # 끼어드는 원인이다. 기하 판정이 같은 쪽에서 x=458을 정확히 찾는다.
        if GUTTER_LO * page_w <= cand <= GUTTER_HI * page_w:
            gutter = cand
    if gutter is None:
        if len(bands) >= 3:  # 3단 이상(희귀): 라벨 순서를 그대로 신뢰
            for b in flow:
                if b["col"] < 1:
                    b["col"] = 0
            return _by_col_then_row(flow)
        return _row_order(flow)

    wide = 0.12 * page_w
    for b in flow:
        if b["col"] < 1:                       # 머리말 라벨 → 위치(y) 그대로 배치
            b["col"] = 0
        elif gutter - b["x0"] > wide and b["x1"] - gutter > wide:
            b["col"] = 0                       # 전폭 블록 → 세로 구분자
        else:
            b["col"] = 1 if (b["x0"] + b["x1"]) / 2 < gutter else 2

    ordered: list[dict] = []
    seg: list[dict] = []

    def flush_seg():
        ordered.extend(_by_col_then_row(seg))
        seg.clear()

    for b in sorted(flow, key=lambda b: b["y0"]):
        if b["col"] == 0:
            flush_seg()
            ordered.append(b)
        else:
            seg.append(b)
    flush_seg()
    return ordered


# ─────────────────────────── 줄 → 영역 배정 ───────────────────────────

def assign_lines(lines: list[dict], regions: list[dict]) -> None:
    """각 본문 줄을 중심점이 들어가는 텍스트 영역에 배정한다(region['lines']).

    어떤 영역에도 안 들어가는 줄은 자기 자신을 영역으로 갖는 떠돌이 줄이 되어
    누락되지 않는다(반환 목록에 추가).
    """
    for r in regions:
        r.setdefault("lines", [])
    for ln in lines:
        cx = (ln["x0"] + ln["x1"]) / 2
        cy = (ln["y0"] + ln["y1"]) / 2
        for r in regions:
            if r["x0"] <= cx <= r["x1"] and r["y0"] <= cy <= r["y1"]:
                r["lines"].append(ln)
                break
        else:
            regions.append({  # 떠돌이 줄 → 1줄짜리 본문 영역
                "kind": "text", "type": "STRAY",
                "x0": ln["x0"], "y0": ln["y0"], "x1": ln["x1"], "y1": ln["y1"],
                "lines": [ln],
            })


# 마스킹된 인라인 수식 자리에 Tesseract가 남기는 큰 공백 틈(내부 3칸 이상).
_MASK_GAP = re.compile(r"(?<=\S)\s{3,}(?=\S)")


def _insert_at(text: str, cuts: list[tuple[int, str]]) -> str:
    """text의 (문자 인덱스, 삽입 문자열) 목록을 왼쪽부터 반영해 합친다."""
    out, prev = [], 0
    for idx, s in sorted(cuts):
        idx = max(prev, min(idx, len(text)))
        seg = text[prev:idx].strip()
        if seg:
            out.append(seg)
        out.append(s)
        prev = idx
    tail = text[prev:].strip()
    if tail:
        out.append(tail)
    return " ".join(out).strip()


def _weave_by_words(text: str, words: list, formulas: list[dict]) -> str | None:
    """단어별 x좌표로 수식의 삽입 지점을 계산한다. 못 하면 None.

    words는 [(단어 글자수, x0, x1)]. 수식 왼쪽에 완전히 놓인 단어들의 글자 수를
    세면 그 수식이 줄의 몇 번째 글자 뒤에 오는지 알 수 있다. tsv 단어 글자 총합과
    txt 글자 수가 다를 수 있으므로(같은 줄의 다른 판독) 비율로 환산한다.
    """
    words = [w for w in words if w[0] > 0]
    if not words:
        return None
    total = sum(n for n, _a, _b in words)
    stripped = _squash(text)
    if total <= 0 or not stripped:
        return None
    cuts: list[tuple[int, str]] = []
    for f in formulas:
        fx = f["x0"]
        # 수식보다 확실히 왼쪽에서 끝나는 단어들의 글자 수(경계가 겹치면 중심으로 판정)
        n_left = sum(n for n, a, b in words if b <= fx or (a < fx and (a + b) / 2 < fx))
        n_scaled = round(n_left * len(stripped) / total)
        # 공백 제외 n_scaled번째 글자 뒤의 원문 인덱스를 찾는다
        seen, idx = 0, len(text)
        for i, ch in enumerate(text):
            if seen >= n_scaled:
                idx = i
                break
            if not ch.isspace():
                seen += 1
        # tsv 글자 수와 txt 글자 수가 다르면(같은 줄의 다른 판독) 위 환산에 ±몇 글자
        # 오차가 생겨 낱말 중간을 가를 수 있다 — 가까운 공백으로 스냅해 낱말 경계에
        # 넣는다(마스킹 틈은 공백이므로 대개 정확히 그 자리로 붙는다).
        best, bestd = idx, None
        for j in range(max(0, idx - 4), min(len(text), idx + 5)):
            if text[j].isspace():
                d = abs(j - idx)
                if bestd is None or d < bestd:
                    best, bestd = j, d
        cuts.append((best, f"${f['text']}$"))
    return _insert_at(text, cuts)


def _weave_line(text: str, formulas: list[dict], words: list | None = None) -> str:
    """한 본문 줄에 인라인 수식을 제자리로 끼워 넣는다.

    1순위 — 단어 좌표(tsv): 수식 x가 어느 단어들 뒤인지 세어 정확한 지점에 넣는다.
    2순위 — 마스킹 공백 틈: 수식 영역은 OCR 전에 흰색으로 가려져 줄에 큰 공백 틈이
      남는다. 틈 수가 수식 수와 같으면 왼→오로 채운다(단어 좌표가 없는 보충 줄 등).
    3순위 — 줄 끝에 붙임(안전 폴백).
    검토단 C-4: 이 처리 전에는 수식이 전부 줄 끝으로 밀려 문장이 조사만 남았다.
    """
    fs_sorted = sorted(formulas, key=lambda f: f["x0"])
    fstr = [f"${f['text']}$" for f in fs_sorted]
    if words:
        woven = _weave_by_words(text, words, fs_sorted)
        if woven:
            return woven
    gaps = list(_MASK_GAP.finditer(text))
    if len(gaps) == len(fstr):
        return _insert_at(text, [(m.start(), s) for m, s in zip(gaps, fstr)])
    return " ".join([text] + fstr).strip()


def assemble_region_text(region: dict, embeddings: list[dict],
                         consumed: set[int] | None = None) -> str:
    """영역에 배정된 본문 줄과 (스캔 경로의) 문장 속 수식을 읽기 순서로 합친다.

    영역 안에서만 행(y) 묶음 + 가로(x) 정렬을 하므로, 페이지 전체를 한꺼번에
    정렬할 때 생기던 읽기 순서 뒤섞임이 없다. embeddings는 스캔 경로에서만
    채워지며(내장 경로는 줄에 이미 인라인으로 들어 있음) 영역 안에 중심이
    들어오는 수식을 다룬다. 수식이 어느 본문 줄에 y로 겹치고 그 줄의 x범위 안에
    있으면 그 줄의 제자리(_weave_line)에 끼워 넣고, 아니면 독립 항목으로 둔다.
    consumed에 삽입된 수식의 id를 기록해 겹치는 영역 중복 삽입을 막는다.
    """
    lines: list[dict] = [dict(ln) for ln in region.get("lines", [])]
    region_fs: list[dict] = []
    for f in embeddings:
        if consumed is not None and id(f) in consumed:
            continue
        cx = (f["x0"] + f["x1"]) / 2
        cy = (f["y0"] + f["y1"]) / 2
        if region["x0"] <= cx <= region["x1"] and region["y0"] <= cy <= region["y1"]:
            region_fs.append(f)
            if consumed is not None:
                consumed.add(id(f))

    # 각 수식을 host 본문 줄(y 겹침 + x범위 내)에 배정한다. host가 없으면 독립 항목.
    hosted: dict[int, list[dict]] = {id(l): [] for l in lines}
    items: list[dict] = list(lines)
    for f in region_fs:
        fcx = (f["x0"] + f["x1"]) / 2
        fcy = (f["y0"] + f["y1"]) / 2
        host = None
        for l in lines:
            if l["y0"] <= fcy <= l["y1"] and l["x0"] <= fcx <= l["x1"]:
                if host is None or (l["y1"] - l["y0"]) < (host["y1"] - host["y0"]):
                    host = l
        if host is not None:
            hosted[id(host)].append(f)
        else:
            items.append({**f, "text": f"${f['text']}$"})   # 독립 위치
    for l in lines:
        if hosted[id(l)]:
            l["text"] = _weave_line(l["text"], hosted[id(l)], l.get("words"))

    if not items:
        return ""

    items.sort(key=lambda it: (it["y0"] + it["y1"]) / 2)
    rows: list[list[dict]] = []
    for it in items:
        if rows:
            ry0 = min(x["y0"] for x in rows[-1])
            ry1 = max(x["y1"] for x in rows[-1])
            if ry0 <= (it["y0"] + it["y1"]) / 2 <= ry1:  # 세로로 겹치면 같은 행
                rows[-1].append(it)
                continue
        rows.append([it])

    out = []
    for row in rows:
        row.sort(key=lambda it: it["x0"])
        out.append(" ".join(it["text"] for it in row))
    return clean_text(join_lines(out))
