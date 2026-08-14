"""PDF OCR — 본문·수식·그림을 분리 인식해 읽기 순서대로 Markdown으로 추출.

입력 폴더의 PDF를 읽어, 페이지마다 레이아웃을 분석해 영역별로 최적의 엔진에
보내고 읽기 순서대로 정돈된 '원본이름_OCR.md'를 출력 폴더에 저장한다.

영역별 라우팅:
  - 그림/표  → OCR하지 않고 PNG로 저장하고 ![그림] 링크 + 캡션을 바로 아래 병합
  - 수식      → pix2text(MFD/MFR)로 LaTeX 변환 ($$...$$ / $...$, 우측 수식 번호 부착)
  - 본문/제목 → 내장 텍스트(있으면) 또는 Tesseract(한국어+영어, 다단이면 칼럼별)
  - 색칠된 강조/예제 박스 → 원래 위치에 > [참고] 인용 블록
  - 머리말/쪽번호/워터마크 → 버림. 단, 하단의 * † ‡ 각주는 본문으로 보존

본문 출처는 페이지마다 자동 선택한다:
  - 내장 텍스트 레이어가 있으면 그대로 활용(정확하고 빠름). 단, 깨지기 마련인
    수식 부분은 버리고 새로 인식한 LaTeX로 채운다.
  - 없으면(순수 스캔본) Tesseract로 본문을 인식한다.
  - 본문 속 '$'는 \\$로 이스케이프한다 — 삽입되는 수식 구분자 $와 짝을 이뤄
    본문이 수식으로 렌더링되는 것을 막는다(스캔본에서는 대부분 오인식 잡음).

성능:
  - 모델(레이아웃/수식)은 전체 실행에서 1회만 로드해 재사용한다.
  - Tesseract(외부 프로세스)와 다음 페이지의 레이아웃·수식검출은 현재 페이지의
    수식 인식(MFR)과 겹쳐 실행된다(_TESS_POOL/_PREFETCH_POOL). 페이지 최대 병목은
    책 종류에 따라 다르다(실측): 내장 텍스트본은 MFR, 스캔본은 Tesseract(~90%).
  - 스캔 원본이 기준 해상도(200dpi)보다 높은 페이지는 원본 해상도로 한 번 더
    렌더링해 OCR 입력·수식 크롭·그림 저장에 쓴다(좌표 공간은 200dpi로 통일).
  - 결과는 페이지마다 즉시 파일에 기록한다(중단 안전).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

import pdf_chapters
import pdf_table
import tuning

# 분해된 하위 모듈 — 공개 이름은 여기서 재수출한다(기존 호출부·테스트 호환).
from pdf_latex import (  # noqa: F401
    _SINGLE_SYMBOL,
    clean_latex,
)
import pdf_text  # noqa: F401
from pdf_text import (  # noqa: F401
    HIRES_MAX_DPI,
    MASK_MARGIN,
    MIN_LINE_CONF,
    PSM_CANDIDATES,
    RENDER_DPI,
    RESCUE_MIN_CONF,
    RESCUE_MIN_WORDISH,
    SCAN_CELL_MIN_CONF,
    SCAN_CELL_SCALES,
    TESS_LANG,
    _MATH_SPAN,
    _WATERMARK_RE,
    _WORDISH,
    clean_text,
    detect_columns,
    detect_rotation,
    load_tesseract_result,
    make_scan_cell_fn,
    merge_rescue_lines,
    native_scan_dpi,
    ocr_region_text,
    start_tesseract,
    tesseract_lines,
)

from common import (
    exit_with_message,
    feature_dirs,
    find_pdfs,
    find_skipped_subfolders,
    find_stale_outputs,
    human_size,
    setup_external_tools,
    tmp_root,
)

FEATURE = "PDF OCR"

FIG_MARGIN = tuning.get("figure", "margin")        # 그림을 잘라낼 때 사방 여백(픽셀)
FIG_MAX_PX = tuning.get("figure", "max_px")        # 저장 그림의 최장변 상한
FIG_COLORS = tuning.get("figure", "colors")        # 양자화 색 수(용량 약 절반)
MIN_SANE_CHAR_RATIO = tuning.get("layout", "min_sane_char_ratio")  # 정상 글자율 하한
PARA_JOIN_MARGIN = tuning.get("layout", "para_join_margin")        # 문단 병합 여유
HEADER_BAND_RATIO = tuning.get("layout", "header_band_ratio")      # 머리말 띠
# 상단 띠 안에 있어도 이보다 긴 글은 머리말로 보지 않는다 — 상단 여백이 좁은
# 자료의 첫 줄(장·절 제목)이 매 쪽 잘려 나가는 것을 막는다. 5권 실측 머리말은
# 최장 38자(장제목 + 쪽번호)이고, 잘려 나가던 첫 줄들은 40자를 넘었다.
HEADER_MAX_CHARS = 60
HEADER_EXT_RATIO = tuning.get("layout", "header_ext_ratio")        # 확장 띠(내용 병행)
FOOT_BAND_RATIO = tuning.get("layout", "foot_band_ratio")          # 내장책 쪽번호 띠
CAPTION_MAX_CHARS = tuning.get("layout", "caption_max_chars")      # 캡션 최대 길이
# Tesseract를 MFR과 겹쳐 돌리는 전용 스레드 풀. 워커 2 — 고해상 페이지의 두
# Tesseract 패스(400dpi 주 + 200dpi 보충)는 서로 의존이 없는데 워커 1이면 직렬로
# 돈다(검토단 4 지적: 스캔본 wall의 ~90%가 Tesseract). 실측 A/B(공학수학1 5쪽,
# 워밍업 제외): 10.66→8.95초/쪽, wall 16% 절감, 한글 3000 동일(정확도 손실 0).
# 두 패스가 MFR·프리페치와 코어를 나눠 써 이론상한(~37%)보다 낮지만 공짜 이득이다.
# 외부 프로세스라 GIL 무관, 두 패스는 이미지 복사본·temp 파일명(page/band vs
# pager/bandr)이 달라 충돌하지 않는다. 다음 페이지 선계산은 별도 풀이 담당한다.
_TESS_POOL = ThreadPoolExecutor(max_workers=2)
# 다음 페이지의 레이아웃+수식검출을 현재 페이지 MFR과 겹치는 선행 스레드
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=1)



# ─────────────────────────── 내장 텍스트 본문 추출 ───────────────────────────

def _is_sane_char(ch: str) -> bool:
    o = ord(ch)
    return (
        ch.isascii()
        or 0xAC00 <= o <= 0xD7A3   # 한글 음절
        or 0x3130 <= o <= 0x318F   # 한글 호환 자모
        or 0x0370 <= o <= 0x03FF   # 그리스 문자 — 물리 본문의 α β γ 줄 보존(검토단)
        or 0x3000 <= o <= 0x303F   # CJK 문장부호
        or 0xFF00 <= o <= 0xFFEF   # 전각 영숫자·문장부호
        or 0x2018 <= o <= 0x201D   # 따옴표
        or ch in "·…—–"
    )


def has_embedded_text(textpage, min_chars: int = 50) -> bool:
    """페이지에 쓸 만한 내장 텍스트 레이어가 있는지 검사한다.

    글자 수만이 아니라 정상 글자 비율도 본다 — 과거 타 도구가 입힌 저품질
    OCR층을 본문으로 신뢰하면 스캔 재인식이 영영 돌지 않으므로, 비율이 낮으면
    스캔 경로로 넘긴다(검토단 지적. 내장 5권 실측 최저 0.875라 여유가 크다).
    """
    n = textpage.count_chars()
    if n < min_chars:
        return False
    text = textpage.get_text_range(0, n)
    visible = [ch for ch in text if not ch.isspace()]
    if len(visible) < min_chars:
        return False
    sane = sum(1 for ch in visible if _is_sane_char(ch))
    return sane / len(visible) >= MIN_SANE_CHAR_RATIO


# ─── 위첨자 복원(내장 텍스트 전용) ───
# 내장 텍스트층은 위첨자를 '작은 글꼴의 보통 글자'로만 표현한다 — pdfium의 문자
# 상자는 세로 위치를 구분해 주지 않으므로(밑수와 지수의 bottom/top이 동일) 유일한
# 신호는 글꼴 크기다(실측 대학물리 p298: 밑수 10=6.15pt, 지수 10=4.61pt = 75%).
# 이걸 안 쓰면 '35 × 10^10'이 '35 X 1010'으로 평문화돼 값이 10^7배 틀린다(검토단 C-3).
# 실측 규모: 표 셀 203건 + 내장 본문 199건.
_SUP_RATIO = 0.85            # 이 비율 미만이면 작은 글꼴로 본다
_SUP_CHARS = set("0123456789+-−")
_SUP_MAX_RUN = 3             # 지수는 짧다(10^-19 등) — 긴 런은 본문 크기 변화다
# 밑수는 숫자나 닫는 괄호만 인정한다. pdfium은 위/아래 첨자를 구분해 주지 않으므로
# (문자 상자·원점 모두 밑수와 동일) 문자 밑수는 아래첨자일 확률이 높다 — 실측 표본
# 241건 분류: 숫자 34%·닫는괄호 10%는 전부 정상(10^{5}, (12 A)^{2}), 소문자 45%·
# 대문자 11%는 대부분 아래첨자(v_1을 v^{1}로, C_2를 C^{2}로 오인). 과학적 표기의
# 10^n(값이 10^7배 틀리던 원인)만 확실히 잡고 나머지는 건드리지 않는다.
_SUP_BASE_OK = set("0123456789)]}")
# 지수 앞에 이 글자가 큰 글꼴로 놓여 있으면 과학적 표기가 망가진 것이다.
# 정상이라면 지수의 부호(-)도 작은 글꼴이라 런에 함께 들어온다. 큰 글꼴 부호나
# 따옴표 글리프가 밑수 자리에 있다는 것은 내장 OCR 층이 '10^-3'을 '10-3'·'10“8'
# 처럼 부호를 본문 크기로 잘못 새겼다는 뜻이며, 그 셀의 값은 자릿수가 틀린다.
# 문자 밑수('m2'의 m)는 여기 넣지 않는다 — 정상적인 단위 지수이거나 아래첨자라
# 오염과 구분되지 않는다(실측: 넣으면 멀쩡한 표 3개가 함께 폐기됐다).
_SUP_SEVERED_SIGN = set("-−+*\"'`´“”’‘")
# 숫자를 닮은 글자. 과학적 표기('1.00 X IO3') 안에서 지수의 밑수 자리에 오면
# 내장 OCR 층이 '10'을 'IO'로 잘못 읽은 것이다 — 곱셈 표시가 같은 셀에 있을
# 때만 인정한다(그냥 'm2'의 m 같은 정상 단위 지수와 섞이지 않게).
_SUP_DIGIT_LOOKALIKE = set("OoIlQq")
_SCI_MULT = re.compile(r"[Xx×]\s*$|[Xx×]\s*\S")


def _sup_runs(items: list[tuple[str, float]]) -> list[tuple[int, int, bool]]:
    """작은 글꼴 런을 찾아 [(시작, 끝, 밑수적합)] 목록으로 돌려준다.

    밑수적합=False는 '글꼴 크기는 위첨자라고 말하는데 앞 글자가 밑수로 쓸 수
    없다'는 뜻이다 — 정상적인 아래첨자일 수도 있고, 내장 OCR 층이 밑수를
    문자로 깨뜨린 것일 수도 있다(10을 IO로). 표 폐기 판정이 이 값을 쓴다.
    """
    sizes = [s for ch, s in items if s > 1.0 and not ch.isspace()]
    if len(sizes) < 2:
        return []
    base = sorted(sizes)[len(sizes) // 2]
    if base <= 0:
        return []
    small = [i for i, (ch, s) in enumerate(items)
             if 1.0 < s < base * _SUP_RATIO and ch in _SUP_CHARS]
    runs, i = [], 0
    while i < len(small):
        j = i
        while j + 1 < len(small) and small[j + 1] == small[j] + 1:
            j += 1
        run = small[i:j + 1]
        i = j + 1
        if len(run) > _SUP_MAX_RUN:
            continue
        # 밑수는 바로 앞에 붙어 있어야 한다 — 사이에 공백이 있으면 위첨자가 아니라
        # 별개 토큰이다(실측 오탐: '2 X 10 *' → '2 X ^{1}^{0}', 'ka 20' → 'ka ^{20}').
        prev = items[run[0] - 1][0] if run[0] > 0 else ""
        runs.append((run[0], run[-1], prev in _SUP_BASE_OK))
    return runs


def superscript_marks(items: list[tuple[str, float]]) -> dict[int, tuple[str, str]]:
    """(글자, 글꼴크기) 목록에서 위첨자 런을 찾아 {인덱스: (앞, 뒤)} 표식을 만든다.

    인덱스 기반이라 호출부의 글자 순서·삽입 위치 계산을 흐트러뜨리지 않는다.
    지수 문맥(앞 글자가 숫자·닫는 괄호)일 때만 감싼다 — 각주 번호나
    글꼴이 섞인 제목이 잘못 위첨자가 되는 것을 막는다.
    """
    marks: dict[int, tuple[str, str]] = {}
    for s, e, ok in _sup_runs(items):
        if not ok:
            continue
        marks[s] = ("^{", "")
        marks[e] = (marks.get(e, ("", ""))[0], "}")
        if s == e:
            marks[s] = ("^{", "}")
    return marks


def graft_superscripts(plain: str, sized: list[tuple[str, float]]) -> str:
    """이미 잘 띄어쓰기된 텍스트(plain)에 위첨자 표식만 이식한다.

    간격 규칙을 새로 만들면 숫자 안에 헛공백이 생긴다('1 0^{10}') — pdfium이 준
    plain의 띄어쓰기를 그대로 두고, 같은 순서의 글자 목록(sized)에서 계산한
    위첨자 표식만 해당 글자 자리에 붙인다. 글자열이 어긋나면 plain을 그대로 쓴다.
    """
    marks = superscript_marks(sized)
    if not marks:
        return plain
    seq = [ch for ch, _fs in sized]
    out, j = [], 0
    for ch in plain:
        if ch.isspace():
            out.append(ch)
            continue
        while j < len(seq) and seq[j].isspace():
            j += 1
        if j >= len(seq) or seq[j] != ch:
            return plain                      # 정렬 실패 — 안전하게 원문 유지
        pre, post = marks.get(j, ("", ""))
        out.append(pre + ch + post)
        j += 1
    return "".join(out)


def superscript_severed(plain: str, sized: list[tuple[str, float]]) -> int:
    """지수의 부호가 본문 글꼴로 떨어져 나간 런의 개수 — 표 폐기 판정에 쓴다.

    내장 텍스트층이 저품질 선행 OCR인 책에서는 '10^-3'이 '10-3'·'10“8'처럼
    새겨진다: 지수 숫자는 작은 글꼴인데 부호는 본문 크기라서, 위첨자 런의
    밑수 자리에 부호·따옴표 글리프가 남는다. 그 셀의 값은 자릿수가 틀린다
    (실측: 대학물리 표 26.2 철 온도계수 5.0×10^-3 → 5.0×10^-8, 10만 배).

    밑수가 숫자 닮은 글자('1.00 X IO3')인 경우도 센다 — 같은 셀에 곱셈
    표시가 있을 때만이다. 그 밖의 문자 밑수는 세지 않는다: 'm2'의 m처럼
    정상적인 단위 지수이거나 아래첨자라서 오염과 구분되지 않는다(실측:
    구분 없이 세면 멀쩡한 표 3개가 함께 폐기됐다).

    본문에는 쓰지 않는다 — 문장은 문맥으로 회복되지만 표의 수치는 회복
    수단이 없기 때문이다.
    """
    n = 0
    sci = bool(_SCI_MULT.search(plain))
    for s, _e, ok in _sup_runs(sized):
        if ok:
            continue
        prev = sized[s - 1][0] if s > 0 else ""
        if prev in _SUP_SEVERED_SIGN or (sci and prev in _SUP_DIGIT_LOOKALIKE):
            n += 1
    return n


_SCI_RESTORED = re.compile(r"10\^\{")
_SCI_FLAT = re.compile(r"[Xx×]\s*10[-−]?\d")


def table_superscript_partial(md: str) -> bool:
    """한 표 안에서 지수 복원이 반쪽만 됐는지 — 가장 위험한 형태다.

    셀 단위 신호(글꼴 크기)로는 잡히지 않는 실패가 있다: 글꼴 크기가 지수를
    아예 표시하지 않으면 포기할 런조차 없어 조용히 평문으로 남는다. 그러나
    같은 표의 다른 셀이 '10^{24}'로 제대로 복원됐다면, 평문으로 남은
    'X 1025'는 복원 실패가 확실하다(실측: 대학물리 표 E.2에서 12행 중
    천왕성 8.68×10^25과 달 7.35×10^22 두 셀만 평문으로 남았다).

    AI는 표의 나머지가 맞으니 그 표를 신뢰하게 되므로, 반쪽 복원은 전부
    틀린 표보다 오히려 더 위험하다 — 표째로 폐기하고 PNG를 남긴다.
    """
    return bool(_SCI_RESTORED.search(md) and _SCI_FLAT.search(md))


def char_font_sizes(textpage, n: int) -> list[float]:
    """문자별 글꼴 크기 목록. API가 없거나 실패하면 빈 목록(기능 비활성)."""
    try:
        import pypdfium2.raw as _pr

        return [float(_pr.FPDFText_GetFontSize(textpage.raw, i)) for i in range(n)]
    except Exception:
        return []


def _page_char_index(textpage, s_pt: float, page_h_pt: float):
    """페이지 전체 글자를 (글자, 글꼴, x0pt, x1pt, ypt)로 한 번만 색인한다.

    표 셀마다 다시 훑지 않도록 페이지당 1회만 만든다(글자 ~1.5천개 수준).
    좌표는 PDF 포인트 공간이라 get_text_bounded의 인자와 같은 기준이다.
    """
    n = textpage.count_chars()
    sizes = char_font_sizes(textpage, n)
    if not sizes:
        return []
    text = textpage.get_text_range(0, n)
    if len(text) != n:
        text = "".join(textpage.get_text_range(i, 1) for i in range(n))
    out = []
    for i, ch in enumerate(text):
        if ch in "\r\n":
            continue
        try:
            l, b, r, t = textpage.get_charbox(i)
        except Exception:
            continue
        out.append((ch, sizes[i], l, r, (b + t) / 2))
    return out


def embedded_lines(page, textpage, formulas: list[dict]) -> tuple[list[dict], list[dict]]:
    """내장 텍스트를 줄 단위로 추출하고, 문장 속 수식을 제자리에 끼워 넣는다.

    수식 영역 안의 내장 글자(깨진 수식 OCR 잔재)는 버리고, 그 자리에 새로 인식한
    LaTeX($...$)를 삽입한다. 반환: (본문 줄 목록, 줄에 삽입되지 않고 남은 수식 목록).
    """
    scale = RENDER_DPI / 72
    page_height = page.get_size()[1]
    n = textpage.count_chars()
    text = textpage.get_text_range(0, n)
    if len(text) != n:  # 서러게이트 등으로 인덱스가 어긋나면 글자별로 다시 읽는다
        text = "".join(textpage.get_text_range(i, 1) for i in range(n))

    embeddings = [f for f in formulas if f["kind"] == "embedding"]

    def char_box(i: int) -> tuple[float, float, float, float]:
        left, bottom, right, top = textpage.get_charbox(i)
        return (left * scale, (page_height - top) * scale,
                right * scale, (page_height - bottom) * scale)

    def hit_formula(cx: float, cy: float):
        for f in formulas:
            if f["x0"] <= cx <= f["x1"] and f["y0"] <= cy <= f["y1"]:
                return f
        return None

    font_sizes = char_font_sizes(textpage, n)

    lines: list[dict] = []
    consumed_ids: set[int] = set()
    chars: list[tuple[str, tuple, bool, float]] = []  # (글자, px 박스, 복원 여부, 글꼴)

    def flush_line() -> None:
        nonlocal chars
        # 조사 중복 절제: 수식 박스가 삼킨 조사 '이'를 복원했는데 바로 뒤(공백 없이)
        # 실제 '이'가 이어지면('이다/이면/이고/이므로'의 첫 글자) 복원분은 군더더기다
        # ('$수식$이이다' → '$수식$이다'). 실측 45건. 공백이 낀 '이 이론'류는
        # 사이에 공백 글자가 있어 영향받지 않고, 대상을 '이'+'이'로 한정해 안전하다.
        chars = [c for j, c in enumerate(chars)
                 if not (c[2] and c[0] == "이"
                         and j + 1 < len(chars) and chars[j + 1][0] == "이")]
        kept = [(ch, box) for ch, box, _, _ in chars]
        sup_marks = superscript_marks([(ch, fs) for ch, _, _, fs in chars])
        only_restored = chars and all(restored for _, _, restored, _ in chars)
        chars = []
        if not kept or only_restored:
            return
        sane = sum(1 for ch, _ in kept if _is_sane_char(ch) and not ch.isspace())
        visible = sum(1 for ch, _ in kept if not ch.isspace())
        if visible == 0 or sane / visible < MIN_SANE_CHAR_RATIO:
            return
        x0 = min(b[0] for _, b in kept)
        y0 = min(b[1] for _, b in kept)
        x1 = max(b[2] for _, b in kept)
        y1 = max(b[3] for _, b in kept)

        inserts: list[tuple[int, str]] = []
        for f in sorted(embeddings, key=lambda f: f["x0"]):
            if id(f) in consumed_ids:
                continue
            overlap = min(y1, f["y1"]) - max(y0, f["y0"])
            if overlap < 0.5 * min(y1 - y0, f["y1"] - f["y0"]):
                continue
            tolerance = 0.8 * (f["y1"] - f["y0"])
            if f["x0"] > x1 + tolerance or f["x1"] < x0 - tolerance:
                continue
            idx = next(
                (k for k, (_, b) in enumerate(kept) if (b[0] + b[2]) / 2 >= f["x0"]),
                len(kept),
            )
            inserts.append((idx, f" ${f['text']}$ "))
            consumed_ids.add(id(f))
        pieces: list[str] = []
        for k, (ch, _) in enumerate(kept):
            pieces.extend(marker for idx, marker in inserts if idx == k)
            # 내장 텍스트의 '$'는 이스케이프 — 수식 구분자 $와 짝을 이루면
            # 본문이 수식으로 렌더링된다(위 marker의 $만 구분자로 남긴다).
            pre, post = sup_marks.get(k, ("", ""))
            pieces.append(pre + (r"\$" if ch == "$" else ch) + post)
        pieces.extend(marker for idx, marker in inserts if idx == len(kept))
        merged = " ".join("".join(pieces).split())
        if merged:
            lines.append({"text": merged, "x0": x0, "y0": y0, "x1": x1, "y1": y1})

    for i, ch in enumerate(text):
        if ch in "\r\n":
            flush_line()
            continue
        box = char_box(i)
        cx = (box[0] + box[2]) / 2
        f = hit_formula(cx, (box[1] + box[3]) / 2)
        if f is not None:
            is_trailing_hangul = (
                f["kind"] == "embedding"
                and "가" <= ch <= "힣"
                and cx >= f["x0"] + 0.55 * (f["x1"] - f["x0"])
            )
            if not is_trailing_hangul:
                continue
            chars.append((ch, box, True, font_sizes[i] if font_sizes else 0.0))
        else:
            chars.append((ch, box, False, font_sizes[i] if font_sizes else 0.0))
    flush_line()

    remaining = [f for f in formulas if id(f) not in consumed_ids]
    return lines, remaining



# ─────────────────────────── 본문 안전 정제 ───────────────────────────


# 달리는 머리말: '쪽번호 + 제N장/Chapter' 꼴(예: '256 제 6장 시변계와 Maxwell 방정식').
# 쪽번호가 앞에 없는 '연습문제 1.2' 같은 절 표제는 매치되지 않는다(본문 보존).
# '장' 바로 뒤에 한글이 붙으면('3. 제 2장에서 다룬…' — 문제 번호+장 참조 문장)
# 머리말이 아니라 본문이므로 제외한다(검토단 오탐 실측 반영).
_RUNNING_HDR = re.compile(
    r"^\s*\d{1,4}\s*[.·]?\s*(제\s*\d+\s*장(?![가-힣])|C\s*H\s*A\s*P\s*T\s*E\s*R|Chapter)",
    re.I)

# 각주 시작 마커: 레이아웃이 각주를 머리말·워터마크와 같은 '버림' 클래스로
# 분류하므로, 하단 버림 영역에서 이 마커로 시작하는 실텍스트만 본문으로 살린다.
_FOOTNOTE_RE = re.compile(r"^\s*[*＊†‡]")

# 표 캡션 표지('표 5.1 …') — 위쪽 캡션 흡수와 표 추출 트리거가 함께 쓴다.
_TABLE_CAP_RE = re.compile(r"^표\s*\d")





# ─────────────────────────── 영역 분류 ───────────────────────────

CALLOUT_COLOR_RATIO = tuning.get("layout", "callout_color_ratio")  # 색 박스 판정


def colored_ratio(page_image, region: dict) -> float:
    """영역 배경의 유색(채도 있는) 픽셀 비율. 흰/검/회색이면 0에 가깝다.

    색칠된 강조·예제 박스(콜아웃)를 일반 본문과 구분하는 데 쓴다.
    """
    crop = page_image.crop(
        (region["x0"], region["y0"], region["x1"], region["y1"])
    ).convert("RGB").resize((40, 40))
    pix = list(crop.getdata())
    colored = sum(1 for r, g, b in pix if max(r, g, b) - min(r, g, b) > 40)
    return colored / len(pix) if pix else 0.0




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


def column_bands(regions: list[dict]) -> dict[int, tuple[float, float]]:
    """칼럼 번호별 x 범위(중심 추정용)를 구한다. {col: (x0, x1)}."""
    bands: dict[int, tuple[float, float]] = {}
    for r in regions:
        c = r.get("col", 1)
        if c < 1:
            continue
        if c in bands:
            bands[c] = (min(bands[c][0], r["x0"]), max(bands[c][1], r["x1"]))
        else:
            bands[c] = (r["x0"], r["x1"])
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
    bands = sorted(column_bands(layout_texts).values())
    gutter = None
    if len(bands) == 2 and bands[1][0] - bands[0][1] > -0.05 * page_w:
        gutter = (bands[0][1] + bands[1][0]) / 2
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
    stripped = re.sub(r"\s+", "", text)
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
    return clean_text(" ".join(out))


# ─────────────────────────── 페이지 → Markdown ───────────────────────────

def md_link_path(path: str) -> str:
    """Markdown 링크에 넣어도 안전한 경로 표기로 만든다.

    CommonMark는 ![](...)의 경로를 (a) 닫는 괄호 ')'와 (b) 공백 양쪽에서 끊는다.
    책 이름에 공백/괄호가 있으면(예: '대학물리 교재_OCR_images/…', 재실행 '(1)')
    링크가 그 자리에서 끊겨 이미지가 렌더되지 않는다(검토단 실측: 공백만으로
    2,349개 링크가 깨져 있었다). 둘 중 하나라도 있으면 각괄호 <...>로 감싼다 —
    <> 안에서는 공백·괄호가 모두 목적지의 일부로 인정된다.
    """
    return f"<{path}>" if re.search(r"[()\s]", path) else path


def save_figure(page_image, region: dict, images_dir: Path, page_no: int, idx: int,
                k: float = 1.0) -> str:
    """그림 영역을 PNG로 저장하고 MD에 넣을 상대 경로를 반환한다.

    page_image가 고해상 이미지면 k(고해상/기준 배율)로 좌표를 환산해 자른다.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    x0 = max(0, round(region["x0"] * k) - FIG_MARGIN)
    y0 = max(0, round(region["y0"] * k) - FIG_MARGIN)
    x1 = min(page_image.width, round(region["x1"] * k) + FIG_MARGIN)
    y1 = min(page_image.height, round(region["y1"] * k) + FIG_MARGIN)
    name = f"p{page_no}_fig{idx}.png"
    crop = page_image.crop((x0, y0, x1, y1))
    if max(crop.size) > FIG_MAX_PX:
        r = FIG_MAX_PX / max(crop.size)
        crop = crop.resize((round(crop.width * r), round(crop.height * r)),
                           Image.LANCZOS)
    if crop.mode != "P":
        crop = crop.convert("RGB").quantize(
            colors=FIG_COLORS, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)
    crop.save(images_dir / name, optimize=True)
    return md_link_path(f"{images_dir.name}/{name}")


def _accept_table(t_md: str | None) -> str:
    """추출된 MD 표의 최종 위생 게이트. 통과하면 표를, 아니면 빈 문자열을 돌려준다.

    정상 글자율이 낮거나(수식 셀의 깨진 텍스트) 한자·전각 잡음이 있으면 버린다.
    """
    if not t_md:
        return ""
    vis = [c for c in t_md if not c.isspace() and c not in "|-\\"]
    sane = sum(1 for c in vis if _is_sane_char(c)) / len(vis) if vis else 0
    junk = sum(1 for c in t_md if "一" <= c <= "鿿" or c in "（）☆□◇◎")
    return t_md if (sane >= 0.8 and junk == 0) else ""


# ─── 의미론적 절 헤딩(예제·정리·풀이…) ───
# 페이지 앵커('## N페이지')만으로는 AI가 '예제 3-1을 보여 줘' 같은 요청에서 절의
# 시작·끝을 알 수 없다(MinerU 비교에서 확인된 유일한 실질 열세). 본문 블록이 절
# 표지로 시작하면 '### 표지'로 승격하고 나머지는 본문으로 남긴다.
#
# 오탐 차단은 실측 기반이다(5권 산출물 조사):
#   - 표지어가 낱말의 일부인 경우: '정의역', '정의구간', '문제를', '예제와', '참고문언'
#     → 표지어 바로 뒤에 한글이 오면 표지가 아니다.
#   - 참조 문장: '퀴즈 7.8에서 본 것처럼' → 번호 바로 뒤에 한글이 오면 표지가 아니다.
#   - 색인·목록 줄: '연습문제 2.7, 공기 교체 연습문제 2.9, …' → 같은 표지어가 두 번
#     이상 나오면 목록이다.
_SEM_WORDS = (
    "예\\s?제", "연습\\s?문제", "복습\\s?문제", "문\\s?제", "풀\\s?이", "해\\s?답",
    "증\\s?명", "따름\\s?정리", "보조\\s?정리", "정\\s?리", "정\\s?의", "참\\s?고",
    "퀴\\s?즈", "요\\s?약", "예\\s?시", "보\\s?기",
    "Example", "Theorem", "Problem", "Definition", "Solution", "Proof",
)
# 번호 없이도 절 표지로 인정하는 말(관행적으로 단독 표제로 쓰인다).
_SEM_STANDALONE = {"예제", "연습문제", "복습문제", "문제", "풀이", "해답", "증명",
                   "참고", "요약", "퀴즈", "Solution", "Proof", "Problem"}
_SEM_HEAD = re.compile(
    r"^\s*(" + "|".join(_SEM_WORDS) + r")(?![가-힣])"
    # 번호는 통째로만 인정한다 — 뒤에 숫자·구두점·한글이 이어지면 매칭 실패시켜
    # '퀴즈 7.8에서'가 '퀴즈 7'로 잘려 표지가 되는 것을 막는다(부분 매칭 차단).
    r"(?:\s*([0-9]+(?:\s?[.\-]\s?[0-9]+)*)(?![0-9.\-가-힣]))?"
    r"\s*[:：._\-]?\s*")
# 표제 뒤 짧은 제목까지 헤딩에 포함할 최대 길이(그 이상은 본문으로 남긴다).
_SEM_TITLE_MAX = 30
_SEM_SENT_END = re.compile(r"(?:다|요|까|음|함)[.?!]\s*$|[.?!]\s*$")


def split_semantic_heading(text: str) -> tuple[str | None, str]:
    """본문 블록이 절 표지로 시작하면 (헤딩, 나머지 본문)을, 아니면 (None, 원문)."""
    m = _SEM_HEAD.match(text)
    if not m:
        return None, text
    word = re.sub(r"\s+", "", m.group(1))
    num = re.sub(r"\s+", "", m.group(2) or "")
    rest = text[m.end():].strip()
    # 같은 표지어가 뒤에 또 나오면 색인·목록 줄이다(헤딩 아님)
    if re.sub(r"\s+", "", rest).count(word) >= 1:
        return None, text
    # 번호가 안 붙었는데 뒤가 숫자로 시작하면 번호 매칭이 실패한 참조 문장이다
    # ('퀴즈 7.8에서 …') — 표지로 보지 않는다.
    if not num and rest[:1].isdigit():
        return None, text
    if not num and word not in _SEM_STANDALONE:
        return None, text            # '정의 …', '정리 …'는 번호가 있어야 표지로 본다
    head = f"{word} {num}".strip()
    # 표지 뒤 짧은 제목은 헤딩에 붙인다('정리 1.2.1 유일한 해의 존재')
    if rest and len(rest) <= _SEM_TITLE_MAX and not _SEM_SENT_END.search(rest):
        head = f"{head} {rest}".strip()
        rest = ""
    # 스캔 잔재 구분자(밑줄·콜론 등)가 헤딩 꼬리에 남지 않게 한다('예제 2-10 _')
    return head.rstrip(" _:：.-").strip(), rest


def render_flow(flow: list[dict], page_w: int) -> list[str]:
    """읽기 순서로 정렬된 블록들을 Markdown 줄 목록으로 만든다.

    - text   : 연속한 본문 블록은 양끝맞춤 여부로 한 문단으로 병합
    - callout: 색칠된 강조/예제 박스 → > [참고] 인용 블록(원래 위치 유지)
    - image  : ![그림](경로) + 바로 아래에 그림 캡션
    - formula: $$ ... $$ (+ 수식 번호)
    """
    # 칼럼별 본문 우측 끝(문단 병합 판정용). 다단이면 칼럼마다 폭이 다르다.
    col_right: dict[int, float] = {}
    for b in flow:
        if b["btype"] == "text":
            c = b.get("col", 1)
            col_right[c] = max(col_right.get(c, 0), b["x1"])

    md: list[str] = []
    paragraph = ""
    cur_col = None

    def flush_para():
        nonlocal paragraph
        if paragraph:
            md.extend([paragraph, ""])
            paragraph = ""

    for b in flow:
        if b.get("col") != cur_col:  # 칼럼이 바뀌면 문단 끊기
            flush_para()
            cur_col = b.get("col")
        if b["btype"] == "text":
            body = b["text"]
            head, rest = split_semantic_heading(body)
            if head:                       # 절 표지 → '### 헤딩'으로 승격
                flush_para()
                md.extend([f"### {head}", ""])
                if not rest:
                    continue
                body = rest
            paragraph = (paragraph + " " + body).strip() if paragraph else body
            join_limit = col_right.get(b.get("col", 1), page_w) - PARA_JOIN_MARGIN
            if b["x1"] < join_limit:  # 우측 끝에 못 미치면 문단 끝
                flush_para()
            continue
        flush_para()
        if b["btype"] == "callout":
            md.extend([f"> [참고] {b['text']}", ""])
        elif b["btype"] == "image":
            md.append(f"![{b['caption']}]({b['path']})")
            if b.get("caption_text"):
                md.extend(["", f"*{b['caption_text']}*"])
            if b.get("table_md"):  # 표 구조가 추출된 경우 그림 아래에 병기
                md.extend(["", *b["table_md"].splitlines()])
            md.append("")
        else:  # formula
            label = f"  {b['label']}" if b.get("label") else ""
            md.extend([f"$$ {b['text']} $${label}", ""])
    flush_para()
    return attach_orphan_captions(md)


# 그림 링크 뒤에 평문으로 떨어진 캡션 표지. 번호 뒤에 한글이 이어지면
# ('그림 1.2에서 …') 캡션이 아니라 본문 참조이므로 배제한다 — 이 경계가
# 없으면 5권에서 49건의 본문 문단을 캡션으로 잘못 삼킨다(실측).
_ORPHAN_CAP = re.compile(
    r"^(?:그림|그럼|기림|표|Fig(?:ure)?\.?|Table)\s*[0-9]+[.．][0-9]+(?![0-9가-힣])")
# 캡션이 두 줄로 쪼개졌을 때의 뒷줄('문제 1.17.'). 문장은 받지 않는다.
_ORPHAN_CONT = re.compile(r"^(?:문제|복습문제|예제|연습문제)\s*[0-9]+[.．][0-9]+\.?$")
# 실측상 진짜 캡션 첫 줄의 90%가 19자 이하 — 60자를 넘으면 본문으로 본다.
_ORPHAN_MAX = 60
# 캡션일 수 없는 줄머리(헤딩·다른 그림·블록수식·인용·표)
_ORPHAN_STOP = ("#", "![", "$$", ">", "|", "*")


def attach_orphan_captions(md: list[str]) -> list[str]:
    """그림 링크 바로 뒤에 평문으로 남은 캡션을 캡션(*…*)으로 흡수한다.

    render_flow의 기하 매칭은 캡션이 '그림 1.28' + '문제 1.17.' 처럼 두 줄로
    쪼개지거나 그림과 가로로 어긋나면 놓친다(5권 실측 696건, 대부분 전기회로
    이론의 연습문제면). 이미 캡션이 붙은 그림은 건드리지 않고, 한 캡션을 두
    그림이 나눠 갖지도 않는다(먼저 만난 그림이 가져간다). 두 번 적용해도
    결과가 같다.
    """
    out: list[str] = []
    i, n = 0, len(md)
    while i < n:
        line = md[i]
        out.append(line)
        if not line.startswith("!["):
            i += 1
            continue
        nonblank = [j for j in range(i + 1, n) if md[j].strip()][:2]
        if nonblank and md[nonblank[0]].lstrip().startswith("*"):
            i += 1                                  # 이미 캡션이 있다
            continue
        take: list[int] = []
        for k, j in enumerate(nonblank):
            s = md[j].strip()
            if s.startswith(_ORPHAN_STOP):
                break
            if k == 0:
                if not (_ORPHAN_CAP.match(s) and len(s) <= _ORPHAN_MAX):
                    break
            elif not _ORPHAN_CONT.match(s):
                break
            take.append(j)
        if not take:
            i += 1
            continue
        out.extend(["", "*" + " ".join(md[j].strip() for j in take) + "*", ""])
        i = take[-1] + 1
        while i < n and not md[i].strip():           # 흡수한 줄 뒤 빈 줄 정리
            i += 1
    return out


PDF_READ_LIMIT_MB = 100      # 읽기 도구가 PDF 텍스트 추출을 거부하는 경계(실측)


def ai_preamble(pdf_name: str, images_dir_name: str, pdf_bytes: int) -> str:
    """이 문서를 읽는 AI를 위한 자기 기술 안내 한 줄(인용 블록).

    폴백 1순위는 PNG다. 읽기 도구는 100MB를 넘는 PDF의 열람을 거부하는데
    교재 스캔본은 대개 이를 초과한다(보유 5권 실측 88~748MB, 4권이 초과).
    PDF를 1순위로 안내하면 AI가 열리지 않는 경로로 유도되므로 순서를 뒤집고,
    이 책이 열리는 크기인지도 함께 알려 준다.
    """
    mb = pdf_bytes / 1048576
    note = (f"원본 PDF(`{pdf_name}`, {mb:.0f}MB)도 같은 폴더에 있으나, 읽기 도구는 "
            f"{PDF_READ_LIMIT_MB}MB를 넘는 PDF를 열지 못한다"
            + ("—이 책이 그에 해당하므로 PNG가 유일한 폴백이다."
               if mb > PDF_READ_LIMIT_MB else "(이 책은 그 아래라 열람 가능)."))
    return (f"> **AI 안내**: 이 문서는 `{pdf_name}`의 OCR 변환본이다. "
            "'## N페이지' 절은 원본 PDF의 N쪽과 1:1로 대응한다. 그림·표·수식을 "
            f"정밀하게 확인해야 할 때는 '{images_dir_name}' 폴더의 PNG를 열람하라 "
            "— 본문의 그림 링크가 그대로 파일 경로이며, 원본 화소가 보존되어 있어 "
            "OCR이 표로 옮기지 못한 도표도 판독할 수 있다. 쪽 제목의 '(인쇄 N쪽)'은 "
            "교재에 인쇄된 쪽번호다 — 학생이 '교재 274쪽'이라 하면 그 값으로 찾아라. "
            "PDF 쪽과 인쇄 쪽의 차이는 일정하지 않아(같은 책 안에서도 변한다) "
            f"산술로 환산하면 안 된다. {note}")


_BODY_SKIP = ("!", "|", "#", ">", "$$", "*")


def body_chars(md: list[str]) -> int:
    """그림·표·헤딩·캡션을 뺀 순수 본문 글자 수 — 조용한 전멸 판정에 쓴다."""
    n = 0
    for line in md:
        s = line.strip()
        if not s or s.startswith(_BODY_SKIP):
            continue
        n += len(re.sub(r"\s", "", _MATH_SPAN.sub(" ", s)))
    return n


def has_ink(page_image, thresh: int = 250, min_ratio: float = 0.002) -> bool:
    """쪽에 실제로 내용이 있는지 — 백지 쪽과 '내용이 있는데 다 놓친 쪽'을 가른다."""
    small = page_image.convert("L").resize((160, 220))
    dark = sum(1 for p in small.getdata() if p < thresh)
    return dark / (160 * 220) > min_ratio


_PREAMBLE_LINE = re.compile(r"(?m)^> \*\*AI 안내\*\*: .*$")
_PAGE_HEAD_NO = re.compile(r"(?m)^## (\d+)페이지 \(인쇄 (\d+)쪽\)$")
PAGE_NO_WINDOW = 10      # 이웃 판정 창(앞뒤 쪽 수)
PAGE_NO_MIN_VOTES = 3    # 창 안에 이만큼 있어야 판정한다
PAGE_NO_TOL = 3          # 이웃 중앙값과 이만큼 넘게 어긋나면 오탐


def prune_page_number_outliers(md_path: Path) -> int:
    """이웃 쪽과 오프셋이 어긋나는 인쇄 쪽번호를 지운다. 반환: 지운 개수.

    쪽마다 독립으로 읽으면 머리말의 다른 숫자(장 번호·연도·수식)가 쪽번호로
    둔갑한다 — 실측: 전자기학은 머리말에 쪽번호가 아예 없는데 562쪽 중 13쪽에
    엉뚱한 값이 붙었고(예: PDF 81쪽 → '인쇄 22쪽'), 대학수학은 오프셋이
    +5~+8이어야 하는데 -30~+10으로 흩어졌다.

    오프셋은 책 안에서 변하지만 천천히 변한다(실측 최대 변화폭 16, 1,177쪽에
    걸쳐). 그래서 이웃 창의 중앙값에서 크게 벗어나면 오탐이다. 창 안에 표본이
    부족하면(고립된 값) 검증할 수 없으므로 지운다 — 확인 못 한 쪽번호는
    없는 것보다 나쁘다(AI가 그 값을 믿고 엉뚱한 쪽을 읽는다).
    """
    text = md_path.read_text(encoding="utf-8")
    hits = [(int(m.group(1)), int(m.group(2)), m.span())
            for m in _PAGE_HEAD_NO.finditer(text)]
    if not hits:
        return 0
    offs = {pdf: pdf - pr for pdf, pr, _s in hits}
    drop: list[tuple[int, int]] = []
    for pdf, _pr, span in hits:
        near = sorted(o for p, o in offs.items()
                      if p != pdf and abs(p - pdf) <= PAGE_NO_WINDOW)
        if len(near) < PAGE_NO_MIN_VOTES:
            drop.append(span)
            continue
        med = near[len(near) // 2]
        if abs(offs[pdf] - med) > PAGE_NO_TOL:
            drop.append(span)
    if not drop:
        return 0
    out, last = [], 0
    for s, e in drop:
        out.append(text[last:s])
        out.append(re.sub(r" \(인쇄 \d+쪽\)$", "", text[s:e]))
        last = e
    out.append(text[last:])
    md_path.write_text("".join(out), encoding="utf-8")
    return len(drop)


def insert_glossary(md_path: Path) -> int:
    """완성된 MD의 머리말 뒤에 '이 책이 쓰는 용어' 절을 끼워 넣는다.

    반환: 실린 용어 수(0이면 색인을 못 찾아 아무것도 넣지 않았다).
    이미 실려 있으면 다시 넣지 않는다(두 번 적용해도 결과가 같다).
    """
    import pdf_glossary

    text = md_path.read_text(encoding="utf-8")
    if "## 이 책이 쓰는 용어" in text:
        return 0
    terms = pdf_glossary.build(text)
    block = pdf_glossary.block(terms)
    if not block:
        return 0
    m = _PREAMBLE_LINE.search(text)
    at = m.end() if m else text.find("\n\n")
    if at < 0:
        return 0
    md_path.write_text(text[:at] + "\n\n" + "\n".join(block) + text[at:],
                       encoding="utf-8")
    print(f"  [용어] 이 책이 쓰는 용어 {len(terms)}개를 머리말 뒤에 실었습니다")
    return len(terms)


def _discard_output(out_path: Path, images_dir: Path) -> None:
    """쓸모없는 산출물을 지운다 — 잔해가 정식 이름을 선점하지 못하게."""
    out_path.unlink(missing_ok=True)
    if images_dir.is_dir():
        for f in images_dir.glob("*.png"):
            f.unlink(missing_ok=True)
        try:
            images_dir.rmdir()
        except OSError:
            pass


def upright_page(page_image, tmp_dir: Path, page_no: int):
    """눕거나 뒤집힌 쪽을 세워서 돌려준다(필요 없으면 원본 그대로).

    가로가 세로보다 긴 쪽만 검사한다 — 교재의 세로 쪽에 OSD를 다 돌리면
    쪽마다 비용이 붙고, 실제 문제는 '눕혀 스캔한 쪽'에서 생긴다. 세워야
    한다고 판단되면 회전 각도를 함께 돌려주어 호출부가 로그로 남긴다.
    """
    if page_image.width <= page_image.height:
        return page_image, 0
    probe = tmp_dir / f"osd{page_no}.png"
    page_image.save(probe)
    try:
        angle = detect_rotation(probe)
    finally:
        probe.unlink(missing_ok=True)
    if angle % 360 == 0:
        return page_image, 0
    # OSD의 각도는 '이만큼 돌리면 똑바로'라는 뜻 — PIL의 rotate는 반시계이므로
    # expand=True로 잘림 없이 그대로 적용한다.
    return page_image.rotate(-angle, expand=True), angle


def precompute_page(page_image):
    """레이아웃 분석 + 수식 검출 — 선행 스레드에서 다음 페이지 몫을 미리 계산한다.

    (렌더링은 pdfium이 스레드 불안전이라 반드시 메인 스레드에서 한다)
    """
    import pdf_layout
    import pdf_math

    return pdf_layout.analyze(page_image), pdf_math.find_formulas(page_image)


_PAGE_NO_TOKEN = re.compile(r"(?<![\d.])(\d{1,4})(?![\d.])")
# 낱자로 흩어진 숫자 런만 붙인다('8 7 4' → '874'). 인접 숫자 사이 공백을
# 무조건 지우면 '392 16장'이 '39216장'이 되어 쪽번호가 사라진다(실측).
_SPACED_DIGITS = re.compile(r"(?<!\d)\d(?: \d)+(?!\d)")
PAGE_NO_DRIFT = 60          # PDF 쪽과 이만큼 넘게 벌어지면 쪽번호가 아니다


def printed_page_number(header_text: str, page_no: int) -> int | None:
    """머리말 한 줄에서 인쇄 쪽번호를 읽어낸다(못 찾으면 None).

    머리말은 '274  CHAPTER 7 일차 회로' 또는 '14.3 삼중적분  495'처럼 쪽번호가
    양끝에 붙는다. 장 번호('CHAPTER 7')·절 번호('14.3')와 헷갈리지 않도록
    소수점에 붙은 숫자를 빼고, PDF 쪽 번호와 상식적인 거리(±60) 안에 있는
    후보만 받는다 — 오프셋은 책마다·구간마다 다르지만 그 정도로 벌어지지는
    않는다(실측 최대 +26).

    저품질 내장층은 숫자를 낱자로 띄워 새긴다('8 7 4') — 먼저 붙인다.
    """
    t = _SPACED_DIGITS.sub(lambda m: m.group(0).replace(" ", ""),
                           " ".join((header_text or "").split()))
    if not t:
        return None
    best = None
    for m in _PAGE_NO_TOKEN.finditer(t):
        v = int(m.group(1))
        if v < 1 or abs(page_no - v) > PAGE_NO_DRIFT:
            continue
        # 양끝에 가까울수록 쪽번호답다(가운데 숫자는 본문·장 번호일 확률이 큼)
        edge = min(m.start(), len(t) - m.end())
        if best is None or edge < best[0]:
            best = (edge, v)
    return best[1] if best else None


def read_printed_page(page, page_image, tmp_dir: Path, page_no: int) -> int | None:
    """상단 머리말 띠를 직접 읽어 인쇄 쪽번호를 회수한다.

    파이프라인의 줄 목록에 기대지 않는다 — 책에 따라 머리말이 줄로 잡히지
    않는다(실측: 응용수학·전기회로는 header_band 안에 줄이 0개였다).
    내장 텍스트층을 먼저 보고, 비어 있으면 띠만 한 줄 OCR한다.
    """
    try:
        h, w = page.get_height(), page.get_width()
        tp = page.get_textpage()
        band = tp.get_text_bounded(left=0, bottom=h * (1 - HEADER_BAND_RATIO),
                                   right=w, top=h)
    except Exception:
        band = ""
    n = printed_page_number(band, page_no)
    if n is not None:
        return n
    return printed_page_number(
        pdf_text.ocr_top_band(page_image, tmp_dir, str(page_no),
                              HEADER_BAND_RATIO), page_no)


def process_page(page, page_image, images_dir: Path, page_no: int,
                 tmp_dir: Path, pre=None, hires_image=None,
                 force_scan: bool = False
                 ) -> tuple[list[str], int, str, int | None]:
    """한 페이지를 인식해 (Markdown 줄, 수식 수, 본문 출처, 인쇄 쪽번호)를 반환한다.

    pre: precompute_page()가 미리 계산한 (레이아웃, 수식검출). 없으면 여기서 계산.
    hires_image: 스캔 원본이 기준 해상도보다 높은 페이지의 원본 해상도 렌더링.
      레이아웃·좌표 계산은 기준 해상도(page_image) 공간에서 그대로 하고,
      Tesseract 입력·수식 크롭·그림 저장만 이 이미지를 써서 원본 화질을 살린다.
    force_scan: 참이면 내장 텍스트 레이어가 있어도 무시하고 스캔 경로로 인식한다
      (타 도구가 입힌 저품질 OCR층을 신뢰하지 않는 책 — 장구분.toml의 force_scan).
    """
    import pdf_layout
    import pdf_math

    page_w = page_image.width
    k = hires_image.width / page_image.width if hires_image is not None else 1.0
    hires_dpi = round(RENDER_DPI * k)
    regions = pre[0] if pre else pdf_layout.analyze(page_image)
    image_regions = [r for r in regions if r["kind"] == "image"]
    layout_texts = [r for r in regions if r["kind"] == "text"]
    drop_boxes = [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in regions if r["kind"] == "drop"]

    # 레이아웃이 머리말을 TEXT로 잘못 남긴 페이지의 누수 차단: 페이지 상단 띠에
    # 완전히 들어간 텍스트 영역·본문 줄은 러닝 헤더이므로 버린다.
    # 임계 7.2%: 5권 실측에서 머리말은 y1≤6.8%H에서 끝나고, 본문 첫 줄은
    # (스캔북 포함) y0≥7.3%H에서 시작한다 — 양쪽 모두 여유가 있는 경계값.
    # 다만 비율만 믿으면 상단 여백이 좁은 자료(강의 슬라이드, 여백 없는 조판)의
    # 매 쪽 첫 줄 — 대개 장·절 제목 — 을 말없이 잘라낸다(검토단 실증). 띠 안의
    # 글이 러닝 헤더답게 '짧을' 때만 버린다. 러닝 헤더는 쪽번호·장제목 조각이라
    # 짧고, 본문 첫 줄은 문장이거나 제목이라 길다.
    header_band = HEADER_BAND_RATIO * page_image.height
    in_band = [r for r in layout_texts
               if r["y1"] <= header_band and len(r.get("text", "")) <= HEADER_MAX_CHARS]
    drop_boxes += [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in in_band]
    _dropped = {id(r) for r in in_band}
    layout_texts = [r for r in layout_texts if id(r) not in _dropped]

    # 그림 영역 내부의 글자·수식 라벨은 본문으로 새어 나오면 안 된다(이미지 PNG에 포함됨).
    # 영역 안에 중심이 들어오는 텍스트 줄·수식을 걸러내는 판정 함수.
    fig_boxes = [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in image_regions]

    def in_figure(box: dict) -> bool:
        cx = (box["x0"] + box["x1"]) / 2
        cy = (box["y0"] + box["y1"]) / 2
        return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in fig_boxes)

    # 그림 위에 적힌 축·곡선 라벨('f' 'x' 등 홑 기호)은 중심이 그림 박스 경계 바로
    # 바깥에 잡혀 in_figure를 빠져나와 '$$f$$' 같은 무의미한 독립 수식이 된다
    # (라벨 내용은 이미 그림 PNG에 있다). 홑 기호 독립 수식만 그림 catchment를
    # 살짝 넓혀(3%H·2%W) 걸러낸다 — 실수식은 홑 기호 하나로 전시되지 않는다.
    _fig_pad_x = 0.02 * page_image.width
    _fig_pad_y = 0.03 * page_image.height

    def near_figure(box: dict) -> bool:
        cx = (box["x0"] + box["x1"]) / 2
        cy = (box["y0"] + box["y1"]) / 2
        return any(x0 - _fig_pad_x <= cx <= x1 + _fig_pad_x
                   and y0 - _fig_pad_y <= cy <= y1 + _fig_pad_y
                   for x0, y0, x1, y1 in fig_boxes)

    # 수식은 MFD로 검출(독립+인라인) → LaTeX 인식
    detections = pre[1] if pre else pdf_math.find_formulas(page_image)

    # 스캔 페이지: Tesseract(외부 프로세스)와 MFR(LaTeX 인식)은 겹쳐 돌린다. 스캔본은
    # Tesseract가 페이지 시간의 ~90%로 최대 병목이므로(실측) MFR보다 먼저 비동기로
    # 던져 MFR·다음 페이지 선계산과 최대한 겹친다. 환각으로 버려질 검출 박스까지
    # 가리게 되지만, 그런 영역은 그림 조각·잡음이라 본문이 아니다(기준 페이지
    # 대조로 출력 동일성 검증됨).
    textpage = page.get_textpage()
    embedded = has_embedded_text(textpage) and not force_scan
    tess_future = None
    rescue_future = None
    ocr_bands = None
    # 하단 버림 영역은 각주일 수 있다(레이아웃이 각주·꼬리말·워터마크를 같은
    # 클래스로 버림). 마스크에서 빼고 OCR한 뒤 내용으로 판별해 각주만 살린다.
    foot_boxes = [b for b in drop_boxes if b[1] > 0.8 * page_image.height] \
        if not embedded else []
    if not embedded:
        mask = [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in image_regions]
        mask += [(b[0], b[1], b[2], b[3]) for _, b in detections]
        mask += [b for b in drop_boxes if b not in foot_boxes]
        ocr_bands = detect_columns(layout_texts, page_w)  # 다단이면 칼럼별로 따로 인식
        if hires_image is not None:  # 고해상 입력으로 인식(좌표는 결과 수신 후 환산)
            mask_hi = [tuple(v * k for v in b) for b in mask]
            bands_hi = [(x0 * k, x1 * k) for x0, x1 in ocr_bands] if ocr_bands else None
            tess_future = _TESS_POOL.submit(
                tesseract_lines, hires_image, mask_hi, tmp_dir, bands_hi, hires_dpi,
                tag=f"p{page_no}")
            # 고해상 사각지대 구조: 400dpi에서 Tesseract가 짧은 들여쓰기 줄을 PSM
            # 불문 놓치는 사례 실측(p567) — 기준 해상도로 한 번 더 읽어 주 결과에
            # 없는 줄만 보충한다. 같은 풀(순차)이라 MFR 그늘에 함께 숨는다.
            rescue_future = _TESS_POOL.submit(
                tesseract_lines, page_image, mask, tmp_dir, ocr_bands,
                tag=f"p{page_no}r")
        else:
            tess_future = _TESS_POOL.submit(
                tesseract_lines, page_image, mask, tmp_dir, ocr_bands,
                tag=f"p{page_no}")

    try:
        if hires_image is not None:  # 수식 크롭도 원본 해상도로
            latexes = pdf_math.recognize_latex(
                hires_image, [tuple(round(v * k) for v in box) for _, box in detections])
        else:
            latexes = pdf_math.recognize_latex(page_image, [box for _, box in detections])
    except Exception:
        for fut in (tess_future, rescue_future):  # MFR 실패 시 떠 있는 Tesseract 정리
            if fut is not None:
                try:
                    fut.result()
                except Exception:
                    pass  # Tesseract 자체 오류가 원인(MFR 예외)을 가리지 않게 한다
        raise
    formulas = [
        {"kind": kind, "text": lx, "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]}
        for (kind, b), lx in ((d, clean_latex(x)) for d, x in zip(detections, latexes))
        if lx
    ]
    n_formulas = len(formulas)
    isolated = [f for f in formulas if f["kind"] == "isolated"]

    # 본문 줄 확보 (내장 텍스트 우선, 없으면 위에서 병렬 시작한 Tesseract 결과)
    if embedded:
        lines, isolated = embedded_lines(page, textpage, formulas)
        weave: list[dict] = []  # 내장 경로는 줄에 인라인 수식이 이미 들어 있음
        source = "내장 텍스트"
    else:
        lines = tess_future.result()
        if hires_image is not None:  # 고해상 좌표 → 기준 공간으로 환산
            for ln in lines:
                for key in ("x0", "y0", "x1", "y1"):
                    ln[key] /= k
                # 단어 경계도 같은 공간으로 — 보충 줄(기준 해상도)과 섞이므로 필수
                if ln.get("words"):
                    ln["words"] = [(n, a / k, b / k) for n, a, b in ln["words"]]
        weave = [f for f in formulas if f["kind"] == "embedding"]  # 스캔: 인라인 수식 재삽입
        source = "Tesseract" + (f"({len(ocr_bands)}단)" if ocr_bands else "")
        if hires_image is not None:
            source += f"·{hires_dpi}dpi"
        if rescue_future is not None:
            try:  # 기준 해상도 보충 — 실패해도 주 결과에는 영향 없음(보충일 뿐)
                resc = rescue_future.result()
            except Exception:
                resc = []
            # 레이아웃이 본문으로 보증한 영역 안의 줄만 보충한다 — 영역 밖 떠돌이
            # 줄(색 밴드의 저해상 오독 등)이 콜아웃·본문 잡음으로 새는 것을
            # 차단한다(p567 실측: 'OE wer 5288' 유입 사례).
            resc = [ln for ln in resc
                    if any(r["x0"] <= (ln["x0"] + ln["x1"]) / 2 <= r["x1"]
                           and r["y0"] <= (ln["y0"] + ln["y1"]) / 2 <= r["y1"]
                           for r in layout_texts)]
            n_rescued = merge_rescue_lines(lines, resc)
            if n_rescued:
                source += f"·구조{n_rescued}줄"

    # 버리기 전에 인쇄 쪽번호를 붙잡는다 — 교재의 쪽번호는 바로 이 머리말 띠에
    # 있는데(하단 아님, 실측 5권 전부) 도구가 통째로 버려 왔다. 그래서 학생이
    # "교재 274쪽"이라 하면 AI가 PDF 274쪽을 읽어 엉뚱한 답을 했다.
    # 단일 오프셋으로는 못 고친다: 실측 결과 오프셋이 책 안에서도 변한다
    # (대학물리 +13→+8→+4→-3, 대학수학 +8→+5). 쪽마다 새기는 수밖에 없다.
    printed_no = read_printed_page(page, page_image, tmp_dir, page_no)

    # 그림 내부에 들어온 줄·수식 라벨 제거 (그림 PNG에 이미 포함되어 중복·잡음이 됨)
    lines = [ln for ln in lines
             if not in_figure(ln)
             and (ln["y1"] > header_band
                  or len(ln.get("text", "")) > HEADER_MAX_CHARS)]
    # 위치 띠(7.2%)를 벗어난 머리말 잔존(스캔 크롭 변동): 확장 띠(12%) 안에서
    # '쪽번호 + 장 표지' 내용 형태만 추가로 버린다 — 쪽번호 없는 절 표제
    # ('연습문제 1.2' 등)는 본문이므로 보존된다.
    hdr_ext = HEADER_EXT_RATIO * page_image.height
    lines = [ln for ln in lines
             if not (ln["y0"] < hdr_ext and _RUNNING_HDR.match(ln["text"]))]
    # 내장 경로: 레이아웃 drop 마스크가 스캔 전용이라 하단 쪽번호·꼬리말(순수
    # 숫자류)이 떠돌이 줄로 본문에 샐 수 있다(검토단 지적) — 최하단 띠의
    # 숫자 줄만 버린다. (내장 5권 실측: 해당 누수 0건 — 예방 조치)
    if embedded:
        foot_band = FOOT_BAND_RATIO * page_image.height
        lines = [ln for ln in lines
                 if not (ln["y0"] > foot_band
                         and re.fullmatch(r"[\d\s.\-/|]{1,12}", ln["text"]))]
    # 하단 버림 영역(각주·워터마크·쪽번호)의 전면 OCR 줄은 신뢰하지 않고 제거한다 —
    # 작은 하단 영역은 전면 이진화로 뭉개지거나 통째로 누락되기 쉽다. 각주는 아래
    # 복원 단계에서 개별 크롭으로 정확히 되살린다(워터마크·쪽번호는 걸러진다).
    if foot_boxes:
        def _in_foot(ln):
            cx, cy = (ln["x0"] + ln["x1"]) / 2, (ln["y0"] + ln["y1"]) / 2
            return any(fx0 <= cx <= fx1 and fy0 <= cy <= fy1
                       for fx0, fy0, fx1, fy1 in foot_boxes)
        lines = [ln for ln in lines if not _in_foot(ln)]
    isolated = [f for f in isolated
                if not in_figure(f)
                and not (_SINGLE_SYMBOL.match(f["text"]) and near_figure(f))]
    weave = [f for f in weave if not in_figure(f)]

    # 줄을 레이아웃 텍스트 영역에 배정(없으면 떠돌이 영역 생성)
    text_regions = [dict(r) for r in layout_texts]
    assign_lines(lines, text_regions)
    woven: set[int] = set()
    for r in text_regions:
        r["text"] = assemble_region_text(r, weave, woven)
    # 색 밴드(파란 소제목·예제 표지·강조 박스)의 흰 글씨는 전면 이진화로 사라져
    # 텍스트가 비면 아래 pool에서 탈락한다 — 해당 색 영역만 개별 크롭으로 재인식해
    # 소제목·표지 구조를 복원한다(스캔 경로 전용; 내장 경로는 텍스트 레이어가 있음).
    # 아래 두 복원은 스캔 경로 전용이다 — 내장(embedded) 책은 텍스트 레이어에서
    # 소제목·장 제목이 이미 온전히 나오므로 손대지 않는다(v5 출력과 동일 유지).
    if not embedded:
        base_img = hires_image if hires_image is not None else page_image
        for idx, r in enumerate(text_regions):
            if len(_WORDISH.findall(r["text"])) >= 2:
                continue
            if colored_ratio(page_image, r) <= CALLOUT_COLOR_RATIO:
                continue
            box = (r["x0"] * k, r["y0"] * k, r["x1"] * k, r["y1"] * k)
            rec = ocr_region_text(base_img, box, tmp_dir, f"band{page_no}_{idx}", hires_dpi)
            if len(_WORDISH.findall(rec)) >= 2:
                r["text"] = rec
        # 장 표지 제목 되살리기: 레이아웃이 상단의 '큰' 제목(사진 위에 박힌 장 제목
        # 등)을 러닝 헤더처럼 drop으로 버리는 경우가 있다. 러닝 헤더는 얇으므로(≈2%H),
        # 상단의 충분히 '큰'(>4.5%H) drop 영역만 개별 크롭으로 인식해, 페이지 어디에도
        # 없는(이미지에만 박힌) 제목이면 되살린다.
        page_txt = re.sub(r"\s+", "", " ".join(r.get("text", "") for r in text_regions))
        for idx, r in enumerate(regions):
            if r["kind"] != "drop":
                continue
            if r["y0"] > 0.16 * page_image.height:
                continue
            if (r["y1"] - r["y0"]) < 0.045 * page_image.height:
                continue
            box = (r["x0"] * k, r["y0"] * k, r["x1"] * k, r["y1"] * k)
            rec = ocr_region_text(base_img, box, tmp_dir, f"title{page_no}_{idx}", hires_dpi)
            if len(_WORDISH.findall(rec)) < 2 or _RUNNING_HDR.match(rec):
                continue
            if re.sub(r"\s+", "", rec) in page_txt:  # 이미 본문/캡션에 있으면 중복 방지
                continue
            text_regions.append({"x0": r["x0"], "y0": r["y0"], "x1": r["x1"],
                                 "y1": r["y1"], "col": 1, "type": "TITLE", "text": rec})
            page_txt += re.sub(r"\s+", "", rec)  # 같은 제목의 중복 복원 방지
        # 하단 각주 되살리기: 하단 drop(각주·워터마크·쪽번호)을 개별 크롭으로 인식해,
        # 각주 마커(* † ‡)·한글(4자+)·연도범위(1803-1853) 중 하나가 있고 아직 페이지에
        # 없으면 본문으로 살린다. 워터마크('Made with…')·쪽번호는 셋 다 없어 걸러진다.
        page_txt = re.sub(r"\s+", "", " ".join(r.get("text", "") for r in text_regions))
        for fi, b in enumerate(foot_boxes):
            rec = ocr_region_text(base_img, tuple(v * k for v in b),
                                  tmp_dir, f"foot{page_no}_{fi}", hires_dpi)
            if not rec or _RUNNING_HDR.match(rec):
                continue
            if not (_FOOTNOTE_RE.match(rec)
                    or len(re.findall(r"[가-힣]", rec)) >= 4
                    or re.search(r"\(\s*1?\d{3}\s*[-~–]\s*1?\d{3}\s*\)", rec)):
                continue
            if re.sub(r"\s+", "", rec) in page_txt:
                continue
            text_regions.append({"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3],
                                 "col": 1, "type": "TEXT", "text": rec})
            page_txt += re.sub(r"\s+", "", rec)  # 같은 각주의 중복 복원 방지
    # 어느 영역에도 못 들어간 인라인 수식은 독립 수식으로 승격한다(무음 소실 방지).
    isolated += [f for f in weave if id(f) not in woven]

    # 수식 번호(우측 여백 단문)를 같은 행의 독립 수식에 붙임.
    # 어느 수식에도 붙지 못한 후보는 본문으로 복원한다 — 좁은 우측 칼럼의
    # 짧은 실제 본문이 소리 없이 사라지는 것을 막는다.
    eq_labels = [r for r in text_regions if is_eq_label(r, page_w) and r["text"]]
    used_labels: set[int] = set()
    for f in isolated:
        fy = (f["y0"] + f["y1"]) / 2
        for r in eq_labels:
            if r["y0"] <= fy <= r["y1"]:
                f["label"] = r["text"]
                used_labels.add(id(r))
                break
    pool = [r for r in text_regions if r["text"] and id(r) not in used_labels]

    # 그림 캡션을 해당 그림에 붙임: 그림과 가로로 겹치고 바로 아래/안쪽에 있는
    # '캡션다운' 텍스트(번호·설명 조각)를 모두 모아 위→아래 순서로 합친다.
    # 본문 문단이 빨려드는 것을 막기 위해, 길거나 캡션 표지가 없는 긴 글은 제외한다.
    # 오귀속(검토단 실측 756건)은 is_caption_like 쪽에서 좁혔다 — 완결된 문장과
    # 한글 없는 잡음 조각을 캡션 후보에서 뺐다. 순회 순서는 건드리지 않는다:
    # y0로 정렬해 보았더니 공유 캡션의 청구 순서가 바뀌어 전기회로 p53의 캡션이
    # 5개→2개로 줄었다(먼저 잡은 그림이 배타권을 갖는 구조라 순서가 결과를
    # 바꾼다). 순서를 바꾸려면 공유 캡션 처리부터 재설계해야 한다.
    used: set[int] = set()
    cap_gap = 0.16 * page_image.height
    near_gap = 0.04 * page_image.height
    for fig in image_regions:
        def _in_band(r, _f=fig):
            # 표지('그림 3-7' 등)로 시작하는 글만 넓은 띠를 허용한다. 표지가 없는
            # 글은 그림 바로 아래로 제한 — 넓은 띠(16%H≈340px)가 캡션 아래의
            # 별개 본문 영역까지 삼켜 캡션에 본문이 붙던 경로다(실측 p113: 캡션
            # y1191, 본문 y1419가 둘 다 띠 안이었다).
            gap = cap_gap if _CAPTION_RE.match(r["text"]) else near_gap
            return _f["y0"] - 10 <= r["y0"] <= _f["y1"] + gap

        cands = [
            r for r in pool
            if id(r) not in used
            and _overlap_ratio(r["x0"], r["x1"], fig["x0"], fig["x1"]) > 0.4
            and _in_band(r)
            and is_caption_like(r["text"])
            # 본문 참조 문장('그림 1.28은 5개의 소자를…')은 캡션이 아니다.
            # 표지+번호로 시작하되 번호 뒤에 조사가 붙으면 참조다(실측 p53).
            and not _CAPTION_REF.match(r["text"])
        ]
        # 표 캡션('표 N ...')은 표 위에 붙는 관행 — 바로 위 띠에서 추가로 흡수한다.
        above = [
            r for r in pool
            if id(r) not in used
            and _overlap_ratio(r["x0"], r["x1"], fig["x0"], fig["x1"]) > 0.4
            and fig["y0"] - 0.06 * page_image.height <= r["y1"] <= fig["y0"] + 10
            and _TABLE_CAP_RE.match(r["text"])
        ]
        cands = sorted(above, key=lambda r: r["y0"]) + sorted(cands, key=lambda r: r["y0"])
        fig["caption_text"] = " ".join(r["text"] for r in cands)
        for r in cands:
            used.add(id(r))

    body_regions = [r for r in pool if id(r) not in used]

    # 다단 페이지 읽기 순서: 칼럼 번호 → y. 단일 칼럼이면 모두 col=1이라 y 순서와 같다.
    col_bands = column_bands(layout_texts + image_regions)

    # 읽기 순서 흐름 구성: 본문/강조박스 + 독립 수식 + 그림
    flow: list[dict] = []
    for r in body_regions:
        btype = "callout" if is_callout(r, page_image) else "text"
        flow.append({"btype": btype, "text": r["text"], "col": r.get("col", 1),
                     "y0": r["y0"], "y1": r["y1"], "x0": r["x0"], "x1": r["x1"]})
    for f in isolated:
        cx = (f["x0"] + f["x1"]) / 2
        flow.append({"btype": "formula", "text": f["text"], "label": f.get("label", ""),
                     "col": infer_column(cx, col_bands), "y0": f["y0"], "y1": f["y1"],
                     "x0": f["x0"], "x1": f["x1"]})
    fig_idx = 0
    fig_src = hires_image if hires_image is not None else page_image
    s_pt = RENDER_DPI / 72          # 200dpi 픽셀 → PDF 포인트
    page_h_pt = page.get_size()[1]
    for fig in sorted(image_regions, key=lambda r: r["y0"]):
        fig_idx += 1
        path = save_figure(fig_src, fig, images_dir, page_no, fig_idx, k)
        # 표 구조 추출: TABLE 분류이거나 캡션이 '표 N'인 영역만 시도한다
        # (그래프 축·틀이 격자로 오인되는 것을 캡션 의미로 원천 차단).
        # 격자·채움·정상글자율 게이트를 통과하면 그림 아래에 MD 표를 병기한다.
        #   내장 텍스트층 책: 셀 텍스트를 pdfium으로 추출하되, 위첨자로 판정됐는데
        #     표기에 반영되지 못한 셀이 하나라도 있으면 표를 폐기(PNG 유지).
        #     '내장층은 정밀하다'는 전제가 깨지는 책이 있다 — 대학물리의 내장층은
        #     저품질 선행 OCR이라 '10'이 'IO'로 깨져 지수 이식이 조용히 실패하고,
        #     표 26.2의 철 온도계수가 10^-3에서 10^-8로(10만 배) 나갔다(검토단 실측).
        #   스캔 책: 셀을 두 배율로 OCR해 완전 일치+고신뢰인 셀만 채택하고, 하나라도
        #     불안정하면 표를 폐기(PNG 유지) — 위첨자·범위값 오독으로 틀린 수치를
        #     표로 내보내는 위험을 원천 차단(합의 게이트, 실측 검증).
        # 두 경로가 같은 정책을 갖는다: 못 미더우면 표를 내보내지 않는다.
        table_md = ""
        is_table_region = (fig.get("type") == "TABLE"
                           or _TABLE_CAP_RE.match(fig.get("caption_text", "")))
        if is_table_region and embedded:
            crop = page_image.crop((fig["x0"], fig["y0"], fig["x1"], fig["y1"]))
            page_chars = _page_char_index(textpage, s_pt, page_h_pt)
            unresolved = [0]

            def _cell(box, _f=fig, _u=unresolved):
                l = (_f["x0"] + box[0]) / s_pt
                r = (_f["x0"] + box[2]) / s_pt
                b = page_h_pt - (_f["y0"] + box[3]) / s_pt
                t = page_h_pt - (_f["y0"] + box[1]) / s_pt
                plain = clean_text(textpage.get_text_bounded(
                    left=l, bottom=b, right=r, top=t)).replace("$", r"\$")
                # 위첨자 표식만 이식한다(띄어쓰기는 pdfium 결과를 그대로 신뢰).
                inside = [(ch, fs) for ch, fs, x0, x1, cy in page_chars
                          if l <= (x0 + x1) / 2 <= r and b <= cy <= t]
                _u[0] += superscript_severed(plain, inside)
                return graft_superscripts(plain, inside)

            t_md = pdf_table.extract(crop, _cell)
            if unresolved[0] == 0 and not table_superscript_partial(t_md or ""):
                table_md = _accept_table(t_md)
        elif is_table_region and not embedded:
            crop = page_image.crop((fig["x0"], fig["y0"], fig["x1"], fig["y1"]))
            scan_cell, unreliable = make_scan_cell_fn(crop, tmp_dir, f"tbl{page_no}_{fig_idx}")
            t_md = pdf_table.extract(crop, scan_cell)
            if unreliable[0] == 0:  # 불안정 셀이 하나도 없을 때만 채택
                table_md = _accept_table(t_md)
        flow.append({"btype": "image", "path": path, "caption": f"그림 p{page_no}-{fig_idx}",
                     "caption_text": fig.get("caption_text", ""), "table_md": table_md,
                     "col": fig.get("col", 1), "y0": fig["y0"], "y1": fig["y1"],
                     "x0": fig["x0"], "x1": fig["x1"]})
    flow = order_flow(flow, layout_texts, page_w)

    md = render_flow(flow, page_w)
    return md, n_formulas, source, printed_no


def process_pdf(pdf_path: Path, output_dir: Path) -> Path:
    """PDF 전체를 인식해 Markdown 파일로 저장하고 그 경로를 반환한다.

    결과는 페이지를 처리할 때마다 곧바로 파일에 기록한다(중단 안전, 저메모리).
    그림은 '원본이름_images' 폴더에 저장하고 MD에서 상대 경로로 참조한다.
    """
    import pypdfium2 as pdfium

    total_formulas = 0
    failed_pages = 0
    blank_pages: list[int] = []   # 잉크는 있는데 본문을 못 건진 쪽(조용한 전멸)
    # PDF를 먼저 열어 검증한다 — 손상·암호 PDF는 여기서 예외가 나 출력 파일을 만들기
    # 전에 중단되므로 0바이트 MD 잔해가 남지 않는다(검토단 B3: 잔해가 다음 실행의
    # 이름을 '(1)'로 밀어내 진짜 산출물을 밀쳐내던 문제). 파일 핸들 누수도 없다.
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        num_pages = len(pdf)
        # MD와 그림 폴더 둘 다 비어 있는 이름을 고른다 — MD만 지워진 잔존 폴더에
        # 새 그림이 섞여 들어가는 것을 막는다. 파일은 배타 생성('x')으로 연다 —
        # 동시 이중 실행이 같은 출력에 겹쳐 쓰는 것을 막는다(검토단 지적).
        base = output_dir / f"{pdf_path.stem}_OCR.md"
        out_path, n = base, 0
        out = None
        while out is None:
            if not out_path.with_name(f"{out_path.stem}_images").exists():
                try:
                    out = open(out_path, "x", encoding="utf-8")
                except FileExistsError:
                    pass
            if out is None:
                n += 1
                out_path = base.with_name(f"{base.stem} ({n}){base.suffix}")
        images_dir = out_path.with_name(f"{out_path.stem}_images")

        # dir=tmp_root(): 페이지 이미지 수백 MB가 %TEMP% 가 아니라 도구 폴더 안에
        # 생기게 한다(tmp_root()가 None이면 tempfile 기본값으로 물러선다).
        with tempfile.TemporaryDirectory(dir=tmp_root()) as tmp, out:
            tmp_dir = Path(tmp)
            out.write(f"# {pdf_path.name}\n\n")
            out.write(ai_preamble(pdf_path.name, images_dir.name,
                                  pdf_path.stat().st_size) + "\n\n")
            # 장 구분: 등록된 프로파일이 있으면 머리 목차 + 해당 쪽 앞 장 제목을
            # 자동으로 넣는다(없으면 기존처럼 장 헤딩 없이 진행).
            chapters = pdf_chapters.for_book(pdf_path.stem, num_pages)
            if pdf_chapters.for_book(pdf_path.stem) and not chapters:
                print("  [경고] 등록된 장 구분 프로파일이 이 문서의 쪽 수와 맞지"
                      " 않아 적용하지 않습니다 (같은 파일명의 다른 문서인지"
                      " 확인하세요).")
            toc = pdf_chapters.toc_block(chapters)
            if toc:
                out.write("\n".join(toc) + "\n")
                print(f"  [장구분] 프로파일 적용: {len(chapters)}개 장")
            force_scan = pdf_chapters.force_scan(pdf_path.stem)
            if force_scan:
                print("  [본문] 내장 텍스트 레이어를 신뢰하지 않고 스캔 경로로 강제합니다"
                      " (force_scan)")
            # 선행 파이프라인: 다음 페이지의 레이아웃+수식검출을 현재 페이지의
            # 인식 작업과 겹친다. 렌더링만 메인 스레드(pdfium 제약).
            next_image = None
            next_future = None
            for page_no in range(1, num_pages + 1):
                if page_no in chapters:
                    out.write(f"# {chapters[page_no]}\n\n")
                # 쪽 제목은 인식이 끝난 뒤에 쓴다 — 머리말에서 읽어낸 인쇄
                # 쪽번호를 함께 넣기 위해서다.
                printed = None
                try:
                    page = pdf[page_no - 1]
                    page_image = (next_image if next_image is not None
                                  else page.render(scale=RENDER_DPI / 72).to_pil())
                    pre = None
                    if next_future is not None:
                        try:
                            pre = next_future.result()
                        except Exception:
                            pre = None  # 선계산 실패 → 아래에서 동기 재계산
                    next_image = next_future = None
                    if pre is None:
                        # 프리페치를 던지기 전에 메인에서 동기 계산한다 — 같은 모델
                        # 싱글턴을 두 스레드가 동시에 돌리는 경합(첫 페이지·선계산
                        # 실패 페이지에서 발생)을 차단한다(검토단 지적).
                        pre = precompute_page(page_image)
                    if page_no < num_pages:  # 다음 페이지 몫을 미리 던져 둔다
                        try:
                            next_image = pdf[page_no].render(
                                scale=RENDER_DPI / 72).to_pil()
                            next_future = _PREFETCH_POOL.submit(
                                precompute_page, next_image)
                        except Exception:
                            next_image = next_future = None
                    # 스캔 원본이 기준 해상도보다 높으면 원본 해상도로도 렌더링
                    # (pdfium은 스레드 불안전이므로 메인 스레드에서만 렌더링한다)
                    hires = None
                    ndpi = min(native_scan_dpi(page), HIRES_MAX_DPI)
                    if ndpi > RENDER_DPI:
                        hires = page.render(scale=ndpi / 72).to_pil()
                    page_md, n_formulas, source, printed = process_page(
                        page, page_image, images_dir, page_no, tmp_dir,
                        pre=pre, hires_image=hires, force_scan=force_scan
                    )
                    # 조용한 전멸 방어: 잉크는 있는데 본문이 한 글자도 안 나온
                    # 쪽은 눕힌 스캔일 수 있다(레이아웃이 본문을 그림으로 오분류).
                    # OSD로 세워 한 번만 다시 인식한다.
                    if not body_chars(page_md) and has_ink(page_image):
                        fixed, angle = upright_page(page_image, tmp_dir, page_no)
                        if angle:
                            print(f"    {page_no}페이지: 눕힌 쪽으로 판단해"
                                  f" {angle}도 세워 다시 인식합니다")
                            page_md, n_formulas, source, printed = process_page(
                                page, fixed, images_dir, page_no, tmp_dir,
                                force_scan=force_scan
                            )
                        if not body_chars(page_md):
                            blank_pages.append(page_no)
                except Exception as e:  # 페이지 하나의 실패가 책 전체를 날리지 않도록
                    failed_pages += 1
                    out.write(f"## {page_no}페이지\n\n")
                    out.write(f"(이 페이지는 인식에 실패했습니다: {type(e).__name__})\n\n")
                    out.flush()
                    print(f"    {page_no}/{num_pages}페이지 실패: {type(e).__name__}: {e}")
                    continue
                total_formulas += n_formulas
                out.write(f"## {page_no}페이지"
                          + (f" (인쇄 {printed}쪽)" if printed else "") + "\n\n")
                if page_md:
                    out.write("\n".join(page_md) + "\n")
                out.flush()  # 중단 시에도 여기까지의 결과가 파일에 남는다
                print(
                    f"    {page_no}/{num_pages}페이지 완료"
                    f" (본문: {source}, 수식 {n_formulas}개)"
                )
            # 완료 표식 — 이 줄이 없으면 중단으로 잘린 파일이다(재실행 판단 근거).
            # 수식 수는 '검출' 수이며 문서에 실린 수와 다르다(그림 내부 라벨은
            # 그림 PNG가 이미 담고 있어 본문에서 제외된다) — 오해를 막아 명시한다.
            out.write(f"\n> [변환 완료] {num_pages}페이지, 검출 수식 {total_formulas}개"
                      + (f", 실패 {failed_pages}페이지" if failed_pages else "")
                      + (f", 본문 못 건진 쪽 {len(blank_pages)}개" if blank_pages else "")
                      + "\n")
    finally:
        pdf.close()

    if num_pages and failed_pages == num_pages:
        # 전멸한 산출물이 정식 이름을 차지하면 다음 실행의 완성본이 '(1)'로
        # 밀려나고, 사람도 AI도 잘린 쪽을 먼저 연다(검토단 실증) — 지운다.
        _discard_output(out_path, images_dir)
        raise RuntimeError("모든 페이지가 인식에 실패했습니다")
    if blank_pages:
        head = ", ".join(str(p) for p in blank_pages[:10])
        more = f" 외 {len(blank_pages) - 10}쪽" if len(blank_pages) > 10 else ""
        print(f"  [경고] 내용이 있는데 본문을 못 건진 쪽 {len(blank_pages)}개:"
              f" {head}{more}")
        print("         원본 PDF의 해당 쪽을 확인하세요(눕힌 스캔·특이 레이아웃).")
    print(f"  [저장] {out_path.name} (수식 총 {total_formulas}개)")

    # 자가 감사: 방금 저장한 산출물의 결함(낙오 $·중괄호·깨진 링크·페이지 수)을
    # 인쇄 쪽번호는 쪽마다 독립으로 읽으므로 오탐이 섞인다 — 이웃과 대조해
    # 걸러낸다(파일이 다 쓰인 뒤라야 이웃을 볼 수 있다).
    try:
        n_drop = prune_page_number_outliers(out_path)
        if n_drop:
            print(f"  [쪽번호] 이웃과 어긋나는 인쇄 쪽번호 {n_drop}개를 지웠습니다")
    except Exception as e:
        print(f"  [쪽번호] 검증을 실행하지 못했습니다: {type(e).__name__}")

    # 이 책이 쓰는 용어 목록을 머리말 뒤에 싣는다. 색인은 책 뒤쪽에 있으므로
    # 스트리밍 중에는 만들 수 없다 — 파일이 완성된 뒤 한 번 끼워 넣는다.
    try:
        insert_glossary(out_path)
    except Exception as e:  # 용어 목록 실패가 성공한 변환을 망치면 안 된다
        print(f"  [용어] 목록을 만들지 못했습니다: {type(e).__name__}")

    # 즉시 점검한다. 발견이 있으면 리포트 파일을 MD 옆에 남긴다(없으면 안 남김).
    try:
        import pdf_audit

        summary, report, n_def = pdf_audit.audit_file(out_path)
        print(f"  [감사] {summary}")
        if n_def or "확인 필요" in summary:
            report_path = out_path.with_name(f"{out_path.stem}_감사.txt")
            report_path.write_text(report, encoding="utf-8")
            print(f"  [감사] 상세 리포트: {report_path.name}")
    except Exception as e:  # 감사 실패가 성공한 변환을 실패로 만들면 안 된다
        print(f"  [감사] 감사 자체를 실행하지 못했습니다: {type(e).__name__}")
    return out_path


_sleep_block = None   # (handle, reason_context) — 살려 둬야 사유 문자열이 유효하다


def prevent_sleep(enable: bool) -> None:
    """OCR 중에는 시스템이 절전으로 들어가지 못하게 막는다.

    AC 전원의 절전 대기가 60분으로 켜져 있는데, Windows의 절전 타이머는
    CPU 부하가 아니라 사용자 입력 유휴를 본다. 즉 수천 쪽짜리 무인 OCR도
    키보드를 안 건드리면 그냥 잠들어 버린다.

    화면은 일부러 막지 않는다(PowerRequestSystemRequired만 건다) —
    패널은 꺼지고 변환은 계속된다.

    SetThreadExecutionState가 아니라 PowerSetRequest를 쓰는 이유는
    `powercfg /requests`의 SYSTEM 칸에 아래 사유 문자열까지 찍혀서
    나중에 "무엇이 이 기계를 깨워 두는가"를 감사할 수 있기 때문이다.
    레거시 API는 그 목록에 아예 나타나지 않아 검증이 불가능하다.

    실패해도 변환 자체에는 영향이 없으므로 조용히 넘어간다.
    """
    global _sleep_block
    if os.name != "nt":
        return

    POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x00000001
    # POWER_REQUEST_TYPE: 0=Display 1=System 2=AwayMode 3=Execution
    PowerRequestSystemRequired = 1

    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        if enable:
            if _sleep_block is not None:
                return

            class _Detailed(ctypes.Structure):
                _fields_ = [("LocalizedReasonModule", wintypes.HMODULE),
                            ("LocalizedReasonId", wintypes.ULONG),
                            ("ReasonStringCount", wintypes.ULONG),
                            ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR))]

            class _Reason(ctypes.Union):
                _fields_ = [("Detailed", _Detailed),
                            ("SimpleReasonString", wintypes.LPWSTR)]

            class _ReasonContext(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG),
                            ("Flags", wintypes.DWORD),
                            ("Reason", _Reason)]

            k32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
            k32.PowerCreateRequest.restype = wintypes.HANDLE
            k32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
            k32.PowerSetRequest.restype = wintypes.BOOL

            ctx = _ReasonContext()
            ctx.Version = 0
            ctx.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
            ctx.Reason.SimpleReasonString = "PDF Editor: OCR in progress"

            h = k32.PowerCreateRequest(ctypes.byref(ctx))
            if h and h != -1 and k32.PowerSetRequest(h, PowerRequestSystemRequired):
                _sleep_block = (h, ctx)
                return
            # 최신 API가 안 되면 레거시로 물러선다(감사는 안 되지만 동작은 한다)
            k32.SetThreadExecutionState(0x80000000 | 0x00000001)
        else:
            if _sleep_block is not None:
                h = _sleep_block[0]
                k32.PowerClearRequest(h, PowerRequestSystemRequired)
                k32.CloseHandle(h)
                _sleep_block = None
            else:
                k32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass


def main() -> None:
    try:
        setup_external_tools()
    except RuntimeError as e:
        exit_with_message(str(e))

    try:
        input_dir, output_dir = feature_dirs(FEATURE)
    except OSError as e:
        exit_with_message(
            "입력·출력 폴더를 만들 수 없습니다. 같은 이름의 파일이 자리를 차지하고"
            " 있거나, 폴더에 쓸 권한이 없거나, 디스크가 가득 찼을 수 있습니다.\n"
            f"  {e}")

    # 바로가기에 PDF를 끌어다 놓으면 그 파일들을 바로 처리한다 — 입력 폴더를
    # 찾아 들어가 복사하는 단계가 없어진다. 원본은 읽기만 하므로 그대로 남는다.
    dropped = [Path(a) for a in sys.argv[1:]]
    if dropped:
        pdfs = [p for p in dropped if p.suffix.lower() == ".pdf" and p.is_file()]
        for p in dropped:
            if p not in pdfs:
                why = "PDF가 아닙니다" if p.suffix.lower() != ".pdf" else "파일을 찾을 수 없습니다"
                print(f"[건너뜀] {p.name} — {why}")
        if not pdfs:
            exit_with_message("처리할 PDF가 없습니다. PDF 파일을 끌어다 놓아 주세요.")
        print(f"[끌어다 놓기] {len(pdfs)}개 파일을 바로 처리합니다.\n")
    else:
        pdfs = find_pdfs(input_dir)
        if not pdfs:
            exit_with_message(
                f"입력 폴더에 PDF 파일이 없습니다.\n"
                f"OCR을 적용할 PDF를 여기에 넣어 주세요:\n  {input_dir}\n"
                f"\n또는 바탕화면 'PDF OCR' 바로가기에 PDF를 끌어다 놓으면 바로 변환됩니다."
            )

        # 하위 폴더는 탐색하지 않는다 — 챕터별 폴더를 통째로 끌어다 놓는 실수가
        # 흔하므로 조용히 넘기지 않고 알린다(검토단 지적).
        skipped = find_skipped_subfolders(input_dir)
        if skipped:
            print(f"[알림] PDF가 든 하위 폴더 {len(skipped)}개는 처리하지 않습니다"
                  f" ({', '.join(skipped[:5])}). PDF를 입력 폴더에 직접 놓아 주세요.\n")

    # 이전 실행이 중단된 잔해가 정식 이름을 차지하고 있으면 다음 완성본이
    # '(1)'로 밀려나 사람도 AI도 잘린 파일을 먼저 연다(검토단 실증).
    stale = find_stale_outputs(output_dir)
    if stale:
        print(f"[경고] 완료 표식이 없는(중단된) 산출물 {len(stale)}개가 출력 폴더에"
              f" 있습니다: {', '.join(stale[:5])}")
        print("       지우고 다시 돌리는 것을 권합니다 — 그대로 두면 새 결과가"
              " '이름 (1).md'로 저장됩니다.\n")

    print(f"=== {FEATURE}: {len(pdfs)}개 파일 처리 (본문: 한국어+영어, 수식: LaTeX) ===")
    print("(레이아웃·수식 인식 모델을 로드하는 중입니다...)\n")
    try:
        import pdf_layout
        import pdf_math

        pdf_layout.load_parser()
        pdf_math.load_models()
    except Exception as e:  # 모델 파손 등 RuntimeError 외 오류도 안내로 전환
        exit_with_message(str(e))

    started = time.monotonic()
    ok = 0
    failures: list[str] = []
    try:
        for pdf_path in pdfs:
            try:
                print(f"[파일] {pdf_path.name} ({human_size(pdf_path.stat().st_size)})")
                process_pdf(pdf_path, output_dir)
                ok += 1
            except Exception as e:  # 개별 파일 실패가 전체 작업을 멈추지 않도록
                print(f"  [실패] {type(e).__name__}: {e}")
                failures.append(pdf_path.name)
            print()
    except KeyboardInterrupt:
        # 스레드풀 종료 대기(atexit join)로 창이 수 분 멈추는 것을 피하고 즉시 끝낸다.
        # 지금까지의 페이지는 이미 파일에 기록되어 있다(페이지별 flush).
        print("\n[중단] 사용자가 중단했습니다 — 지금까지의 결과는 파일에 남아 있습니다.")
        _TESS_POOL.shutdown(wait=False, cancel_futures=True)
        _PREFETCH_POOL.shutdown(wait=False, cancel_futures=True)
        os._exit(130)

    print(f"=== 완료: Markdown {ok}개 저장 → {output_dir} ===")

    # 결과를 보러 폴더를 찾아 들어가지 않아도 되도록 열어 준다.
    # 실패해도 변환 자체는 끝났으므로 조용히 넘긴다(원격·무인 실행 대비).
    if ok:
        try:
            os.startfile(output_dir)
        except Exception:
            pass

    # 오래 걸린 작업은 자리를 비우게 된다 — 끝났음을 소리로 알린다.
    # 짧은 변환까지 울리면 성가시므로 1분을 넘긴 경우만.
    if time.monotonic() - started > 60:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    if failures:
        print(f"처리하지 못한 파일 {len(failures)}개: {', '.join(failures)}")
        sys.exit(1)


if __name__ == "__main__":
    prevent_sleep(True)
    try:
        main()
    finally:
        prevent_sleep(False)
