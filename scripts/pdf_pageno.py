"""인쇄 쪽번호 — 쪽마다 머리말 띠에서 읽고, 책이 다 모인 뒤 이웃 쪽과 대조해 확정한다.

쪽마다 독립으로 읽은 번호에는 장 번호·본문 숫자가 섞인다. 되풀이되는 번호를 거르고,
이웃 중앙값과 어긋난 값을 지우고, 최장 증가 부분열만 남긴 뒤, 오프셋이 같은 두 확정값
사이를 채운다. 확인 못 한 쪽번호는 없는 것보다 나쁘기 때문이다(README '설계에서 배운 것').

pdf_ocr에서 떼어 낸 모듈이다 — pdf_ocr가 이름을 재수출하므로 pdf_ocr.X 호출은 그대로 된다.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from pathlib import Path

import pdf_text


_PAGE_HEAD_NO = re.compile(r"(?m)^## (\d+)페이지 \(인쇄 (\d+)쪽\)$")
_PAGE_HEAD_ANY = re.compile(r"(?m)^## (\d+)페이지(?: \(인쇄 (\d+)쪽\))?$")
PAGE_NO_WINDOW = 10      # 이웃 판정 창(앞뒤 쪽 수)
PAGE_NO_MIN_VOTES = 3    # 창 안에 이만큼 있어야 판정한다
PAGE_NO_TOL = 1          # 이웃 중앙값과 이만큼 넘게 어긋나면 오탐
PAGE_NO_FILL_GAP = 20    # 오프셋이 같은 두 확정값 사이가 이 폭 이하면 메운다
PAGE_NO_MAX_REPEAT = 3   # 같은 인쇄 번호가 이만큼의 쪽에 나오면 쪽번호가 아니다


def split_repeated_page_numbers(pairs: list[tuple[int, int]]):
    """(쪽마다 하나뿐인 값, 여러 쪽에 되풀이되는 값)으로 가른다.

    장 번호를 쪽번호로 읽으면 그 장의 모든 쪽이 같은 값('1')을 받는다. 그러면
    오프셋이 한 칸씩 늘어나는 그 값들이 서로의 이웃 검증을 통과시켜 준다 — 실측
    Floyd(쪽번호가 인쇄되지 않은 판본)에서 1장 내내 '인쇄 1쪽'이 붙었고, 이웃
    대조 뒤에도 틀린 값 4개가 남았다. 그래서 되풀이 값은 이웃 대조에서 빼 둔다.
    """
    from collections import Counter

    seen = Counter(n for _p, n in pairs)
    return ([(p, n) for p, n in pairs if seen[n] < PAGE_NO_MAX_REPEAT],
            [(p, n) for p, n in pairs if seen[n] >= PAGE_NO_MAX_REPEAT])


def agreeing_page_numbers(cands: list[tuple[int, int]],
                          anchors: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """후보 중 그 자리의 확정된 이웃(anchors) 오프셋과 맞는 것만 돌려준다.

    되풀이 값에도 진짜가 하나 섞여 있다 — 1장 첫 쪽의 '1'은 목차·장 표지에서도
    '1'을 읽는 바람에 되풀이 값이 된다(실측 응용수학 PDF 16쪽 '1', 17쪽 '2').
    확정된 이웃과 오프셋이 맞으면 되살린다. 이웃이 없으면(Floyd) 되살지 않는다.
    """
    offs = dict((p, p - n) for p, n in anchors)
    out = []
    for p, n in cands:
        near = sorted(o for q, o in offs.items() if abs(q - p) <= PAGE_NO_WINDOW)
        if (len(near) >= PAGE_NO_MIN_VOTES
                and abs((p - n) - near[len(near) // 2]) <= PAGE_NO_TOL):
            out.append((p, n))
    return out


def settle_pairs(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(PDF쪽, 인쇄쪽) 후보를 확정한다 — settle_page_numbers의 순수한 몸통.

    유일한 값끼리 이웃 대조 → 되풀이 값은 확정된 이웃과 맞을 때만 되살림 →
    증가하는 최장 부분열 → 두 앵커가 증명하는 빈칸 채우기.
    """
    unique, repeated = split_repeated_page_numbers(pairs)
    base = confirm_page_numbers(unique)
    return fill_page_numbers(rising_page_numbers(
        base + agreeing_page_numbers(repeated, base)))


def confirm_page_numbers(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """이웃 오프셋 중앙값과 어긋나는 (PDF쪽, 인쇄쪽)을 버린다.

    쪽마다 독립으로 읽으면 머리말의 다른 숫자(장 번호·연도·수식)가 쪽번호로
    둔갑한다 — 실측: 전자기학은 머리말에 쪽번호가 아예 없는데 562쪽 중 13쪽에
    엉뚱한 값이 붙었고(예: PDF 81쪽 → '인쇄 22쪽').

    허용 오차 1: 예전 값 3은 실측에서 오탐을 그대로 통과시켰다 — 반도체 교재는
    본문 오프셋이 25로 일정한데 오독이 22·27로 떨어져 ±3 문턱을 아슬아슬하게
    넘겼고, PDF 200쪽이 실제 175쪽인데 '인쇄 178쪽'으로 새겨져 203쪽과 값이
    겹쳤다. 1로 조여도 정상인 세 권은 한 개도 잃지 않는다(485→485, 921→921,
    960→960 실측) — 진짜 드리프트는 창 중앙값도 함께 움직이기 때문이다.
    """
    offs = {pdf: pdf - pr for pdf, pr in pairs}
    keep = []
    for pdf, pr in pairs:
        near = sorted(o for p, o in offs.items()
                      if p != pdf and abs(p - pdf) <= PAGE_NO_WINDOW)
        if len(near) < PAGE_NO_MIN_VOTES:
            continue          # 고립된 값은 검증할 수 없다
        if abs(offs[pdf] - near[len(near) // 2]) > PAGE_NO_TOL:
            continue
        keep.append((pdf, pr))
    return keep


def rising_page_numbers(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """인쇄 번호가 PDF 쪽 순서대로 증가하는 최장 부분열만 남긴다.

    책의 인쇄 쪽번호는 뒤로 갈수록 반드시 커진다. 이웃 대조를 통과하고도
    앞 값과 같거나 작은 값은(앞표지·화보의 아라비아 숫자 오독) 그 자체로
    증거가 부정한다. 최장 증가 부분열을 남기면 역행이 0이 된다.
    """
    if not pairs:
        return []
    pairs = sorted(pairs)
    tails: list[int] = []      # tails[i] = 길이 i+1 부분열의 최소 끝값
    tail_at: list[int] = []    # 그 부분열의 마지막 원소 인덱스
    prev: list[int | None] = [None] * len(pairs)
    for i, (_pdf, pr) in enumerate(pairs):
        j = bisect_left(tails, pr)
        if j == len(tails):
            tails.append(pr)
            tail_at.append(i)
        else:
            tails[j] = pr
            tail_at[j] = i
        prev[i] = tail_at[j - 1] if j > 0 else None
    out, cur = [], tail_at[len(tails) - 1]
    while cur is not None:
        out.append(pairs[cur])
        cur = prev[cur]
    return out[::-1]


def fill_page_numbers(pairs: list[tuple[int, int]],
                      gap: int = PAGE_NO_FILL_GAP) -> list[tuple[int, int]]:
    """오프셋이 같은 두 확정값 사이의 빈 쪽을 메운다.

    산술 환산이 아니다 — 앞뒤 두 앵커가 모두 검증됐고 그 사이에서 오프셋이
    변하지 않았음을 두 값이 함께 증명할 때만 메운다(b-a == n_b-n_a). 인쇄
    쪽번호는 번호가 찍히지 않은 쪽에도 매겨지므로, 구간 안의 모든 쪽은
    앵커에서 한 칸씩 센 값이다. 오프셋이 다르면 구간 안에서 무언가 어긋난
    것이므로 손대지 않는다. 폭을 20쪽으로 제한하는 것도 같은 이유다 —
    구간이 길수록 상쇄되는 두 개의 스캔 사고가 숨을 여지가 커진다.

    실측 회수율: 92.4%→95.6%, 94.1%→96.8%, 91.6%→97.2%, 21.9%→39.8%.
    """
    got = dict(pairs)
    out = dict(pairs)
    keys = sorted(got)
    for a, b in zip(keys, keys[1:]):
        if b - a < 2 or b - a > gap:
            continue
        if b - a != got[b] - got[a]:
            continue
        for p in range(a + 1, b):
            out[p] = got[a] + (p - a)
    return sorted(out.items())


def settle_page_numbers(md_path: Path) -> tuple[int, int, int]:
    """인쇄 쪽번호를 확정한다. 반환: (지운 수, 채운 수, 바로잡은 수).

    오탐을 지우고, 확정값이 증명하는 빈칸을 메운다. 오탐이 지워진 자리에
    앵커가 다른 값을 증명하면 그 자리는 '바로잡힌' 것이다(실측: 반도체
    PDF 200쪽 '인쇄 178쪽' → 실제 머리말 175쪽).

    확인 못 한 쪽번호는 없는 것보다 나쁘다 — AI가 그 값을 믿고 엉뚱한 쪽을
    읽는다. 반대로 두 앵커가 증명하는 쪽번호를 비워 두는 것도 손해다.
    """
    text = md_path.read_text(encoding="utf-8")
    hits = [(int(m.group(1)), int(m.group(2)) if m.group(2) else None, m.span())
            for m in _PAGE_HEAD_ANY.finditer(text)]
    if not hits:
        return 0, 0, 0
    have = [(p, n) for p, n, _s in hits if n is not None]
    final = dict(settle_pairs(have))
    dropped = added = fixed = 0
    out, last = [], 0
    for pdf, old, (s, e) in hits:
        new = final.get(pdf)
        if new == old:
            continue
        if new is None:
            dropped += 1
        elif old is None:
            added += 1
        else:
            fixed += 1
        head = f"## {pdf}페이지" + (f" (인쇄 {new}쪽)" if new is not None else "")
        out.append(text[last:s])
        out.append(head)
        last = e
    if not out:
        return 0, 0, 0
    out.append(text[last:])
    md_path.write_text("".join(out), encoding="utf-8")
    return dropped, added, fixed


_PAGE_NO_TOKEN = re.compile(r"(?<![\d.])(\d{1,4})(?![\d.])")
# 낱자로 흩어진 숫자 런만 붙인다('8 7 4' → '874'). 인접 숫자 사이 공백을
# 무조건 지우면 '392 16장'이 '39216장'이 되어 쪽번호가 사라진다(실측).
_SPACED_DIGITS = re.compile(r"(?<!\d)\d(?: \d)+(?!\d)")
PAGE_NO_DRIFT = 60          # PDF 쪽과 이만큼 넘게 벌어지면 쪽번호가 아니다
# 쪽번호를 찾을 상단 띠. 좁은 것을 먼저 보고, 못 찾을 때만 넓힌다 — 한 값으로는
# 안 된다(실측 12쪽 표본): 7.2%는 응용수학을 12/12 맞히지만 전자기학은 1/12뿐이고,
# 11%로 넓히면 전자기학이 11/12(오프셋 전부 일치)로 뛰는 대신 응용수학이 3/12로
# 무너진다 — 넓은 띠가 본문 숫자를 함께 물어 오기 때문이다. 순차 폴백은 좁은 띠가
# 이미 찾은 답을 절대 잃지 않고, 못 찾은 쪽에서만 넓은 띠를 시도한다.
HEADER_PROBE_RATIOS = (0.072, 0.11)


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


def read_printed_page(bands: list[str], page_image, tmp_dir: Path, page_no: int,
                      early=None) -> int | None:
    """상단 머리말 띠를 직접 읽어 인쇄 쪽번호를 회수한다.

    파이프라인의 줄 목록에 기대지 않는다 — 책에 따라 머리말이 줄로 잡히지
    않는다(실측: 응용수학·전기회로는 header_band 안에 줄이 0개였다).
    띠마다 내장 텍스트층(bands)을 먼저 보고, 못 읽으면 띠만 한 줄 OCR한다.
    early는 첫 띠의 OCR을 선행 스레드가 미리 띄워 둔 작업이다(precompute_page).
    """
    for i, ratio in enumerate(HEADER_PROBE_RATIOS):
        n = printed_page_number(bands[i], page_no)
        if n is None:
            if i == 0 and early is not None:
                try:
                    txt = early.result()
                except Exception:
                    txt = ""
            else:
                txt = pdf_text.ocr_top_band(page_image, tmp_dir, f"{page_no}_{i}", ratio)
            n = printed_page_number(txt, page_no)
        if n is not None:
            return n
    return None
