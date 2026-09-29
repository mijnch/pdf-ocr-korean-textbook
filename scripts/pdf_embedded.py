"""내장 텍스트층 해석 — 쓸 만한 층인지 보고, 줄을 복원하고, 위첨자를 되살린다.

출판사가 입힌 텍스트층(pdfium textpage)에서 영역별 줄을 뽑고, 글자 크기로 지수(10⁻³ 등)를
찾아 ^{…}로 이식하며, 지수 이식이 표 안에서 절반만 된 경우를 가려낸다. 층 전체를 믿을지
Tesseract와 맞대어 재는 판정(embedded_layer_agreement)은 인식 풀을 쓰므로 pdf_ocr에 있다.

pdf_ocr에서 떼어 낸 모듈이다 — pdf_ocr가 이름을 재수출하므로 pdf_ocr.X 호출은 그대로 된다.
"""

from __future__ import annotations

import re

import tuning
from pdf_text import RENDER_DPI


MIN_SANE_CHAR_RATIO = tuning.get("layout", "min_sane_char_ratio")  # 정상 글자율 하한


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


def _page_char_index(textpage):
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
