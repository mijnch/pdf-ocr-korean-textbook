# -*- coding: utf-8 -*-
"""순수 함수 골든 테스트 — 모델·외부 프로세스 없이 실행된다.

실행: python scripts\tests\test_pure.py   (전부 통과하면 'ALL PASS')
수식 정리·머리말 판정·표 격자·tsv 짝짓기의 회귀를 잡는 안전망이며,
검토단(2026-07-19)이 확인한 결함의 수정을 회귀 케이스로 고정한다.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_audit  # noqa: E402
import pdf_chapters  # noqa: E402
import pdf_ocr  # noqa: E402
import pdf_splice  # noqa: E402
import pdf_table  # noqa: E402
import pdf_text  # noqa: E402

FAIL = []
TOTAL = 0


def check(name, cond):
    global TOTAL
    TOTAL += 1
    if not cond:
        FAIL.append(name)
        print(f"  [실패] {name}")


# ─── clean_latex: 조사 잔재 2계층 절제 (검토단 C1 수정 고정) ───
cl = pdf_ocr.clean_latex
check("safe-strip \\circ", cl(r"x + 1 \circ}") == "x + 1")
check("safe-strip \\Box]", cl(r"\alpha \Box]") == r"\alpha")
check("risky-keep rangle", r"\rangle" in cl(r"\hat{H}|\psi\rangle=E|\psi\rangle"))
check("risky-keep =\\Phi", cl(r"A=\Phi").endswith(r"\Phi"))
check("risky-strip Phi]", cl(r"\alpha \Phi]") == r"\alpha")
check("degree keep", cl(r"90^{\circ}") == r"90^{\circ}")
check("compose keep", r"\circ" in cl(r"f \circ g"))

# ─── 연접 반복 압축(⑮ 잔존 퇴화 공략) — 압축 대상과 보존 대상 고정 ───
check("tandem group", cl(r"a" + r" {\tau} {\bot}" * 10 + " b")
      == r"a {\tau} {\bot} b")
# 12연발 압축 후 남은 장식 꼬리는 조사 잔재 규칙이 마저 제거한다(실측 p36과 동일).
# 정확한 산출값을 고정한다 — '<= 1'은 전삭제(0)까지 허용해 아무것도 안 잡던 약한 단언.
check("tandem braced", cl(r"x" + r" {\overline{{\circ}}}" * 12) == "x")
check("tandem matrix keep",  # 영행렬의 정당한 행 반복은 보존
      cl(r"\begin{matrix} {0} & {0} & {0} & {0} & {0} \\ \end{matrix}").count("{0}") == 5)
check("tandem spacing keep",  # 배치용 간격 런은 보존
      cl(r"a \ \ \ \ \ \ \ b") == r"a \ \ \ \ \ \ \ b")
check("tandem prime keep", cl(r"y\prime\prime\prime\prime") == r"y\prime\prime\prime\prime")
check("tandem short keep",  # 4회 반복(임계 미만)은 보존
      cl(r"a {\tau} {\tau} {\tau} {\tau} b") == r"a {\tau} {\tau} {\tau} {\tau} b")

# ─── 숫자 접합 제거 (검토단 치명 수정 고정: 1 4 6 4 1 보존) ───
check("digit-list keep", cl("1 4 6 4 1") == "1 4 6 4 1")
check("digit-pair keep", cl("(3 4)") == "(3 4)")
check("digit-punct join", "0,0" in cl("5 0 , 0 0 0"))

# ─── 연접 반복이 정당한 수치를 파괴하지 않는다 (검토단 B1 수정 고정) ───
# 구조(백슬래시 명령/중괄호) 없는 순수 숫자·문자·연산자 반복은 내용이므로 보존한다.
check("tandem digits keep", cl("1010101010") == "1010101010")
check("tandem decimal keep", cl("2.0000000000") == "2.0000000000")
check("tandem repeat-digit keep", cl("10000000000") == "10000000000")
check("tandem paren keep", cl("(1-x)(1-x)(1-x)(1-x)(1-x)") == "(1-x)(1-x)(1-x)(1-x)(1-x)")
check("tandem spaced-digits keep", cl("1 0 1 0 1 0 1 0 1 0 1 0") == "1 0 1 0 1 0 1 0 1 0 1 0")
# align/gather 등 정렬 환경의 정당한 행 반복도 압축하지 않는다(_MATRIX_ENV 확장)
check("tandem align keep",
      cl(r"\begin{align}a & b \\ a & b \\ a & b \\ a & b \\ a & b\end{align}").count("a & b") == 5)

# ─── \S 보존 (절 참조 훼손 방지) / 기존 정리 회귀 ───
check("keep section sym", "S" in cl(r"\S 3.2"))
# 안 닫힌 여는 괄호를 실제로 닫아 주는지 정확한 산출값으로 고정한다 — 좌우 '{' '}'
# 개수만 비교하면 빈 문자열('0 == 0')도 통과하던 약한 단언이었다(검토단).
check("balance close", cl(r"\mathbf{E_{0}") == r"\mathbf{E_{0}}")
check("balance strip", cl(r"} x") == "x")
check("root of", r"\sqrt[4]" in cl(r"\root 4 \of {5x}"))
check("eq-run collapse", cl("= = = = = = =").count("=") == 1)
check("token-run", cl(r"\mathrm{o} " * 9).count(r"\mathrm{o}") == 1)
check("only-spacing drop", cl(r"\quad \quad") == "")
check("\\i brace", cl(r"\beta\i") == r"\beta{i}")
check("\\o glyph", "{o}" in cl(r"E_{\o}"))
check("opname cos", cl(r"\operatorname{c o s} x").startswith(r"\cos"))

# ─── 러닝 머리말 판정 (검토단 오탐 수정 고정) ───
hdr = pdf_ocr._RUNNING_HDR
check("hdr match", bool(hdr.match("256 제 6장 시변계와 Maxwell 방정식")))
check("hdr match en", bool(hdr.match("104 Chapter 5 Energy")))
check("hdr no-match ref", not hdr.match("3. 제 2장에서 다룬 회로를 이용하여"))
check("hdr no-match sec", not hdr.match("연습문제 1.2"))

# ─── 워터마크·정상글자 ───
check("wm strip", pdf_ocr._WATERMARK_RE.sub(" ", "Made with Goodnotes/").strip() == "")
check("sane greek", pdf_ocr._is_sane_char("α"))
check("sane hangul", pdf_ocr._is_sane_char("가"))
check("insane hanja", not pdf_ocr._is_sane_char("一"))

# ─── pdf_table: 합성 격자 (행=괘선, 열=백색 홈) ───
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# 실물 비율 유지: 괘선이 열 홈 잉크 임계(1.5%)를 넘지 않도록 행을 충분히 높게
a = np.full((200, 300), 255, dtype=np.uint8)
for y in (5, 60, 115, 170):         # 수평 괘선 4개(1px) → 3행
    a[y, :] = 0
for r0, r1 in ((15, 45), (70, 100), (125, 155)):  # 행마다 좌/우 텍스트 블롭
    a[r0:r1, 20:100] = 0    # 행 잉크가 괘선 임계(55%) 미만이 되게 폭 53%로
    a[r0:r1, 170:250] = 0
timg = Image.fromarray(a)
grid = pdf_table.detect_grid(timg)
check("grid rows/cols", grid is not None and len(grid[0]) == 3 and len(grid[1]) == 2)

_rows = {}
if grid:
    for i, (ry0, ry1) in enumerate(grid[0]):
        _rows[(ry0, ry1)] = i + 1


def cell_ok(box):
    ri = _rows.get((box[1], box[3]), 0)
    return f"이름{ri}" if box[0] < 120 else str(ri)


md = pdf_table.extract(timg, cell_ok) if grid else None
check("table extract", md is not None and "| 이름2 | 2 |" in (md or ""))
check("table eq reject",
      pdf_table.extract(timg, lambda b: "x = 1") is None)
check("table holey reject",
      pdf_table.extract(
          timg, lambda b: "값" if (b[0] < 120 or _rows.get((b[1], b[3])) == 1)
          else "") is None)
check("table junk reject",
      pdf_table.extract(timg, lambda b: "十 값") is None)

# ─── _gutter_cols: 백색 홈 → 열 밴드(열 폭주 폴백의 기반) ───
_gc = pdf_table._gutter_cols
# 홈 2개(폭 8) → 열 3개. 가장자리에 닿은 홈은 경계가 아니므로 세지 않는다.
_mask = [False]*40 + [True]*8 + [False]*40 + [True]*8 + [False]*40
check("gutter cols", len(_gc(_mask, len(_mask))) == 3)
# 좁은 홈(폭 < MIN_GUTTER_W)은 낱말 사이 공백이므로 무시한다
_narrow = [False]*40 + [True]*3 + [False]*40
check("gutter narrow ignored", len(_gc(_narrow, len(_narrow))) == 1)
# 홈이 없으면 한 덩어리
check("gutter none", len(_gc([False]*80, 80)) == 1)

# ─── _accept_table: 스캔/내장 공통 위생 게이트(⑬ 재활성) ───
check("accept clean", pdf_ocr._accept_table("| a | b |\n| --- | --- |\n| 1 | 2 |")
      == "| a | b |\n| --- | --- |\n| 1 | 2 |")
check("accept junk reject", pdf_ocr._accept_table("| 一 | b |\n| --- | --- |") == "")
check("accept none", pdf_ocr._accept_table(None) == "")

# ─── _row_order: 같은 행 좌→우 읽기(표 5.4 (2)(1)(4)(3) 수정 고정) ───
def _blk(name, x0, y0, x1, y1, col=1):
    return {"text": name, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "col": col}


# 공식표: (1) 좌 / (2) 우가 같은 행, y0는 (2)가 몇 px 작다
_tbl = [_blk("(2)", 700, 100, 1200, 160), _blk("(1)", 100, 104, 600, 164),
        _blk("(4)", 700, 200, 1200, 260), _blk("(3)", 100, 203, 600, 263)]
check("row order lr", [b["text"] for b in pdf_ocr._row_order(_tbl)]
      == ["(1)", "(2)", "(3)", "(4)"])
# 통상 문단(세로로 안 겹침)은 순서 불변
_para = [_blk("p1", 100, 100, 1200, 160), _blk("p2", 100, 200, 1200, 260),
         _blk("p3", 100, 300, 1200, 360)]
check("row order para", [b["text"] for b in pdf_ocr._row_order(_para)]
      == ["p1", "p2", "p3"])
# 칼럼 우선: 좌단 전체를 읽은 뒤 우단
_two = [_blk("L1", 100, 100, 500, 160, 1), _blk("R1", 700, 100, 1100, 160, 2),
        _blk("L2", 100, 200, 500, 260, 1), _blk("R2", 700, 200, 1100, 260, 2)]
check("col then row", [b["text"] for b in pdf_ocr._by_col_then_row(_two)]
      == ["L1", "L2", "R1", "R2"])

# ─── _weave_line: 스캔 인라인 수식 제자리 삽입(검토단 C-4 수정 고정) ───
_wl = pdf_ocr._weave_line
_f1 = {"text": "x(0)=0", "x0": 645, "x1": 778}
_f2 = {"text": "c_1=0", "x0": 900, "x1": 980}
# 틈 1개 = 수식 1개: 틈에 채운다(끝에 안 붙임)
check("weave one gap", _wl("초기조건    로부터 얻는다", [_f1])
      == "초기조건 $x(0)=0$ 로부터 얻는다")
# 틈 2개 = 수식 2개: 왼→오 순서로 채운다(수식은 x0로 정렬)
check("weave two gaps", _wl("초기조건    로부터    을 얻는다", [_f2, _f1])
      == "초기조건 $x(0)=0$ 로부터 $c_1=0$ 을 얻는다")
# 틈 수 불일치: 안전 폴백(끝에 붙임)
check("weave fallback", _wl("틈 없는 줄", [_f1]) == "틈 없는 줄 $x(0)=0$")

# 단어 좌표(tsv)가 있으면 틈에 의존하지 않고 정확한 지점에 넣는다 — 1순위 경로.
# words = [(글자수, x0, x1)]. '초기조건'(x 500~640) 뒤, '로부터'(x 800~900) 앞에 수식.
_w = [(4, 500, 640), (3, 800, 900), (6, 950, 1100)]
check("weave by words", _wl("초기조건 로부터 얻는다합니다", [_f1], _w)
      == "초기조건 $x(0)=0$ 로부터 얻는다합니다")
# 틈이 없어도(폴백이었을 상황) 단어 좌표만으로 제자리 삽입된다
check("weave words no gap", "초기조건 $x(0)=0$ 로부터"
      in _wl("초기조건 로부터 얻는다합니다", [_f1], _w))
# 수식 2개도 각각 제자리로(x 순서대로)
check("weave words two", _wl("초기조건 로부터 얻는다합니다", [_f2, _f1], _w)
      == "초기조건 $x(0)=0$ 로부터 $c_1=0$ 얻는다합니다")
# 단어 정보가 비면 틈/끝붙임 경로로 안전하게 내려간다
check("weave words empty", _wl("틈 없는 줄", [_f1], []) == "틈 없는 줄 $x(0)=0$")

# ─── 위첨자 복원: 내장 텍스트층의 '작은 글꼴' 신호로 지수를 되살린다 ───
# (검토단 C-3: '35 × 10^10'이 '35 X 1010'으로 평문화돼 값이 10^7배 틀렸다)
_sm = pdf_ocr.superscript_marks
_gs = pdf_ocr.graft_superscripts
_big, _sml = 6.15, 4.61
_seq = [("1", _big), ("0", _big), ("1", _sml), ("0", _sml)]
check("sup marks run", _sm(_seq) == {2: ("^{", ""), 3: ("", "}")})
check("sup graft", _gs("1010", _seq) == "10^{10}")
check("sup graft spacing", _gs("35 X 1010",
                               [("3", _big), ("5", _big), (" ", 1.0), ("X", _big),
                                (" ", 1.0), ("1", _big), ("0", _big),
                                ("1", _sml), ("0", _sml)]) == "35 X 10^{10}")
check("sup paren base", _gs("(12 A)2",
                            [("(", _big), ("1", _big), ("2", _big), (" ", 1.0),
                             ("A", _big), (")", _big), ("2", _sml)]) == "(12 A)^{2}")
# 문자 밑수는 아래첨자(v_1, C_2)일 확률이 높아 건드리지 않는다 — 위/아래 구분 불가
check("sup letter base skipped",
      _sm([("m", _big), ("2", _sml)]) == {})
check("sup no context", _sm([("가", _big), ("2", _sml)]) == {})
# 긴 작은글꼴 런은 지수가 아니라 글꼴이 섞인 본문이다
check("sup long run", _sm([("1", _big)] + [(str(d), _sml) for d in range(5)]) == {})
# 정렬 실패(글자열 불일치)나 표식 없음이면 원문을 그대로 돌려준다
check("sup mismatch keeps plain", _gs("다른 글자", _seq) == "다른 글자")
check("sup none keeps plain", _gs("1010", [("1", _big), ("0", _big)]) == "1010")

# ─── split_semantic_heading: 절 표지 승격(AI 탐색성) + 오탐 차단 ───
_sh = pdf_ocr.split_semantic_heading
check("sem plain", _sh("예제 1") == ("예제 1", ""))
check("sem spaced", _sh("예 제 2-4") == ("예제 2-4", ""))       # 스캔본 띄어쓰기 변형
check("sem colon", _sh("풀이:") == ("풀이", ""))
check("sem title", _sh("정리 1.2.1_ 유일한 해의 존재")
      == ("정리 1.2.1 유일한 해의 존재", ""))
check("sem body split", _sh("풀이 주어진 함수가 해가 되는지를 검증하는 방법은 구간 내에 "
                            "있는 모든 x에 대해 대입하는 것이다.")[0] == "풀이")
check("sem trailing sep", _sh("예제 2-10 _") == ("예제 2-10", ""))
# 오탐 차단(전부 실 코퍼스에서 관측된 문장)
check("sem reject 정의역", _sh("정의역（domain）, y가 정의되는 집합을 치역이라 한다")[0] is None)
check("sem reject 조사", _sh("문제를 나타낸다. 또한 좀 더 어려운 문제에는 표시했다.")[0] is None)
check("sem reject 합성어", _sh("참고문언 1013 찾아보기 1015")[0] is None)
check("sem reject 참조", _sh("퀴즈 7.8에서 본 것처럼, 그래프에서 기울기에 음의 부호를")[0] is None)
check("sem reject 목록",
      _sh("연습문제 2.7, 공기 교체 연습문제 2.9, 칼륨-40 붕괴")[0] is None)
check("sem reject 무번호정리", _sh("정리 이러한 성질을 이용하면 다음을 얻는다")[0] is None)
# render_flow 배선: 표지로 시작하는 본문 블록이 '### 헤딩' + 본문으로 갈라진다
_flow = [
    {"btype": "text", "text": "앞 문단이다.", "x0": 100, "x1": 1000, "col": 1},
    {"btype": "text", "text": "예제 3.2 휴가 여행", "x0": 100, "x1": 400, "col": 1},
    {"btype": "text", "text": "풀이 이 문제는 등가속도 운동으로 다룬다.",
     "x0": 100, "x1": 1000, "col": 1},
]
_out = pdf_ocr.render_flow(_flow, 1000)
check("sem flow heading", "### 예제 3.2 휴가 여행" in _out)
check("sem flow split", "### 풀이" in _out
      and any(l.startswith("이 문제는 등가속도") for l in _out))
check("sem flow keeps body", any("앞 문단이다." in l for l in _out))
# 표지가 없는 본문은 헤딩을 만들지 않는다
check("sem flow no false heading",
      not [l for l in pdf_ocr.render_flow(
          [{"btype": "text", "text": "정의역은 집합이다.", "x0": 100, "x1": 400, "col": 1}],
          1000) if l.startswith("###")])

# ─── prune_page_number_outliers: 이웃과 어긋나는 인쇄 쪽번호 제거 ───
def _mk(pairs):
    """(pdf쪽, 인쇄쪽 또는 None) 목록 → 임시 MD 파일."""
    body = "\n\n".join(f"## {p}페이지" + (f" (인쇄 {q}쪽)" if q else "") + "\n\n본문"
                       for p, q in pairs)
    f = Path(tempfile.mkdtemp()) / "t.md"
    f.write_text(body + "\n", encoding="utf-8")
    return f


# 오프셋이 일정한 계열은 그대로 둔다
_f = _mk([(i, i - 8) for i in range(100, 116)])
check("settle keeps consistent", pdf_ocr.settle_page_numbers(_f) == (0, 0, 0))
# 하나만 튀면 그것만 지운다 — 그리고 양옆 앵커가 참값을 증명하므로 바로잡는다
_pairs = [(i, i - 8) for i in range(100, 116)]
_pairs[7] = (107, 60)                       # 오프셋 +47 — 튄 값
_f = _mk(_pairs)
check("settle fixes outlier", pdf_ocr.settle_page_numbers(_f) == (0, 0, 1))
check("settle fixed value", "## 107페이지 (인쇄 99쪽)" in _f.read_text(encoding="utf-8"))
# 고립된 값(이웃 표본 부족)은 검증할 수 없으므로 지운다
check("settle drops isolated",
      pdf_ocr.settle_page_numbers(_mk([(5, 3), (200, 150), (400, 380)])) == (3, 0, 0))
# 완만한 드리프트는 살린다(책 안에서 오프셋이 서서히 변함)
check("settle keeps drift",
      pdf_ocr.settle_page_numbers(
          _mk([(i, i - (8 if i < 108 else 7)) for i in range(100, 116)])) == (0, 0, 0))
# ±3 오탐은 예전 허용치(3)를 통과했다 — 반도체 교재 실측(오프셋 25에 22·27이 섞임)
check("settle tol drops near-miss",
      pdf_ocr.confirm_page_numbers([(i, i - 25) for i in range(100, 112)]
                                   + [(112, 112 - 22)]) == [(i, i - 25) for i in range(100, 112)])
# 인쇄 번호가 역행하면 그 자체로 오독이다 — 최장 증가 부분열만 남긴다
check("rising drops backslide",
      pdf_ocr.rising_page_numbers([(1, 10), (2, 11), (3, 4), (4, 13), (5, 14)])
      == [(1, 10), (2, 11), (4, 13), (5, 14)])
check("rising keeps monotone",
      len(pdf_ocr.rising_page_numbers([(i, i - 5) for i in range(20, 40)])) == 20)
# 앵커 사이 보간: 양 끝 오프셋이 같을 때만, 그리고 정해진 폭 안에서만 메운다
check("fill between anchors",
      pdf_ocr.fill_page_numbers([(10, 2), (14, 6)]) ==
      [(10, 2), (11, 3), (12, 4), (13, 5), (14, 6)])
check("fill refuses offset change",
      pdf_ocr.fill_page_numbers([(10, 2), (14, 7)]) == [(10, 2), (14, 7)])
check("fill refuses wide gap",
      pdf_ocr.fill_page_numbers([(10, 2), (60, 52)]) == [(10, 2), (60, 52)])
# 되풀이 적용해도 결과가 같아야 한다(스플라이스 후 재실행)
_f = _mk([(i, i - 8) for i in range(100, 116)])
pdf_ocr.settle_page_numbers(_f)
check("settle idempotent", pdf_ocr.settle_page_numbers(_f) == (0, 0, 0))

# ─── looks_like_prose: 색 배경 글상자와 도표 라벨 가르기 ───
check("prose korean box", pdf_ocr.looks_like_prose(
    "그림 10.17(a)는 그림 10.16(a)에 주어진 토폴로지의 구현을 설명하고 있다. "
    "차동 전압 이득을 계산하라. 각 pnp 소자가 출력 노드에서 저항을 생성한다."))
check("prose circuit labels not",
      not pdf_ocr.looks_like_prose("Vcc Vb Q3 Q4 Vout Vin1 Vin2 P IEE (a) (b)"))
check("prose graph legend not",
      not pdf_ocr.looks_like_prose("IC Forward Active Region VCE V1 IS exp"))
# 표로 잡힌 영역은 한글 산문만 인정한다 — 영문 비교표가 통과하면 안 된다
# (실측: 발진기 비교표 영단어 60개·한글 0자 / 같은 책 예제 상자 한글 122~539자)
check("prose strict rejects english table", not pdf_ocr.looks_like_prose(
    "LC Oscillators Cross-Coupled Colpitts Phase Shift Wien-Bridge Ring "
    "Oscillator Frequency Response Amplitude Startup Condition Loop Gain "
    "Negative Resistance Tank Circuit Quality Factor Output Swing Power",
    strict=True))
check("prose strict keeps korean box", pdf_ocr.looks_like_prose(
    "예제 4.11 회로에서 소자가 능동 영역에서 동작한다는 것을 증명하고 전압을 "
    "구하라. 풀이 전압 강하는 증가하여 컬렉터 전압은 다음과 같이 계산된다.",
    strict=True))

check("prose english box", pdf_ocr.looks_like_prose(
    "The transfer function of the network shown above may be obtained by writing "
    "a node equation at the output and solving for the resulting ratio between "
    "output voltage and input voltage across the passive elements."))

# ─── tinted_ratio: 옅은 색 상자와 흰 바탕 도표 가르기 (실측값 고정) ───
from PIL import Image as _PILImage  # noqa: E402


def _swatch(rgb, w=60, h=40):
    return _PILImage.new("RGB", (w, h), rgb)


_box = {"x0": 0, "y0": 0, "x1": 60, "y1": 40}
# 아주 옅은 하늘색 상자(전자회로 예제 상자와 같은 계열) — 채도 기준은 못 잡는다
check("tint pale blue box", pdf_ocr.tinted_ratio(_swatch((230, 240, 250)), _box) > 0.9)
check("tint pale blue misses saturation gate",
      pdf_ocr.colored_ratio(_swatch((230, 240, 250)), _box) == 0.0)
check("tint white page", pdf_ocr.tinted_ratio(_swatch((255, 255, 255)), _box) == 0.0)
check("tint grey not tinted", pdf_ocr.tinted_ratio(_swatch((240, 240, 240)), _box) == 0.0)
check("tint dark photo not", pdf_ocr.tinted_ratio(_swatch((40, 90, 160)), _box) == 0.0)

# ─── merge_rescue_lines: 어긋난 중복이 문단에 끼어드는 것 차단 ───
def _ln(t, y0, y1, x0=0, x1=500, conf=90):
    return {"text": t, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "conf": conf}


# 세로로 절반 넘게 겹치면 같은 줄 — 중심점이 틈에 떨어져도 막힌다
_pri = [_ln("반도체 내에 전하 캐리어의 전송현상을 다루고 있다", 100, 120)]
check("rescue blocks shifted overlap",
      pdf_text.merge_rescue_lines(_pri, [_ln("반도체 내에 전하 캐리어의 전송현상을", 112, 132)]) == 0)
# 위치가 완전히 달라도 내용이 메아리면 막는다
_pri = [_ln("반도체 내에 전하 캐리어의 전송현상을 다루고 있다", 100, 120)]
check("rescue blocks echo",
      pdf_text.merge_rescue_lines(
          _pri, [_ln("반도체 내에 전하 캐리어의 전송현상율 다루고", 400, 420)]) == 0)
# 진짜로 빠진 줄은 여전히 보충한다(p567 회귀 — 이 기능의 존재 이유)
_pri = [_ln("반도체 내에 전하 캐리어의 전송현상을 다루고 있다", 100, 120)]
check("rescue still adds missing",
      pdf_text.merge_rescue_lines(
          _pri, [_ln("불평형 과잉 캐리어 특성을 설명한다", 400, 420)]) == 1)

# ─── 캡션 띠: 표지 없는 글은 그림 바로 아래로 제한 ───
# (기하 배선은 process_page 안이라 여기서는 판정 함수만 고정한다)
check("capband marker wide", bool(pdf_ocr._CAPTION_RE.match("그림 3-7 구형 전자구름")))
check("capband plain narrow", not pdf_ocr._CAPTION_RE.match("이다, 식 (3-6)에 대입하면"))

# ─── printed_page_number: 머리말에서 인쇄 쪽번호 회수 ───
_ppn = pdf_ocr.printed_page_number
check("ppn left edge", _ppn(("274  CHAPTER 7 일차 회로"), 300) == 274)
check("ppn right edge", _ppn(("14.3 삼중적분   495"), 500) == 495)
check("ppn with bar", _ppn(("7.6 벡터공간 | 437"), 450) == 437)
# 절 번호는 소수점에 붙어 있어 쪽번호로 오인되지 않는다
check("ppn skips section no", _ppn(("16.6 음파 Sound Waves"), 400) is None)
# PDF 쪽과 너무 먼 숫자는 쪽번호가 아니다
check("ppn rejects far", _ppn(("CHAPTER 3 회로 해석 방법"), 900) is None)
check("ppn empty", _ppn("", 100) is None)
# 실측 사례: 전기회로 PDF150 → 인쇄 124
check("ppn real case", _ppn(("124        CHAPTER 3   회로 해석 방법"), 150) == 124)
# 저품질 내장층은 숫자를 낱자로 흩는다 — 붙여서 읽는다
check("ppn spaced digits", _ppn("8 7 4 C H A P T E R 1 8 푸", 900) == 874)
# 그러나 멀쩡한 숫자끼리 붙이면 안 된다('392 16장'→'39216장'이 되던 결함)
check("ppn keeps separate nums", _ppn("392 16장 파동의운동", 400) == 392)

# ─── 매달린 \Phi: 수식 뒤 한글 조사의 오인식 ───
import pdf_latex  # noqa: E402

_cl = pdf_latex.clean_latex
check("phi drop after digit", _cl(r"x^{2}+y^{2}=1 \Phi") == r"x^{2}+y^{2}=1")
check("phi drop after brace", _cl(r"M_{i} =3 6 0 \mathrm{k g} \Phi").endswith("g}"))
check("phi drop after cmd", _cl(r"A=4 \pi \Phi").endswith(r"\pi"))
check("phi keep leading", _cl(r"\Phi = B A") == r"\Phi = B A")
check("phi keep after operator", _cl(r"\Delta V = \Phi").endswith(r"\Phi"))
check("phi keep subscripted", r"\Phi_{B}" in _cl(r"\Phi_{B} = B A \cos \theta"))
check("phi keep midway", r"\Phi" in _cl(r"E = \Phi / A"))
# \O는 Φ로 지어내지 않고 글리프만 남긴다
check("O not invented", _cl(r"4 \pi \O") == r"4 \pi {O}")

# ─── pdf_glossary: 이 책이 쓰는 용어 목록 ───
import pdf_glossary  # noqa: E402

_idx = ("## 900페이지\n\n"
        + "대역 차단 678 대역 통과 필터 676 대역폭 669 테브냉의 정리 139 "
        + "시상수 265 커패시터 216 인덕터 240 노드 해석 82 등가회로 767 "
        + "리액턴스 410 임피던스 411 페이저 396 공진 664 필터 674 이득 655 "
        + "증폭기 187 발진기 465 변압기 589 상전압 534 선간전압 538 "
        + "무효전력 501 역률 505 삼상 533 푸리에 급수 809 전달함수 654\n")
_body = ("## 100페이지\n\n테브냉의 정리를 쓰면 회로가 간단해진다. 시상수는 RC다.\n"
         "테브냉의 정리와 시상수를 함께 쓴다. 커패시터와 인덕터도 나온다.\n"
         "커패시터 전압은 연속이다. 인덕터 전류는 연속이다.\n")
_terms = pdf_glossary.build(_body + _idx)
check("gloss finds book words", "테브냉의 정리" in _terms and "시상수" in _terms)
check("gloss drops index-only", "대역폭" not in _terms)  # 본문에 없음
check("gloss block has guide", "이 책이 쓰는 용어" in pdf_glossary.block(_terms)[0])
check("gloss empty when no index", pdf_glossary.build(_body) == [])
check("gloss block empty ok", pdf_glossary.block([]) == [])
# 그림 링크가 색인으로 오분류되지 않는다(짝을 대량 만든다)
_figs = "## 50페이지\n\n" + "\n".join(
    f"![그림 p50-{i}](대학수학_OCR_images/p50_fig{i}.png)" for i in range(1, 40))
check("gloss ignores figure links", pdf_glossary.build(_figs) == [])
# 문장이 많은 쪽은 색인이 아니다
check("gloss ignores prose",
      not pdf_glossary._is_index_page(
          "## 5페이지\n\n회로 12 전압 34 전류 56 저항 78 " * 8
          + "이다. 그렇다. 구한다. 계산한다."))

# ─── is_caption_like: 오귀속 차단(표지 없는 문장·잡음) ───
_icl = pdf_ocr.is_caption_like
check("cap marker any length",
      _icl("그림 1.2 (예제 1.1) 나무의 높이는 나무까지의 거리와 지표면으로부터"
           " 나무 꼭대기에 이르는 각도를 측정하여 잴 수 있다."))
check("cap short fragment", _icl("여러 가지 물질의 밀도"))
# 표지 없는 완결 문장은 캡션이 아니다 — 예제 문제 문장이 캡션이 되던 경로
check("cap reject sentence",
      not _icl("전투기가 63 m/s의 속력으로 항공모함에 착륙하려고 한다."))
check("cap reject question", not _icl("스 위치를 열면 어떻게 되는가?"))
# 표지가 붙었으면 문장으로 끝나도 캡션이다(원본 캡션이 실제로 문장인 경우)
check("cap marker sentence ok", _icl("그림 5.13 승강기가 위로 가속된다."))
# 한글 없는 잡음 조각은 캡션이 아니다
check("cap reject noise", not _icl("^ 1.18."))
check("cap reject noise2", not _icl(".18 1.29"))
# 문제 번호로 시작하면 본문
check("cap reject problem", not _icl("4.18 다음 회로에서 전류를 구하라"))
# 본문 참조 문장은 캡션 후보에서 제외된다(조사 경계)
_ref = pdf_ocr._CAPTION_REF
check("capref detects", bool(_ref.match("그림 1.28은 5개의 소자를 가진 회로이다.")))
check("capref allows label", not _ref.match("그림 1.28 문제 1.17."))
check("capref allows hyphen label", not _ref.match("그림 3-7 구형 전자구름의 전계강도"))

# ─── 내장 표 폐기 게이트: 지수 복원 실패 판정 ───
_sev = pdf_ocr.superscript_severed
# '10^-3'의 부호가 본문 글꼴로 떨어져 나간 형태 → 오염
_severed = [("1", 6.0), (".", 6.0), ("7", 6.0), (" ", 6.0), ("X", 6.0),
            (" ", 6.0), ("1", 6.0), ("0", 6.0), ("-", 6.0), ("3", 4.5)]
check("severed sign", _sev("1.7 X 10-3", _severed) == 1)
# 정상: 부호까지 작은 글꼴이라 런에 함께 들어오고 밑수가 '0'
_okrun = _severed[:8] + [("-", 4.5), ("3", 4.5)]
check("severed none when sign small", _sev("1.7 X 10-3", _okrun) == 0)
# 단위 지수('m2')는 문자 밑수 — 오염으로 세지 않는다
_unit = [("k", 6.0), ("g", 6.0), ("/", 6.0), ("m", 6.0), ("3", 4.5)]
check("severed skips unit exp", _sev("kg/m3", _unit) == 0)
# 숫자 닮은 글자 밑수는 곱셈 표시가 있을 때만 오염
_look = [("X", 6.0), (" ", 6.0), ("I", 6.0), ("O", 6.0), ("3", 4.5)]
check("severed lookalike with mult", _sev("X IO3", _look) == 1)
check("severed lookalike no mult", _sev("IO3", _look) == 0)
# 표 수준: 같은 표에서 반쪽만 복원된 경우
_tp = pdf_ocr.table_superscript_partial
check("table partial detected",
      _tp("| 지구 | 5.97 X 10^{24} |\n| 달 | 7.35 X 1022 |"))
check("table all restored ok",
      not _tp("| 지구 | 5.97 X 10^{24} |\n| 달 | 7.35 X 10^{22} |"))
check("table no exponents ok", not _tp("| 콘크리트 | 1.0 | 0.8 |"))
check("table flat only ok", not _tp("| 달 | 7.35 X 1022 |"))
# _sup_runs가 밑수 적합 여부를 함께 돌려준다(marks와 일관)
check("sup_runs flags base",
      [ok for _s, _e, ok in pdf_ocr._sup_runs(_severed)] == [False])
check("sup_marks skips bad base", pdf_ocr.superscript_marks(_severed) == {})
check("sup_marks keeps good base", bool(pdf_ocr.superscript_marks(_okrun)))

# ─── ai_preamble: 폴백 안내가 실현 가능한 경로를 가리키는가 ───
_big = pdf_ocr.ai_preamble("대학물리 교재.pdf", "대학물리 교재_OCR_images", 748 * 1048576)
_small = pdf_ocr.ai_preamble("전자기학.pdf", "전자기학_OCR_images", 88 * 1048576)
check("preamble png first", _big.index("PNG를 열람하라") < _big.index("원본 PDF(`"))
check("preamble names images dir", "대학물리 교재_OCR_images" in _big)
check("preamble warns oversize", "유일한 폴백" in _big and "748MB" in _big)
check("preamble allows small", "열람 가능" in _small and "88MB" in _small)
check("preamble is quote", _big.startswith("> **AI 안내**") and "\n" not in _big)

# ─── attach_orphan_captions: 그림 뒤에 평문으로 남은 캡션 회수 ───
_aoc = pdf_ocr.attach_orphan_captions
_IMG = "![그림 p53-1](x/p53_fig1.png)"
# 한 줄 캡션
check("orphan one line",
      _aoc([_IMG, "", "그림 1.28", "", "다음 문단이다."])[:4]
      == [_IMG, "", "*그림 1.28*", ""])
# 두 줄로 쪼개진 캡션은 합쳐서 흡수한다
check("orphan two line",
      "*그림 1.28 문제 1.17.*" in _aoc([_IMG, "", "그림 1.28", "", "문제 1.17.", "", "본문"]))
# 원본 줄은 소비되어 중복 남지 않는다
check("orphan consumes",
      _aoc([_IMG, "", "그림 1.28", "", "문제 1.17."]).count("그림 1.28") == 0)
# 이미 캡션이 있으면 손대지 않는다
check("orphan keeps existing",
      _aoc([_IMG, "", "*그림 1.29 문제 1.18.*", ""]) == [_IMG, "", "*그림 1.29 문제 1.18.*", ""])
# 본문 참조('그림 1.2에서 …')는 캡션이 아니다 — 조사 경계
check("orphan reject 조사",
      not any(l.startswith("*") for l in
              _aoc([_IMG, "", "그림 1.2에서 한 그루의 나무와 문제의 정보를 볼 수 있다."])))
# 페이지·장 헤딩은 절대 삼키지 않는다
check("orphan reject heading",
      "## 928페이지" in _aoc([_IMG, "", "## 928페이지"]))
check("orphan stops at heading",
      _aoc([_IMG, "", "그림 18.54 문제 18.9.", "", "## 928페이지"])[-1] == "## 928페이지")
# 긴 문단은 캡션 표지로 시작해도 본문이다
check("orphan reject long",
      not any(l.startswith("*") for l in _aoc([_IMG, "", "그림 3.61 " + "가" * 70])))
# 다음 그림은 캡션이 아니다 — 한 캡션을 두 그림이 나눠 갖지 않는다
_two = _aoc([_IMG, "", "![그림 p53-2](x/p53_fig2.png)", "", "그림 1.27 문제 1.16."])
check("orphan no double claim", _two.count("*그림 1.27 문제 1.16.*") == 1)
check("orphan claims nearest", _two.index("*그림 1.27 문제 1.16.*")
      > _two.index("![그림 p53-2](x/p53_fig2.png)"))
# 두 번 적용해도 결과가 같다
_once = _aoc([_IMG, "", "그림 1.28", "", "문제 1.17.", "", "본문이다."])
check("orphan idempotent", _aoc(_once) == _once)
# 표 병기(| …)는 캡션으로 오인하지 않는다
check("orphan reject table",
      not any(l.startswith("*") for l in _aoc([_IMG, "", "| a | b |", "| - | - |"])))

# ─── _SINGLE_SYMBOL: 그림 라벨 누수 판정(홑 기호 $$f$$ 수정 고정) ───
ss = pdf_ocr._SINGLE_SYMBOL
check("single f", bool(ss.match("f")))
check("single cmd", bool(ss.match(r"\theta")))
check("single digit", bool(ss.match("7")))
check("not sub", not ss.match("x^{2}"))
check("not two", not ss.match("f g"))
check("not relation", not ss.match("a = b"))
check("not frac", not ss.match(r"\frac{1}{2}"))

# ─── merge_rescue_lines: 고해상 OCR 사각지대 구조(p567 소실 수정 고정) ───
_prim = [{"text": "본문", "x0": 100, "y0": 100, "x1": 800, "y1": 130}]
_resc = [
    {"text": "같은 줄 재인식", "x0": 100, "y0": 102, "x1": 780, "y1": 128},  # 중복
    {"text": "불릿 둘째 줄", "x0": 160, "y0": 200, "x1": 400, "y1": 230},   # 신규
]
check("rescue add", pdf_ocr.merge_rescue_lines(_prim, _resc) == 1
      and _prim[-1]["text"] == "불릿 둘째 줄")
_p2 = [{"text": "좌단", "x0": 100, "y0": 100, "x1": 400, "y1": 130}]
check("rescue col", pdf_ocr.merge_rescue_lines(
    _p2, [{"text": "우단", "x0": 500, "y0": 100, "x1": 800, "y1": 130}]) == 1)

# ─── pdf_audit: 자가 감사(과거 감사 함정의 회귀 고정) ───
_MD_CLEAN = (
    "# 책.pdf\n\n## 1페이지\n\n본문 가격은 \\$3이다.\n\n"
    "$$ \\frac{a}{b} $$\n\n## 2페이지\n\n$x^{2}$ 문장.\n\n"
    "> [변환 완료] 2페이지, 수식 2개\n")
_ra = pdf_audit.audit_text(_MD_CLEAN)
check("audit clean", not _ra["낙오달러"] and not _ra["중괄호불균형"]
      and not _ra["퇴화반복"])
check("audit pages", _ra["페이지절수"] == 2 and _ra["완료표식"] == (2, 0))
_rb = pdf_audit.audit_text(
    "## 1페이지\n\n외로운 $ 하나와 \\$ 이스케이프.\n\n"
    "$\\mathbf{E_{0}$ 안 닫힘\n\n"
    "$\\tau \\tau \\tau \\tau \\tau \\tau \\tau \\tau \\tau \\tau \\tau$\n")
check("audit stray$", len(_rb["낙오달러"]) == 1)
check("audit brace", len(_rb["중괄호불균형"]) == 1)
check("audit degen", len(_rb["퇴화반복"]) == 1)
check("audit colspec ok", not pdf_audit.audit_text(
    "$\\begin{array}{c c c c c c c c c c c c} 1 \\end{array}$\n")["퇴화반복"])
check("audit no marker", pdf_audit.audit_text("## 1페이지\n본문\n")["완료표식"] is None)

# ─── 장 구분 프로파일 로더(파일 IO와 분리한 순수 파싱을 픽스처로 검증) ───
# 실사용 데이터(장구분.toml)의 개수를 못 박으면 사용자가 장을 편집할 때 테스트가
# 깨진다(검토단 지적) — 로더 '동작'을 인라인 픽스처로 고정하고, 실데이터는 구조만 본다.
_fx = {"book": [
    {"name": "책A", "chapters": [{"page": 5, "title": "제1장 가"},
                                {"page": 2, "title": "제2장 나"}]},
    {"name": "책B", "force_scan": True, "chapters": [{"page": 1, "title": "장"}]},
    {"name": "책C", "chapters": [{"page": 0, "title": "잘못된 쪽"},  # page<1 → 건너뜀
                                {"page": 9, "title": ""}]},         # 빈 제목 → 건너뜀
    {"name": "", "chapters": [{"page": 1, "title": "이름없음"}]},   # 이름 없음 → 무시
]}
_prof, _fscan = pdf_chapters._parse_profiles(_fx)
check("parse book chapters", _prof["책A"] == {5: "제1장 가", 2: "제2장 나"})
check("parse force_scan", _fscan == {"책B"} and _prof["책B"] == {1: "장"})
check("parse skip bad entries", "책C" not in _prof)   # 유효 항목이 하나도 없음
check("parse skip noname", all(n for n in _prof))
# 실데이터는 구조만 확인 — 개수는 사용자가 편집해도 되도록 못 박지 않는다.
# 장구분.toml 은 개인 프로파일이라 저장소에 없다(장구분.example.toml 만 배포).
# 그러니 새로 설치한 PC에서는 이 두 건을 건너뛴다 — 없다고 실패로 세면
# 설치하자마자 "도구가 깨졌다"는 잘못된 인상을 준다. 파일 없이도 도구는
# 정상 동작한다(pdf_chapters 가 장 헤딩만 빼고 진행한다).
if pdf_chapters.PROFILE_PATH.is_file():
    _ch = pdf_chapters.for_book("대학물리 교재")
    check("chapters loaded", len(_ch) >= 1
          and all(isinstance(p, int) and p >= 1 and t.strip() for p, t in _ch.items()))
    check("chapters force_scan flag", pdf_chapters.force_scan("전기회로이론") is True)
else:
    print(f"  [건너뜀] 장구분.toml 이 없어 실데이터 2건을 건너뜁니다 "
          f"({pdf_chapters.PROFILE_PATH.name} 은 개인 설정이라 배포되지 않습니다)")
check("chapters unknown book", pdf_chapters.for_book("없는책") == {})
check("chapters toc", pdf_chapters.toc_block({11: "제1장 가", 37: "제2장 나"})
      == ["## 장 구분(자동 감지)", "", "- 제1장 가 — 11페이지", "- 제2장 나 — 37페이지", ""])
check("chapters toc empty", pdf_chapters.toc_block({}) == [])

# ─── 그림 링크 괄호 안전화(재실행 '(1)' 이름에서 링크가 끊기던 결함) ───
check("link plain", pdf_ocr.md_link_path("책_OCR_images/p1_fig1.png")
      == "책_OCR_images/p1_fig1.png")
check("link paren", pdf_ocr.md_link_path("책_OCR (1)_images/p1_fig1.png")
      == "<책_OCR (1)_images/p1_fig1.png>")
check("audit link plain",
      pdf_audit.fig_links("![그림](a_images/p1.png)") == ["a_images/p1.png"])
check("audit link paren",  # 각괄호 형식도 경로를 온전히 읽어야 한다
      pdf_audit.fig_links("![그림](<a (1)_images/p1.png>)") == ["a (1)_images/p1.png"])
# 공백 포함 경로도 <>로 감싸야 CommonMark가 렌더한다(검토단: 2,349개 링크 소실)
check("link space", pdf_ocr.md_link_path("대학물리 교재_OCR_images/p1.png")
      == "<대학물리 교재_OCR_images/p1.png>")
# 감사는 <> 없이 공백/괄호가 든 경로를 '렌더 안 됨'으로 잡는다(파일 존재와 무관)
check("audit render-broken space",
      pdf_audit.render_broken_links("![그림](대학물리 교재_images/p1.png)")
      == ["대학물리 교재_images/p1.png"])
check("audit render-ok wrapped",  # <>로 감싼 것은 렌더되므로 결함 아님
      pdf_audit.render_broken_links("![그림](<대학물리 교재_images/p1.png>)") == [])
check("audit render-ok nospace",
      pdf_audit.render_broken_links("![그림](em_images/p1.png)") == [])

# ─── clean_text: 수식 스팬 보호(검토단 B2 수정 고정) + 수식 밖 노이즈 정리 ───
ct = pdf_text.clean_text
check("ct math bar keep", ct("확률 $P(A | B)$ 이다") == "확률 $P(A | B)$ 이다")
check("ct math bracket keep",
      ct(r"$\langle \phi | \psi \rangle$") == r"$\langle \phi | \psi \rangle$")
check("ct outside bar clean", ct("abc | def") == "abc def")       # 수식 밖은 여전히 정리
check("ct mixed", ct("앞 | $a | b$ 뒤 | 끝") == "앞 $a | b$ 뒤 끝")
check("ct noise empty", ct("| | |") == "")
check("ct spaces", ct("본문   여러   공백") == "본문 여러 공백")

# ─── parse_pages: 페이지 지정 파싱(스플라이스 순수 함수) ───
check("pages single", pdf_splice.parse_pages(["5"]) == [5])
check("pages range", pdf_splice.parse_pages(["3-6"]) == [3, 4, 5, 6])
check("pages mixed dedup", pdf_splice.parse_pages(["5", "3-4", "4"]) == [3, 4, 5])


def _exits(args):  # 잘못된 입력은 exit_with_message로 종료(SystemExit)
    try:
        pdf_splice.parse_pages(args)
        return False
    except SystemExit:
        return True


check("pages bad token", _exits(["abc"]))
check("pages bad range", _exits(["6-3"]))

# ─── is_caption_like: 캡션/본문 판정(캡션 흡수 오판 방지) ───
cap = pdf_ocr.is_caption_like
check("cap figure", cap("그림 5.1 연산증폭기"))
check("cap table", cap("표 17.2 대칭성의 영향"))
check("cap short desc", cap("짧은 설명 조각"))
check("cap problem not", not cap("4.18 다음 회로의 전류를 구하라"))
check("cap long body not", not cap("이 문장은 캡션으로 보기에는 충분히 길어서 "
                                   "본문 문단으로 판정되어야 하는 긴 설명이다"))
check("cap empty not", not cap("   "))

# ─── load_tesseract_result: tsv/txt 짝짓기·게이트 ───
with tempfile.TemporaryDirectory() as td:
    base = Path(td) / "t"
    hdr12 = "\t".join(["level"] * 12)
    base.with_suffix(".tsv").write_text(
        hdr12 + "\n"
        + "4\t1\t1\t1\t1\t0\t10\t10\t100\t20\t-1\t\n"
        + "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t95\t안녕\n"
        + "5\t1\t1\t1\t1\t2\t60\t10\t50\t20\t95\t세$상\n"
        + "4\t1\t1\t1\t2\t0\t10\t40\t100\t20\t-1\t\n"
        + "5\t1\t1\t1\t2\t1\t10\t40\t40\t20\t5\t잡음\n",
        encoding="utf-8")
    base.with_suffix(".txt").write_text("안녕 세$상\n잡음\n", encoding="utf-8")
    lines = pdf_ocr.load_tesseract_result(base)
    check("tsv pair count", len(lines) == 1)  # 저신뢰 줄은 탈락
    check("tsv $ escape", lines and lines[0]["text"] == r"안녕 세\$상")
    check("tsv box", lines and lines[0]["x0"] == 10 and lines[0]["y1"] == 30)

print(f"\n{'ALL PASS' if not FAIL else f'{len(FAIL)}건 실패'} "
      f"(총 {TOTAL}건)")
sys.exit(1 if FAIL else 0)
