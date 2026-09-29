"""페이지 → Markdown — 읽기 순서로 정렬된 블록을 Markdown 줄로 만든다.

그림·표 영역을 PNG로 저장해 링크하고, 절 표지(예제·정리…)를 헤딩으로 올리고, 떨어진
캡션을 그림에 붙인다. 문서 머리의 AI 안내문과 '본문이 비었는가' 판정도 여기 있다.

pdf_ocr에서 떼어 낸 모듈이다 — pdf_ocr가 이름을 재수출하므로 pdf_ocr.X 호출은 그대로 된다.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

import tuning
from pdf_embedded import _is_sane_char
from pdf_text import join_lines, _MATH_SPAN, _squash


FIG_MARGIN = tuning.get("figure", "margin")        # 그림을 잘라낼 때 사방 여백(픽셀)
FIG_MAX_PX = tuning.get("figure", "max_px")        # 저장 그림의 최장변 상한
FIG_COLORS = tuning.get("figure", "colors")        # 양자화 색 수(용량 약 절반)
PARA_JOIN_MARGIN = tuning.get("layout", "para_join_margin")        # 문단 병합 여유


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
    word = _squash(m.group(1))
    num = _squash(m.group(2) or "")
    rest = text[m.end():].strip()
    # 같은 표지어가 뒤에 또 나오면 색인·목록 줄이다(헤딩 아님)
    if _squash(rest).count(word) >= 1:
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
            paragraph = join_lines([paragraph, body])
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
