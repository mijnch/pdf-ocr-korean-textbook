"""산출물 자가 감사 — 변환 직후 MD의 결함을 자동 점검한다.

pdf_ocr가 책 하나를 저장한 직후 자동 호출하고, 기존 MD에 단독 실행도 된다:
  python scripts\\pdf_audit.py <MD 경로> [...]

점검 항목(전부 과거 전수 감사에서 실증된 결함 클래스):
  결함  - 낙오 $      : 이스케이프(\\$)를 뺀 $ 개수가 홀수인 줄 — 구분자 짝 붕괴
        - 중괄호 불균형: 수식 스팬 안에서 여닫이가 안 맞는 것 ($ 짝이 성한 줄만 검사)
        - 깨진 그림 링크: MD가 가리키는 PNG가 없는 것
        - 페이지 수 불일치: '## N페이지' 절 수가 완료 표식의 페이지 수와 다름
        - 완료 표식 없음: 중단으로 잘린 파일
  의심  - 퇴화 반복    : 같은 토큰 10연속·구절 3연속 반복(정당한 행렬 행도 걸리므로
                        결함이 아닌 '육안 확인 필요'로 분류. 열 지정자 {c c c}는 제외)
        - 고아 PNG    : images 폴더에 있는데 MD가 참조하지 않는 파일

감사 교훈의 코드화(과거 검사기의 함정 재발 방지):
  - $ 짝 검사 전에 반드시 \\$ 를 제거한다(본문 이스케이프가 짝으로 오인됨).
  - $ 짝이 홀수인 줄은 중괄호·퇴화 검사를 건너뛴다(스팬 어긋남의 연쇄 허위양성).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ESCAPED = re.compile(r"\\\$")
_BLOCK_SPAN = re.compile(r"\$\$([^\n$]+?)\$\$")   # 블록 수식은 항상 한 줄(산출 규약)
_INLINE_SPAN = re.compile(r"\$([^$\n]+?)\$")
# 그림 링크. 경로에 괄호가 있으면 <...> 형식으로 감싸므로 두 형태를 모두 받는다.
_FIG_LINK = re.compile(r"!\[[^\]]*\]\((?:<([^>]+)>|([^)\n]+))\)")


def fig_links(md_text: str) -> list[str]:
    """MD의 그림 링크 경로 목록(각괄호 형식 해제 포함)."""
    return [a or b for a, b in _FIG_LINK.findall(md_text)]


def render_broken_links(md_text: str) -> list[str]:
    """CommonMark에서 렌더되지 않는 그림 링크(무포장 경로에 공백 포함)를 찾는다.

    ![..](경로)의 무포장 경로는 CommonMark가 공백·괄호에서 끊는다 — 파일이 실재해도
    이미지로 표시되지 않는다. <경로> 형식이면 공백·괄호가 있어도 안전하다. 파일
    존재만 보던 기존 감사(그리고 검토단 1)가 '결함 0'이라 했으나, CommonMark 파서
    기준(검토단 5)으로는 깨진 링크였다 — 이 함수가 그 층을 메운다.
    """
    return [m.group(2) for m in _FIG_LINK.finditer(md_text)
            if m.group(2) is not None and re.search(r"[()\s]", m.group(2))]
# 쪽 제목에는 인쇄 쪽번호가 뒤따를 수 있다("## 400페이지 (인쇄 392쪽)").
_PAGE_HDR = re.compile(r"^## (\d+)페이지(?: \(인쇄 [^)]*\))?$", re.M)
# 표식 문구는 바뀔 수 있다('수식' → '검출 수식', 뒤에 '본문 못 건진 쪽 N개' 추가).
# 쪽 수와 실패 쪽 수만 확실히 집어내고 사이 문구는 느슨하게 받는다 — 문구를 고칠
# 때마다 감사가 '완료 표식 없음'을 오탐하던 것을 막는다.
_DONE = re.compile(
    r"^> \[변환 완료\] (\d+)페이지,[^\n]*?(?:, 실패 (\d+)페이지)?(?:,[^\n]*)?$", re.M)
_COLSPEC = re.compile(r"\{\s*\|?\s*[crl](?:\s*\|?\s*[crl]){2,}\s*\|?\s*\}")
# 감사용 퇴화 판정 임계. pdf_latex의 정리용 _TOKEN_RUN(\\cmd 8연발)과 이름이 겹치면
# 안 된다 — 같은 이름·다른 값은 grep으로 한쪽만 고치는 지뢰다(검토단 S4). 여기 감사
# 임계(10연발)는 정리 임계(8연발)보다 느슨해야 정리 후 잔존만 잡아 헛울지 않는다.
_AUDIT_TOKEN_RUN = re.compile(r"(\S{1,8})(?:\s*\1){9,}")       # 같은 토큰 10연속
_AUDIT_PHRASE_RUN = re.compile(r"(?:^|\s)((?:\S+\s+){3,15}\S+)(?:\s+\1){2,}(?:\s|$)")


def _brace_ok(span: str) -> bool:
    """수식 스팬의 중괄호 균형. \\{ \\} \\\\ 는 세지 않는다."""
    depth = 0
    i, n = 0, len(span)
    while i < n:
        c = span[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return False          # 짝 없는 닫기
            depth -= 1
        i += 1
    return depth == 0


def _snip(line: str, limit: int = 80) -> str:
    line = line.strip()
    return line if len(line) <= limit else line[:limit] + "…"


def audit_text(md_text: str) -> dict:
    """MD 본문 문자열을 감사해 항목별 발견 목록을 반환한다(파일 접근 없음)."""
    stray: list[tuple[int, str]] = []
    unbalanced: list[tuple[int, str]] = []
    degenerate: list[tuple[int, str]] = []

    for no, line in enumerate(md_text.splitlines(), 1):
        bare = _ESCAPED.sub("", line)           # \$ 는 구분자가 아니다 — 먼저 제거
        if "$" not in bare:
            continue
        if bare.count("$") % 2:                 # 낙오 $ — 이후 검사는 무의미(연쇄 오인)
            stray.append((no, _snip(line)))
            continue
        spans = _BLOCK_SPAN.findall(bare)
        spans += _INLINE_SPAN.findall(_BLOCK_SPAN.sub(" ", bare))
        for s in spans:
            if not _brace_ok(s):
                unbalanced.append((no, _snip(s)))
            core = " ".join(_COLSPEC.sub(" ", s).split())
            if _AUDIT_TOKEN_RUN.search(core) or _AUDIT_PHRASE_RUN.search(core):
                degenerate.append((no, _snip(s)))

    m = _DONE.search(md_text)
    pages = _PAGE_HDR.findall(md_text)
    return {
        "낙오달러": stray,
        "중괄호불균형": unbalanced,
        "퇴화반복": degenerate,
        "완료표식": (int(m.group(1)), int(m.group(2) or 0)) if m else None,
        "페이지절수": len(pages),
        "실패페이지수": md_text.count("(이 페이지는 인식에 실패했습니다"),
    }


def count_spans(md_text: str) -> tuple[int, int]:
    """(페이지 절 수, 수식 스팬 수)를 실측한다. $ 짝이 깨진 줄의 스팬은 세지 않는다."""
    n_f = 0
    for line in md_text.splitlines():
        bare = _ESCAPED.sub("", line)
        if "$" not in bare or bare.count("$") % 2:
            continue
        n_f += len(_BLOCK_SPAN.findall(bare))
        n_f += len(_INLINE_SPAN.findall(_BLOCK_SPAN.sub(" ", bare)))
    return len(_PAGE_HDR.findall(md_text)), n_f


def recount_marker(md_text: str) -> str:
    """완료 표식의 페이지·수식 수를 파일 실측값으로 갱신해 돌려준다.

    표식이 없으면 끝에 새로 붙인다. 실패 페이지 수도 본문 실측으로 다시 센다.

    라벨이 '검출 수식'이 아니라 '수식'인 이유: 검출 수(MFD가 찾은 상자 수)와
    수록 수(최종 문서에 남은 수식 수)는 다르다 — 빈 LaTeX·그림 안 라벨은
    탈락하기 때문이다(실측 전기회로이론: 검출 24,340 vs 수록 14,105). 예전에는
    변환이 검출 수를, 스플라이스가 수록 수를 **같은 라벨에** 적어, 스플라이스를
    한 번 돌리면 표식의 뜻이 조용히 바뀌었다. 지금은 양쪽 다 이 함수를 거쳐
    파일에 실제로 든 수를 적는다 — 누구든 세어서 확인할 수 있는 값이다.
    """
    pages, n_f = count_spans(md_text)
    failed = md_text.count("(이 페이지는 인식에 실패했습니다")
    marker = (f"> [변환 완료] {pages}페이지, 수식 {n_f}개"
              + (f", 실패 {failed}페이지" if failed else ""))
    if _DONE.search(md_text):
        return _DONE.sub(lambda _m: marker, md_text, count=1)
    return md_text.rstrip("\n") + f"\n\n{marker}\n"


_PAGE_SEC = re.compile(r"(?m)^## (\d+)페이지(?: \(인쇄 [^)]*\))?$")
_SKIP_LINE = ("!", "|", "#", ">", "$$", "*")
_MATH = re.compile(r"\$\$.+?\$\$|(?<!\$)\$[^$\n]+?\$(?!\$)", re.S)
_HANGUL_ONLY = re.compile(r"[가-힣]")   # 공백을 넣으면 공백 창이 반복으로 잡힌다
_SUP_OK = re.compile(r"10\^\{")
_SUP_FLAT = re.compile(r"[Xx×]\s*10[-−]?\d")
_BRACKETS = re.compile(r"\\left|\\right")


def _page_sections(md_text: str):
    marks = [(int(m.group(1)), m.start()) for m in _PAGE_SEC.finditer(md_text)]
    for i, (pno, s) in enumerate(marks):
        e = marks[i + 1][1] if i + 1 < len(marks) else len(md_text)
        yield pno, md_text[s:e]


def empty_pages(md_text: str, min_chars: int = 10) -> list[str]:
    """본문이 사실상 비어 있는 쪽 — 눕힌 스캔·레이아웃 오분류의 조용한 신호.

    그림·표만 있는 쪽도 잡힌다(참조표 쪽 등 정당한 경우가 있어 결함으로 올리되
    쪽 번호를 함께 준다). 검토단이 만든 '300쪽 전부 빈 책'이 결함 0건을 받은
    구멍을 막는 항목이다.
    """
    out = []
    for pno, sec in _page_sections(md_text):
        body = 0
        for line in sec.split("\n")[1:]:
            s = line.strip()
            if not s or s.startswith(_SKIP_LINE):
                continue
            body += len(re.sub(r"\s", "", _MATH.sub(" ", s)))
        if body < min_chars:
            out.append(f"{pno}페이지 (본문 {body}자)")
    return out


def flat_exponent_cells(md_text: str) -> list[str]:
    """표 안에서 지수 복원이 반쪽만 된 쪽 — 값이 자릿수 단위로 틀린다.

    같은 표에 '10^{24}'와 'X 1025'가 함께 있으면 후자는 복원 실패가 확실하다.
    파이프라인이 이런 표를 폐기하도록 고쳤으나(pdf_ocr), 이미 만들어진 산출물과
    앞으로 새로 생길 변형을 잡기 위해 감사에도 둔다.
    """
    out = []
    for pno, sec in _page_sections(md_text):
        rows = "\n".join(l for l in sec.split("\n") if l.startswith("|"))
        if rows and _SUP_OK.search(rows) and _SUP_FLAT.search(rows):
            out.append(f"{pno}페이지")
    return out


_EX_HEAD = re.compile(r"(?m)^### (예제|문제|연습문제)\s*[0-9]")
_CONTINUATION = re.compile(r"^(?:\$\$|결론|분석|따라서|그러므로|즉\b|이때\b)")


def orphan_example_headings(md_text: str) -> list[tuple[int, str]]:
    """예제 헤딩 바로 뒤에 문제 진술 없이 앞 예제의 꼬리가 오는 곳.

    2단 예제 박스에서 헤딩이 한 블록 일찍 나오면, 앞 예제의 결론·수식이 새
    헤딩 아래로 들어간다 — AI는 헤딩을 믿고 '예제 5.4의 풀이'라며 5.3의
    풀이를 답한다(검토단 실측 대학물리 10건). 헤딩을 옮기는 자동 수정은
    3,729개 헤딩 전체를 건드려 위험이 크므로, 사람이 확인하도록 드러낸다.
    """
    lines = md_text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if not _EX_HEAD.match(line):
            continue
        nxt = next((l.strip() for l in lines[i + 1:i + 5] if l.strip()), "")
        if nxt and _CONTINUATION.match(nxt):
            out.append((i + 1, f"{line[:40]} ← {nxt[:44]}"))
    return out


def bracket_imbalance(md_text: str) -> list[tuple[int, str]]:
    """수식 스팬의 대괄호·\\left/\\right 짝이 맞지 않는 곳(중괄호는 별도 항목).

    '결함'이 아니라 '확인 필요'로 다룬다 — 스캔본에서 낱개 ']'는 OCR 잡음으로
    흔하고(5권 실측 2,400여 건) 대개 문맥으로 읽힌다. 짝이 깨진 채 렌더가
    막히는 진짜 사례를 사람이 훑어보게 하는 목록이다.
    """
    out = []
    for i, line in enumerate(md_text.split("\n"), 1):
        for m in _MATH.finditer(line):
            s = m.group(0)
            # \right는 낱말 경계를 봐야 한다 — \rightsquigarrow·\rightarrow가
            # 걸려 들면 괄호가 없는 수식까지 불균형으로 잡힌다(실측 오탐 다수).
            lefts = len(re.findall(r"\\left(?![A-Za-z])", s))
            rights = len(re.findall(r"\\right(?![A-Za-z])", s))
            if s.count("[") != s.count("]") or lefts != rights:
                out.append((i, s[:70]))
                break
    return out


def duplicate_runs(md_text: str, win: int = 14) -> list[tuple[int, str]]:
    """한 줄 안에서 같은 한글 토막이 그대로 두 번 나오는 곳 — 중복 삽입 의심.

    두 해상도·두 PSM의 판독을 합칠 때 같은 문장이 서로 다르게 깨진 채 두 번
    들어가는 일이 있다(검토단 실측 246줄). 겹치는 창을 훑어 이미 본 토막이
    다시 나오면 반복이다 — 반복 없는 글에서는 모든 창이 서로 다르다.

    한글 창만 센다. 라틴 글자로 확장해 봤더니 영문 교재에서 목차·본문의 정당한
    반복이 대량으로 걸렸다(창 14자 611건, 30자로 늘려도 35건이 모두 오탐 —
    'Representation of Discrete-Tim'은 목차에 두 번 나오는 진짜 장 제목이다).
    한글은 같은 길이의 창에 담기는 정보가 훨씬 많아 우연한 반복이 사실상 없다.
    그래서 이 점검은 한글 문서 전용이며, 리포트가 그 범위를 함께 밝힌다 —
    영문 문서에서 0건은 '중복이 없다'가 아니라 '이 점검이 보지 못한다'는 뜻이다.
    (예방은 언어와 무관하게 작동한다 — pdf_text.merge_rescue_lines 의 메아리
    판정은 라틴 글자도 함께 센다.)
    """
    out = []
    for i, line in enumerate(md_text.split("\n"), 1):
        if line.startswith(_SKIP_LINE) or len(line) < win * 2:
            continue
        s = _MATH.sub(" ", line)
        seen: set[str] = set()
        for j in range(len(s) - win + 1):
            frag = s[j:j + win]
            if len(_HANGUL_ONLY.findall(frag)) < win - 3:
                continue                      # 한글이 옅은 토막(수식·공백)은 건너뜀
            if frag in seen:
                out.append((i, frag))
                break
            seen.add(frag)
    return out


def audit_file(md_path: Path) -> tuple[str, str, int]:
    """MD 파일 하나를 감사한다. 반환: (한 줄 요약, 상세 리포트, 결함 수)."""
    md_text = md_path.read_text(encoding="utf-8")
    r = audit_text(md_text)

    links = fig_links(md_text)
    broken = [l for l in links if not (md_path.parent / l).exists()]
    render_broken = render_broken_links(md_text)   # 파일은 있으나 CommonMark 미표시
    images_dir = md_path.with_name(f"{md_path.stem}_images")
    referenced = {Path(l).name for l in links}
    orphans = ([p.name for p in images_dir.iterdir() if p.name not in referenced]
               if images_dir.is_dir() else [])

    defects: list[str] = []
    suspects: list[str] = []

    def _list(title: str, items, bucket: list[str], show: int = 5) -> None:
        if not items:
            return
        bucket.append(f"■ {title}: {len(items)}건")
        for it in items[:show]:
            bucket.append(f"    {it[0]}행: {it[1]}" if isinstance(it, tuple)
                          else f"    {it}")
        if len(items) > show:
            bucket.append(f"    … 외 {len(items) - show}건")

    _list("낙오 $ (구분자 짝 붕괴)", r["낙오달러"], defects)
    _list("수식 중괄호 불균형", r["중괄호불균형"], defects)
    _list("깨진 그림 링크(파일 없음)", broken, defects)
    _list("렌더 안 되는 그림 링크(<> 없이 공백·괄호 — CommonMark 미표시)",
          render_broken, defects)
    if r["완료표식"] is None:
        defects.append("■ 완료 표식 없음 — 중단으로 잘린 파일")
    elif r["완료표식"][0] != r["페이지절수"]:
        defects.append(f"■ 페이지 수 불일치: 절 {r['페이지절수']}개 ≠ "
                       f"표식 {r['완료표식'][0]}페이지")
    if r["실패페이지수"]:
        defects.append(f"■ 인식 실패 페이지: {r['실패페이지수']}건")
    # ── 내용 쪽 점검 ──────────────────────────────────────────────
    # 위 항목은 전부 '구문'이다. 검토단 실증: 300쪽 전부 빈 책도, 표 셀의 지수가
    # 통째로 날아간 책도 구문상 완벽해서 '결함 0건'을 받았다. 원본과 대조하는
    # 감사는 도구 안에서 불가능하지만, 원본을 몰라도 알 수 있는 결함은 잡는다.
    empty = empty_pages(md_text)
    dupes = duplicate_runs(md_text)
    orphan_ex = orphan_example_headings(md_text)
    flat = flat_exponent_cells(md_text)
    brackets = bracket_imbalance(md_text)
    _list("본문이 비어 있는 쪽(그림·표만 있거나 인식 실패)", empty, defects)
    _list("표 셀의 지수가 평문으로 남음(같은 표에 ^{ }가 함께 있음 — 값이"
          " 자릿수 단위로 틀림)", flat, defects)
    # 아래 둘은 결함이 아니라 확인 항목이다 — 스캔 잡음으로 흔해 결함으로
    # 세면 실제 결함이 숫자에 묻힌다(실측: 대괄호 2,400여 건, 중복 1,500여 건).
    _list("수식 대괄호·\\left짝 불균형(스캔 잡음일 수 있음 — 육안 확인)",
          brackets, suspects)
    # 이 점검은 한글 전용이다(사유는 duplicate_runs 참조). 한글이 옅은 문서에서
    # 0건은 '중복이 없다'가 아니라 '보지 못한다'는 뜻이므로 그렇게 적는다.
    if len(_HANGUL_ONLY.findall(md_text)) < 0.05 * len(md_text):
        suspects.append("■ 중복 삽입 점검: 이 문서는 한글이 옅어 건너뛴다"
                        " (이 점검은 한글 전용 — 0건이 '중복 없음'을 뜻하지 않는다)")
    else:
        _list("같은 줄에서 긴 한글 토막이 반복(중복 삽입 의심)", dupes, suspects)
    _list("예제 헤딩 뒤에 문제 진술 없이 앞 예제의 꼬리가 옴"
          " — 2단 배치 순서 뒤바뀜(육안 확인)", orphan_ex, suspects)
    _list("퇴화 반복 의심 수식(정당한 행렬 반복일 수 있음 — 육안 확인)",
          r["퇴화반복"], suspects)
    _list("고아 PNG(MD가 참조하지 않음)", orphans, suspects)

    n_def = (len(r["낙오달러"]) + len(r["중괄호불균형"]) + len(broken)
             + len(render_broken) + len(empty) + len(flat)
             + (0 if r["완료표식"] and r["완료표식"][0] == r["페이지절수"] else 1)
             + r["실패페이지수"])
    n_sus = (len(r["퇴화반복"]) + len(orphans) + len(dupes) + len(brackets)
             + len(orphan_ex))
    summary = (f"결함 {n_def}건"
               + (f", 확인 필요 {n_sus}건" if n_sus else "")
               + f" (페이지 {r['페이지절수']}, 그림 링크 {len(links)})")
    # '결함 없음'은 내용이 정확하다는 뜻으로 읽힌다 — 이 감사는 원본 PDF와 한
    # 번도 대조하지 않으므로 범위를 문구로 못 박는다(검토단 5인 전원 지적).
    report = "\n".join(
        [f"[산출물 자가 감사 — 기계 점검만. 원본 PDF와 대조하지 않으므로"
         f" 내용 정확성은 보증하지 않는다] {md_path.name}", summary, ""]
        + (defects or ["기계 점검 항목 전부 통과 (원문 충실도는 미검증)"])
        + [""] + (suspects or []))
    return summary, report.rstrip() + "\n", n_def


def main() -> None:
    # 콘솔이 cp949여도 리포트의 '—' 등 비-cp949 문자로 죽지 않게 UTF-8로 고정한다
    # (검토단 5 실측: 발견이 있을 때만 UnicodeEncodeError로 죽던 최악의 실패 모드).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) < 2:
        sys.exit("사용법: python pdf_audit.py <MD 경로> [...]")
    worst = 0
    for arg in sys.argv[1:]:
        _, report, n_def = audit_file(Path(arg))
        print(report)
        worst = max(worst, n_def)
    sys.exit(1 if worst else 0)


if __name__ == "__main__":
    main()
