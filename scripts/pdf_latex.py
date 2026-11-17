"""수식 LaTeX 잔재 정리 — MFR 출력에서 환각·조사 오인 잔재를 걷어내는 순수 함수.

pdf_ocr가 인식한 LaTeX마다 clean_latex()를 통과시킨다. 모델·외부 프로세스에
의존하지 않으므로 scripts\\tests\\test_pure.py가 이 모듈을 직접 검증한다.

정리 순서: \\protect 제거 → 꼬리 반복 절단 → 렌더러 호환 정규화(치환·연발 압축)
→ 조사 장식 절제 → 중괄호 균형 → 빈 꼬리 제거(균형과 고정점까지 반복) → 압축.
"""

from __future__ import annotations

import re


# 인라인 수식 박스가 뒤따르는 한글 조사(이/가/는/도/를 등)까지 덮으면, 수식 인식기는
# 그 글자를 의미 없는 LaTeX 기호로 잘못 만들어 식 끝에 붙인다. 실측된 잔재 형태:
#   \circ}  \circ\}  \circ]  \circ[  \circ1  \circ        (← '이'를 \circ 로 오인)
#   \triangleright]   \Phi]   \Box]   \square]            (← 다른 글자 오인)
#   \! \left] \right.  같은 자투리 묶음
# 끝에 오는 이런 '의미 없는 장식 토큰 + 짝 안 맞는 괄호'만 제거한다.
# 단, 각도 표기(^{\circ})와 합성함수(f \circ g: \circ 뒤에 피연산자가 옴)는 보존한다.
# 잔재 명령 앞에는 빈칸·thin space(\,)만 흡수한다(괄호는 식의 일부이므로 건드리지 않음).
_LEAD = r"(?:\s|\\[,!;:])*"
# 잔재 명령 뒤에 붙는 짝 안 맞는 괄호·\} \] \left] \right. 등 의미 없는 자투리.
_JUNK_PIECE = r"(?:\\left[\]\)]|\\right[.\]\)]|\\[}\])]|[{}\[\]()1])"
_TAIL = rf"(?:{_JUNK_PIECE}|\s|\\[,!;:])*"
# 2계층 절제(검토단이 확인한 실수학 파괴 방지):
#   확실 계층(\circ 등 장식 글리프) — 식 끝이면 그대로 잔재로 본다.
#   위험 계층(\Phi \rangle \dagger \S \simeq — 'E|\psi\rangle', '…=\Phi'처럼
#   실수식이 이 기호로 끝나는 표준 표기가 존재) — 뒤에 짝 안 맞는 괄호
#   자투리가 실제로 붙어 있을 때만 잔재로 본다('\Phi]'는 절제, '=\Phi'는 보존).
_PARTICLE_SAFE = r"(?:\\circ|\\triangleright|\\Box|\\square|\\flat)"
_PARTICLE_RISKY = r"(?:\\Phi|\\rangle|\\simeq|\\dagger|\\ddagger|\\ddag|\\S)"
_PARTICLE_ARTIFACT = re.compile(
    _LEAD + rf"(?:{_PARTICLE_SAFE}{_TAIL}"
    rf"|{_PARTICLE_RISKY}(?:\s|\\[,!;:])*{_JUNK_PIECE}{_TAIL})$"
)
_DEGREE_END = re.compile(r"\^\s*\{?\s*\\circ\s*\}?\s*$")  # 각도: 90^{\circ}


def _strip_circ_artifact(latex: str) -> str:
    """식 끝의 '한글 조사 오인식' 장식 잔재를 제거한다(각도·합성함수는 보존)."""
    latex = latex.strip()
    if _DEGREE_END.search(latex):   # 각도 표기는 건드리지 않음
        return latex
    cleaned = _PARTICLE_ARTIFACT.sub("", latex).strip()
    # 전부 지워지면(원래 장식뿐이었으면) 원본 유지 — 과도 제거 방지
    return cleaned if cleaned else latex


def _balance_latex(latex: str) -> str:
    """LaTeX 중괄호 균형을 맞춘다(내용은 최대한 보존).

    - 짝 없는 닫기 '}'(앞에 여는 게 없는 것)는 제거한다.   예) '} x' → ' x'
    - 끝까지 안 닫힌 여는 '{'는 그만큼 닫기 '}'를 보충한다. 예) '\\mathbf{E_{0}' → '\\mathbf{E_{0}}'
    - 이스케이프된 \\{ \\} \\\\ 는 중괄호로 세지 않는다. 균형 잡힌 식은 그대로 둔다.
    """
    s = latex.strip()
    out: list[str] = []
    depth = 0
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:        # \{ \} \\ 등 이스케이프는 통째로 보존
            out.append(s[i:i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
            out.append(c)
        elif c == "}":
            if depth > 0:                  # 짝 있는 닫기만 유지
                depth -= 1
                out.append(c)
            # depth==0이면 짝 없는 닫기 → 버림
        else:
            out.append(c)
        i += 1
    res = "".join(out)
    if depth > 0:                          # 안 닫힌 여는 괄호만큼 닫아 준다
        res += "}" * depth
    return res.strip() or s


# 식 끝의 '내용 없는 명령'(예: \underline{{}} — 조사 오인 잔재가 균형 보정을 거친 형태).
# 중괄호 안이 공백·중괄호뿐이면 내용이 없는 것이므로 명령째 제거해도 안전하다.
_EMPTY_CMD_TAIL = re.compile(r"\\[A-Za-z]+\s*(?:\{[\s{}]*\})+\s*$")


def _strip_empty_cmd_tail(latex: str) -> str:
    """식 끝의 내용 없는 명령 그룹을 제거한다(반복 적용). 예) '\\underline{{}}' 제거."""
    prev = None
    while prev != latex:
        prev = latex
        latex = _EMPTY_CMD_TAIL.sub("", latex).strip()
    return latex or prev


# MFR 환각 잔재: \protect(수식 안에서 무의미한 no-op 명령)와,
# 식 끝에서 같은 토큰이 5회 이상 반복되는 꼬리(예: \mathrm{o} \mathrm{o} ...).
# 4회는 y'''' 같은 실제 표기가 있어 건드리지 않는다.
_PROTECT_CMD = re.compile(r"\\protect(?![A-Za-z])\s*")
_REPEAT_TAIL = re.compile(
    r"((?:\\[A-Za-z]+)(?:\{[^{}]*\})?)(?:(?:\s|\\[,;:!])*\1){4,}(?:\s|\\[,;:!])*$"
)
# 식 끝의 내용 없는 첨자(예: x^{} — 꼬리 제거가 남긴 빈 위첨자/아래첨자)와
# 명령 없이 남은 빈 그룹({} — 치환이 명령 이름만 지웠을 때의 잔해)
_EMPTY_SCRIPT_TAIL = re.compile(r"[\^_]\s*\{\s*\}\s*$")
_EMPTY_GROUP_TAIL = re.compile(r"(?:\s*\{\s*\})+\s*$")
# 간격 명령·공백뿐인 수식(예: \quad 60연발 환각) — 내용이 없으므로 통째로 버린다
_ONLY_SPACING = re.compile(r"^(?:\\[,;:! ]|\\quad|\\qquad|[\s~])*$")

# ─── 렌더러 호환 정규화: MFR이 내놓는 MathJax/KaTeX 미지원 명령을 치환·정리 ───
# 5권 72,816개 수식 전수 인벤토리로 실측된 명령만 다룬다(추측 금지).
_CIRCLED = dict(zip((str(i) for i in range(21)), "⓪①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"))
_COMPAT_SUBS: list[tuple[re.Pattern, object]] = [
    (re.compile(r"\\root\s*(\{[^{}]*\}|[0-9]+)?\s*\\of(?![A-Za-z])"),
     lambda m: rf"\sqrt[{m.group(1).strip('{} ')}]" if m.group(1) else r"\sqrt"),
    (re.compile(r"\\root(?![A-Za-z])"), r"\\sqrt"),
    (re.compile(r"\\of(?![A-Za-z])"), " "),
    (re.compile(r"\\kern\s*-?\s*\\nulldelimiterspace"), " "),
    (re.compile(r"\\nulldelimiterspace(?![A-Za-z])"), " "),
    # \o: 전자기학·대학물리에서 φ/0의 오인으로 실기호 자리에 옴(E_{\o}=E0 등).
    # 조사쌍 '\o|'('이')만 절제하고, 그 외에는 최근접 글리프 o로 렌더만 살린다.
    # 치환 결과는 {}로 감싼다 — 앞 명령과 붙어 \beta\o→\betao처럼 접합되는 것 방지.
    (re.compile(r"(?<!\\)\\o(?![A-Za-z])\s*\|"), " "),
    (re.compile(r"(?<!\\)\\o(?![A-Za-z])"), "{o}"),
    # \O: 예전에는 자속 Φ의 오인이라 보고 \Phi로 바꿨으나, 실측 결과 대부분
    # 한글 조사('이다')의 오인식이었다 — 5권에서 수식 끝 \Phi 72건이 전부
    # '4\pi\Phi 이다'처럼 없던 기호를 만들어 냈다(원본은 '4π이다'). Φ는 전자기학·
    # 물리에서 실재하는 기호라 가짜가 섞이면 판별이 불가능하다. \o와 같이
    # 최근접 글리프로만 남긴다(의미를 지어내지 않는다).
    (re.compile(r"\\O(?![A-Za-z])"), "{O}"),
    (re.compile(r"\\(beta|theta)i(?![A-Za-z])"), r"\\\1{i}"),
    (re.compile(r"\\setlength\s*\{[^{}]*\}\s*\{[^{}]*\}"), " "),
    (re.compile(r"\\b(?=triangleleft|subseteq|supseteq)|\\B(?=varLambda)"), "\\\\"),
    (re.compile(r"\\hdots(?![A-Za-z])"), r"\\ldots"),
    (re.compile(r"\\textcircled\s*\{\s*(\d{1,2})\s*\}"),
     lambda m: _CIRCLED.get(m.group(1), f"({m.group(1)})")),
    (re.compile(r"\\(?:textcircled|textsc|textnormal|textsl)(?![A-Za-z])"), r"\\text"),
    (re.compile(r"\\emph(?![A-Za-z])"), r"\\mathit"),
    (re.compile(r"\\mathbold(?![A-Za-z])"), r"\\boldsymbol"),
    (re.compile(r"\\underbar(?![A-Za-z])"), r"\\underline"),
    (re.compile(r"\\Bar(?![A-Za-z])"), r"\\bar"),
    (re.compile(r"\\cdotp(?![A-Za-z])"), r"\\cdot"),
    (re.compile(r"\\mathellipsis(?![A-Za-z])"), r"\\ldots"),
    (re.compile(r"\\pounds(?![A-Za-z])"), r"\\mathcal{L}"),  # 라플라스 ℒ 오인
    (re.compile(r"\\AA(?![A-Za-z])"), r"\\text{Å}"),
    (re.compile(r"\\copyright(?![A-Za-z])"), "©"),
    (re.compile(r"\\slash(?![A-Za-z])"), "/"),
    (re.compile(r"\\sp(?![A-Za-z])"), "^"),   # plain-TeX 위첨자
    (re.compile(r"\\sb(?![A-Za-z])"), "_"),
    (re.compile(r"\\i(?![A-Za-z])"), "{i}"),  # 점 없는 i — 수식에선 i가 의도
    (re.compile(r"\\j(?![A-Za-z])"), "{j}"),
    (re.compile(r"\\rq(?![A-Za-z])"), "'"),
    (re.compile(r"\\(?:lefteqn|footnote)(?![A-Za-z])"), ""),  # 뒤 {내용}은 남긴다
    (re.compile(r"\\(?:hfill|break|linebreak|nolinebreak|smallskip|medskip"
                r"|bigskip|indent|enskip|expandafter|vrule|lower|sl|em|mit"
                r"|romannumeral|uppercase|def|newcommand|endaligned|pint"
                r"|math|put"
                r")(?![A-Za-z])"), " "),
]
# 한글 조사 오인 글자쌍('이'=ㅇ+ㅣ → \omicron\parallel 등)과 수학 모드에
# 존재하지 않는 단글자 텍스트 액센트 명령(\b \c \d \v ... — 전수 실측 목록).
# 뒤에 영문자가 오면 다른 명령이므로 건드리지 않는다. (\o \O는 실기호 오인이라
# 위 _COMPAT_SUBS에서 보존형으로 치환하고 여기서 다루지 않는다.)
_GLYPH_ARTIFACTS = [
    re.compile(r"\\omicron\s*\\parallel"),
    re.compile(r"\\omicron(?![A-Za-z])"),
    re.compile(r"(?<!\\)\\(?:ss|sc|ae|[bcdelpqruvwxyABGL])(?![A-Za-z])"),
]
_EQ_RUN = re.compile(r"(?:=[\s~]*){5,}")            # 괘선 ═══ 오인(= 5연발 이상)
# 연접 반복(tandem repeat): 같은 조각이 5회 이상 잇달아 나오면 자기회귀 반복 환각이다.
# 단일 토큰만 보는 _TOKEN_RUN의 사각지대(구절 반복 '{\tau}{\bot}…', 중괄호 묶음
# 반복 '{\overline{{\circ}}}…', 막대 잡음 '\boldsymbol{|…')를 덮는다.
# 5회 이상으로 잡아 4계 도함수(y\prime\prime\prime\prime)는 건드리지 않는다.
_TANDEM_RUN = re.compile(r"(.{2,40}?)\1{4,}")
# 행렬·배열·정렬 환경은 같은 행이 정당하게 반복된다(영행렬, 전(全)1 행렬, 정렬식의
# 반복 항) — 압축 제외. align/gather/eqnarray/split/tabular 계열까지 포함한다
# (검토단 실측: 이들 환경의 정당한 행 반복이 압축되던 사각지대).
_MATRIX_ENV = re.compile(
    r"\\begin\{(?:matrix|array|pmatrix|bmatrix|vmatrix|Vmatrix|smallmatrix|cases"
    r"|aligned|align|alignat|flalign|gather|gathered|eqnarray|split|multline"
    r"|tabular|subarray)\*?\}")
# 반복 단위가 간격 명령뿐이면 환각이 아니라 배치용 여백이다(수식과 단위 라벨 사이의
# '\ \ \ \', 들여쓰기 '\qquad\qquad…') — 내용이 아니므로 건드리지 않는다.
_SPACING_CMD = re.compile(r"\\(?:quad|qquad|[,;:!]|\s)")
# 구조적 환각의 표지: LaTeX 명령(\cmd)이나 중괄호 묶음({...}). 자기회귀 반복 환각은
# 이런 구조를 통째로 되풀이한다('{\overline{{\circ}}}', '{\tau}{\bot}', '\overset{…').
_STRUCT_TOKEN = re.compile(r"\\[A-Za-z]|[{}]")


def _collapse_tandem(m: re.Match) -> str:
    """연접 반복을 1회로 줄인다.

    단, 두 경우는 보존한다:
      1) 간격 조각의 반복 — 배치용 여백이다.
      2) 순수 숫자·문자·연산자의 반복 — 정당한 수치·내용이다(검토단 B1 실측:
         '1010101010'·'2.0000000000'·'(1-x)(1-x)…'가 파괴되던 사각지대).
    구조적 환각(LaTeX 명령/중괄호 묶음의 연발)만 1회로 줄인다.
    """
    unit = m.group(1)
    core = _SPACING_CMD.sub("", unit)
    if not core.strip():                       # 간격 명령뿐 → 배치용 여백, 보존
        return m.group(0)
    if not _STRUCT_TOKEN.search(core):         # 순수 수치·문자 반복 → 정당한 내용, 보존
        return m.group(0)
    return unit
_TOKEN_RUN = re.compile(                             # 같은 토큰 8회 이상 연속 → 1회
    r"((?:\\[A-Za-z]+)(?:\{[^{}]*\})?)(?:(?:\s|\\[,;:!])*\1){7,}")
_EMPTY_SCRIPT = re.compile(r"[\^_]\s*\{\s*\}")       # 빈 첨자(절제 잔해) — 위치 불문


def _normalize_compat(s: str) -> str:
    """미지원 명령 치환 + 오인 글자쌍 절제 + 환각 연발 압축. 내용은 보존한다."""
    for pat, rep in _COMPAT_SUBS:
        s = pat.sub(rep, s)
    for pat in _GLYPH_ARTIFACTS:
        s = pat.sub(" ", s)
    s = _EQ_RUN.sub("= ", s)
    s = _TOKEN_RUN.sub(r"\1", s)
    if not _MATRIX_ENV.search(s):      # 행렬의 정당한 행 반복은 보존
        s = _TANDEM_RUN.sub(_collapse_tandem, s)
    s = _EMPTY_SCRIPT.sub("", s)
    return s.strip()


# ─── AI 입력 압축: 렌더 결과가 같은 표기를 짧은 쪽으로 통일 ───
# 이 도구의 산출물은 AI 입력용이다 — MFR의 장식 간격 토큰(\, \; ...)과
# 낱자로 띄운 함수명(\operatorname{c o s})은 토큰만 낭비한다.
# 숫자 '사이 공백' 접합은 하지 않는다 — '1 4 6 4 1'(이항계수 나열)이
# '14641'로 뭉개지는 실수치 오염이 검토단 실측으로 확인되어 제거했다.
_SPACING_TOKEN = re.compile(r"\\[,;:!]|(?<!\\)~")
_DIGIT_PUNCT = re.compile(r"(?<=\d) *([.,]) *(?=\d)")
_OPNAME = re.compile(r"\\operatorname(\*?)\s*\{\s*([A-Za-z](?:\s?[A-Za-z])*)\s*\}")
_STD_FUNCS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "lim", "limsup",
    "liminf", "max", "min", "det", "deg", "dim", "ker", "arg", "gcd", "sup",
    "inf", "Pr", "hom",
}


def _compact_latex(s: str) -> str:
    """렌더 동등 압축: 함수명 통일 → 간격 토큰 → 구두점 주변 공백 → 공백 정리."""
    def _op(m):
        name = m.group(2).replace(" ", "")
        if name in _STD_FUNCS:
            return "\\" + name + " "
        return f"\\operatorname{m.group(1)}{{{name}}}"

    s = _OPNAME.sub(_op, s)
    s = _SPACING_TOKEN.sub(" ", s)
    s = _DIGIT_PUNCT.sub(r"\1", s)
    s = re.sub(r" +([_^])", r"\1", s)  # 첨자 앞 공백(렌더 동일)
    return re.sub(r" {2,}", " ", s).strip()


# 완결된 식 뒤에 홀로 매달린 \Phi — 수식 바로 뒤에 오는 한글 조사('이다/이고/
# 이면/므로')를 MFR이 Φ로 잘못 읽은 것이다. 실측 5권 63건이 전부 이 꼴이었다
# ('4\pi\Phi 이다'의 원본은 '4π이다'). Φ는 전자기학·물리에서 실재하는 기호라
# 가짜가 섞이면 판별이 불가능하므로 지운다.
# 앞이 연산자면('=\Phi', '+\Phi') 진짜 Φ일 수 있으므로 건드리지 않는다 —
# 값·닫는 괄호·문자로 식이 끝난 뒤에 공백을 두고 매달린 경우만 지운다.
_DANGLING_PHI = re.compile(r"(?<=[0-9})\]A-Za-z])\s+\\Phi\s*$")


def clean_latex(latex: str) -> str:
    """수식 LaTeX 최종 정리: 환각 잔재·조사 장식 제거 + 중괄호 균형 + 빈 꼬리 제거.

    꼬리 제거(_strip_empty_cmd_tail)는 바깥 그룹의 닫는 중괄호까지 삼킬 수 있어
    ('x^{\\underline{{}}}' → 'x^{') 제거 후 반드시 재균형해야 한다 — 균형이
    잡힐 때까지 제거→재균형을 반복한다(각 단계가 길이를 줄이므로 항상 끝난다).
    """
    s = _PROTECT_CMD.sub("", latex).strip() or latex
    s = _REPEAT_TAIL.sub("", s).strip() or s
    s = _normalize_compat(s)
    s = _balance_latex(_strip_circ_artifact(s))
    prev = None
    while prev != s:
        prev = s
        s = _balance_latex(_strip_empty_cmd_tail(s))
        s = _EMPTY_SCRIPT_TAIL.sub("", s).strip()
        s = _EMPTY_GROUP_TAIL.sub("", s).strip()
        # 끝의 외로운 백슬래시 제거 — 인라인 위빙('$latex$')에서 닫는 $와 합쳐져
        # \$가 되면 구분자가 사라진다. \\ 행바꿈(짝수 개)은 보존한다.
        m = re.search(r"\\+$", s)
        if m and len(m.group()) % 2:
            s = s[:-1].rstrip()
    s = _compact_latex(s)
    s = _DANGLING_PHI.sub("", s).strip()
    return "" if _ONLY_SPACING.match(s) else s


# 홑 기호 수식 판정(그림 라벨 누수 색출에 쓴다)
_SINGLE_SYMBOL = re.compile(r"^(?:[A-Za-z0-9]|\\[A-Za-z]+)$")
