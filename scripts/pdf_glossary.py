# -*- coding: utf-8 -*-
"""이 책이 실제로 쓰는 용어 목록을 만든다 — AI의 조용한 검색 실패를 막는다.

문제: 산출물은 원문 표기를 그대로 보존하는데, 표준 용어와 교재 표기가 다르다.
`전기장`으로 grep하면 0건이고 그 책은 `전계`라 부른다(201쪽). AI는 '교재에
없다'와 '교재가 다르게 부른다'를 구별할 수 없어, 있는 내용을 없다고 답한다.
실측 검색 실패율 21.6%(표준 용어 199개 중 43개가 0건).

해결: 교재 뒤쪽 찾아보기(색인)에서 용어를 뽑아 본문에 실재하는 것만 남기고
MD 머리에 싣는다. AI가 목록을 보면 그 책의 어휘로 다시 검색할 수 있고,
목록에 없으면 '이 책이 다루지 않는다'는 참인 부정으로 판단할 수 있다.

색인 형식은 책마다 다르므로(공백 구분·별표 구분) 여러 패턴을 시도하고,
본문 검증을 통과한 용어만 채택한다 — 못 뽑으면 목록 없이 진행한다.
"""
import re
from collections import Counter

# 색인은 줄 단위가 아니라 '용어 쪽번호 용어 쪽번호 …'가 한 문단으로 이어붙어
# OCR된다('대역 차단 678 대역 통과 필터 676 대역폭 669'). 그래서 줄 파서가 아니라
# 짝의 스트림으로 훑는다 — finditer가 겹치지 않게 순서대로 잘라 준다.
_INDEX_PAIR = re.compile(
    r"(?P<term>[가-힣A-Za-z][^0-9\n]{0,26}?)"
    r"\s*(?P<pages>\d{1,4}(?:\s*[,~\-]\s*\d{1,4})*)")
# 색인 쪽 판정: 한 쪽에서 이만큼 짝이 나오면 색인으로 본다(본문은 이 밀도가 안 됨).
_INDEX_MIN_HITS = 25
_PAGE_SEC = re.compile(r"(?m)^## (\d+)페이지(?: \(인쇄 [^)]*\))?$")
_STOP = re.compile(r"^(?:그림|표|예제|문제|연습|장|절|페이지|참고|부록)\b")
_SENT_END = re.compile(r"(?:다|라|자|요)[.。]")
GLOSSARY_MAX = 400          # 머리말이 지나치게 길어지지 않게 상한을 둔다


def _sections(md_text: str):
    marks = [(int(m.group(1)), m.start()) for m in _PAGE_SEC.finditer(md_text)]
    for i, (pno, s) in enumerate(marks):
        e = marks[i + 1][1] if i + 1 < len(marks) else len(md_text)
        yield pno, md_text[s:e]


def _strip_noise(sec: str) -> str:
    """그림 링크·수식·표·헤딩을 뺀다.

    그림 링크('![그림 p618-1](대학수학_OCR_images/p618_fig1.png)')는 그 자체로
    '글자 + 숫자' 짝을 대량 만들어 본문 쪽을 색인으로 오분류시킨다(실측: 상위
    용어에 '대학수학_OCR_images/p'가 올라왔다).
    """
    sec = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", sec)
    sec = re.sub(r"\$\$.+?\$\$|(?<!\$)\$[^$\n]+?\$(?!\$)", " ", sec, flags=re.S)
    return re.sub(r"(?m)^[#>|*].*$", " ", sec)


def _page_pairs(sec: str) -> list[str]:
    """한 쪽에서 '용어 쪽번호' 짝의 용어부만 뽑는다."""
    return [m.group("term") for m in _INDEX_PAIR.finditer(_strip_noise(sec))]


def _is_index_page(sec: str) -> bool:
    """색인 쪽인가 — 짝이 촘촘하고 완결된 문장이 거의 없어야 한다.

    색인은 '용어 쪽번호'의 나열이라 문장 종결('…다.', '…라.')이 사실상 없다.
    본문은 짝이 우연히 많아도 종결어미가 즐비하다 — 이것이 가장 확실한 구분이다.
    """
    clean = _strip_noise(sec)
    if len(_INDEX_PAIR.findall(clean)) < _INDEX_MIN_HITS:
        return False
    return len(_SENT_END.findall(clean)) <= 2


def index_terms(md_text: str) -> list[str]:
    """찾아보기 쪽에서 용어 후보를 뽑는다(중복 제거, 등장 순서 유지)."""
    seen: dict[str, None] = {}
    for _pno, sec in _sections(md_text):
        if not _is_index_page(sec):
            continue
        for t in _page_pairs(sec):
            t = re.sub(r"\s{2,}", " ", t).strip(" ·-–—|{}()[]")
            # 한글이 두 자 이상 든 용어만 — OCR 잡음('rome te | {| de mm')을 뺀다.
            if len(t) < 2 or len(re.findall(r"[가-힣]", t)) < 2:
                continue
            if _STOP.match(t):
                continue
            seen.setdefault(t, None)
    return list(seen)


def build(md_text: str, min_body_hits: int = 2) -> list[str]:
    """본문에 실제로 쓰이는 용어만 남긴 목록.

    색인에만 있고 본문에 없는 항목(OCR로 깨진 색인 줄)을 걸러내기 위해
    본문 등장 횟수를 센다. 색인 구간 자체는 세지 않는다.
    """
    terms = index_terms(md_text)
    if not terms:
        return []
    body = "\n".join(sec for _p, sec in _sections(md_text)
                     if not _is_index_page(sec))
    counts = Counter()
    for t in terms:
        # 띄어쓰기 변이를 함께 인정한다('발산 정리'/'발산정리').
        pat = re.escape(t).replace(r"\ ", r"\s*")
        counts[t] = len(re.findall(pat, body))
    kept = [t for t in terms if counts[t] >= min_body_hits]
    kept.sort(key=lambda t: (-counts[t], t))
    return kept[:GLOSSARY_MAX]


def block(terms: list[str]) -> list[str]:
    """머리말에 넣을 줄 목록(빈 목록이면 빈 리스트)."""
    if not terms:
        return []
    return [
        "## 이 책이 쓰는 용어",
        "",
        "> 아래는 이 책의 찾아보기에서 뽑아 본문 등장을 확인한 표기다. 검색이"
        " 0건이면 표준 용어가 아니라 **이 책의 표기**로 다시 찾아보라"
        " (예: 전기장→전계, 테브난→테브냉, 라플라스 변환→Laplace 변환)."
        " 여기에도 없으면 이 책이 그 주제를 다루지 않는 것이다.",
        "",
        ", ".join(sorted(terms)),
        "",
    ]
