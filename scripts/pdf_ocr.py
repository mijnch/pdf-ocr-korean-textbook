"""PDF OCR — 본문·수식·그림을 분리 인식해 읽기 순서대로 Markdown으로 추출.

입력 폴더의 PDF를 읽어, 페이지마다 레이아웃을 분석해 영역별로 최적의 엔진에
보내고 읽기 순서대로 정돈된 '원본이름_OCR.md'를 출력 폴더에 저장한다.

이 파일은 흐름(쪽 준비 → 인식 → 조립 → 책 단위 확정)을 맡고, 단계별 판정은 하위 모듈에
있다 — pdf_embedded(내장 텍스트층), pdf_flow(영역 분류·읽기 순서), pdf_markdown(Markdown
조립), pdf_pageno(인쇄 쪽번호), pdf_text(Tesseract), pdf_layout·pdf_math·pdf_latex(레이아웃·
수식), pdf_table(표). 하위 모듈의 이름은 여기서 재수출하므로 pdf_ocr.X로도 부를 수 있다.

영역별 라우팅:
  - 그림/표  → OCR하지 않고 PNG로 저장하고 ![그림] 링크 + 캡션을 바로 아래 병합
  - 수식      → pix2text(MFD/MFR)로 LaTeX 변환 ($$...$$ / $...$, 우측 수식 번호 부착)
  - 본문/제목 → 내장 텍스트(있으면) 또는 Tesseract(한국어+영어, 다단이면 칼럼별)
  - 색칠된 강조/예제 박스 → 원래 위치에 > [참고] 인용 블록
  - 머리말/쪽번호/워터마크 → 버림. 단, 하단의 * † ‡ 각주는 본문으로 보존

본문 출처는 페이지마다 자동 선택한다:
  - 내장 텍스트 레이어가 있고 책 표본에서 믿을 만하면(embedded_layer_agreement)
    그대로 활용한다. 단, 깨지기 마련인 수식 부분은 버리고 새로 인식한 LaTeX로 채운다.
  - 아니면 Tesseract로 인식한다 — 두 분할 모드 × 두 해상도의 판독을 줄마다
    투표로 고르고(vote_lines), 원본이 300dpi 미만이면 키워서 읽는다.
  - 줄 끝에서 잘린 한글 낱말은 책이 다 모인 뒤 그 책의 표기로 잇는다(resolve_joins).
  - 본문 속 '$'는 \\$로 이스케이프한다 — 삽입되는 수식 구분자 $와 짝을 이뤄
    본문이 수식으로 렌더링되는 것을 막는다(스캔본에서는 대부분 오인식 잡음).

성능:
  - 모델(레이아웃/수식)은 전체 실행에서 1회만 로드해 재사용한다.
  - 쪽들은 겹쳐 흐른다: 메인 스레드가 다음 쪽을 렌더링하고(pdfium 제약,
    prepare_page), 선행 스레드가 그 쪽의 레이아웃·수식 검출을 한 뒤 Tesseract를
    띄운다(precompute_page) — 이번 쪽의 수식 인식(MFR)과 동시에 돈다. 페이지 최대
    병목은 책 종류에 따라 다르다(실측): 내장 텍스트본은 MFR, 스캔본은 Tesseract.
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

import pdf_audit
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
    JOIN,
    MASK_MARGIN,
    _MATH_SPAN,
    MIN_LINE_CONF,
    OCR_MIN_DPI,
    PSM_CANDIDATES,
    RENDER_DPI,
    RESCUE_MIN_CONF,
    RESCUE_MIN_WORDISH,
    SCAN_CELL_MIN_CONF,
    SCAN_CELL_SCALES,
    TESS_LANG,
    _WATERMARK_RE,
    _WORDISH,
    clean_text,
    detect_columns,
    detect_rotation,
    join_lines,
    load_tesseract_result,
    make_scan_cell_fn,
    merge_rescue_lines,
    native_scan_dpi,
    ocr_region_lines,
    ocr_region_text,
    resolve_joins,
    _squash,
    start_tesseract,
    tesseract_lines,
    vote_lines,
)

# 쪽 조립 단계별 하위 모듈 — 이름은 여기서 재수출한다(기존 호출부·테스트 호환).
from pdf_embedded import (  # noqa: F401
    embedded_lines,
    graft_superscripts,
    has_embedded_text,
    _is_sane_char,
    _page_char_index,
    _sup_runs,
    superscript_marks,
    superscript_severed,
    table_superscript_partial,
)
from pdf_flow import (  # noqa: F401
    CALLOUT_COLOR_RATIO,
    _CALLOUT_JUNK,
    CALLOUT_REDO_GAIN,
    _CAPTION_RE,
    _CAPTION_REF,
    assemble_region_text,
    assign_lines,
    _by_col_then_row,
    clean_column_bands,
    colored_ratio,
    geometric_gutter,
    infer_column,
    is_callout,
    is_caption_like,
    is_eq_label,
    order_flow,
    _overlap_ratio,
    _row_order,
    tinted_ratio,
    _weave_line,
)
from pdf_markdown import (  # noqa: F401
    _accept_table,
    ai_preamble,
    attach_orphan_captions,
    body_chars,
    has_ink,
    md_link_path,
    render_flow,
    save_figure,
    split_semantic_heading,
)
from pdf_pageno import (  # noqa: F401
    HEADER_PROBE_RATIOS,
    _PAGE_HEAD_ANY,
    confirm_page_numbers,
    fill_page_numbers,
    printed_page_number,
    read_printed_page,
    rising_page_numbers,
    settle_page_numbers,
    settle_pairs,
    split_repeated_page_numbers,
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
HEADER_BAND_RATIO = tuning.get("layout", "header_band_ratio")      # 머리말 띠
# 상단 띠 안에 있어도 이보다 긴 글은 머리말로 보지 않는다 — 상단 여백이 좁은
# 자료의 첫 줄(장·절 제목)이 매 쪽 잘려 나가는 것을 막는다. 5권 실측 머리말은
# 최장 38자(장제목 + 쪽번호)이고, 잘려 나가던 첫 줄들은 40자를 넘었다.
HEADER_MAX_CHARS = 60
HEADER_EXT_RATIO = tuning.get("layout", "header_ext_ratio")        # 확장 띠(내용 병행)
FOOT_BAND_RATIO = tuning.get("layout", "foot_band_ratio")          # 내장책 쪽번호 띠
# Tesseract를 MFR과 겹쳐 돌리는 전용 스레드 풀. 고해상 페이지의 두 Tesseract
# 패스(400dpi 주 + 200dpi 보충)는 서로 의존이 없어 함께 돈다 — 실측 A/B(공학수학1
# 5쪽): 워커 1→2에서 10.66→8.95초/쪽, 정확도 손실 0. 여기에 다음 쪽의 두 패스가
# 선행 스레드에서 미리 올라오므로(precompute_page) 두 쪽 몫인 4를 둔다.
# 외부 프로세스라 GIL 무관, 작업마다 temp 파일명(p{쪽}/p{쪽}r)이 달라 충돌하지 않는다.
TESS_WORKERS = 4
_TESS_POOL = ThreadPoolExecutor(max_workers=TESS_WORKERS)
# 다음 페이지의 레이아웃+수식검출을 현재 페이지 MFR과 겹치는 선행 스레드
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=1)


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


_AGREE_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
EMBED_MIN_AGREE = 0.75   # 내장층 토큰이 Tesseract 판독과 이만큼은 겹쳐야 신뢰한다
EMBED_PROBE_PAGES = 12   # 판정에 쓰는 표본 쪽 수
EMBED_PROBE_MIN_TOKENS = 15


def embedded_layer_agreement(pdf, tmp_dir: Path,
                             sample: int = EMBED_PROBE_PAGES) -> float | None:
    """내장 텍스트층이 믿을 만한지 표본 쪽에서 Tesseract와 맞대어 잰다.

    반환: 내장층 토큰 중 Tesseract도 읽은 것의 비율(중앙값). 층이 없거나
    표본이 모자라면 None(판정 없음 → 기존 동작 유지).

    왜 필요한가: 어떤 책의 내장층은 출판사 선행 OCR이라 한글을 한 글자씩
    띄워 놓아('각 상 당 평 균 전 력') 그대로 쓰면 본문이 무너진다. 지금까지는
    사람이 장구분.toml에 force_scan을 적어 막았는데, **그 파일이 없어지면
    아무 경고 없이 망가진 층이 채택된다.** 실측으로 확인했다 — 전기회로이론
    한 쪽의 낱말 회수율이 96.4%에서 10.8%로 떨어졌고, 자가 감사는 원본과
    대조하지 않으므로 결함을 한 건도 보고하지 않았다.

    문턱 0.75는 4권 실측에서 얻었다(각 12쪽 표본의 중앙값):
      전기회로이론 67.9% · 대학수학 81.0% · 대학물리 91.9% · 신호와시스템 97.1%
    망가진 책과 정상 책 사이가 13포인트 벌어져 있어 그 사이에 둔다.
    설정 파일이 있으면 그쪽이 우선이다 — 이 판정은 그물이지 대체물이 아니다.
    """
    import statistics

    n = len(pdf)
    if n < 4:
        return None
    step = max(1, int(n * 0.6) // sample)
    vals: list[float] = []
    jobs = []   # 렌더링은 메인 스레드에서, 표본 쪽들의 Tesseract는 한꺼번에 돌린다
    try:
        for i in list(range(int(n * 0.2), int(n * 0.8), step))[:sample]:
            page = pdf[i]
            tp = page.get_textpage()
            raw = tp.get_text_bounded()
            tp.close()
            src = set(_AGREE_TOKEN.findall(raw))
            if len(src) < EMBED_PROBE_MIN_TOKENS:
                page.close()
                continue
            image = page.render(scale=RENDER_DPI / 72).to_pil()
            page.close()
            jobs.append((src, _TESS_POOL.submit(pdf_text.tesseract_lines, image, [],
                                                tmp_dir, None, tag=f"probe{i}")))
        for src, fut in jobs:
            lines = fut.result()
            seen = set(_AGREE_TOKEN.findall(" ".join(l["text"] for l in lines)))
            if len(seen) >= EMBED_PROBE_MIN_TOKENS:
                vals.append(len(src & seen) / len(src))
    except Exception:
        return None            # 판정 실패는 판정 없음으로 — 변환을 막지 않는다
    return statistics.median(vals) if len(vals) >= 3 else None


_PREAMBLE_LINE = re.compile(r"(?m)^> \*\*AI 안내\*\*: .*$")


def settle_line_joins(md_path: Path) -> tuple[int, int]:
    """줄바꿈 이음 표식을 책 전체의 표기로 확정한다. 반환: (붙인 수, 띄운 수).

    책이 다 모여야 증거가 가장 많으므로 파일이 완성된 뒤 한 번 돈다
    (판정 규칙은 pdf_text.resolve_joins).
    """
    text = md_path.read_text(encoding="utf-8")
    new, glued, spaced = resolve_joins(text)
    if new != text:
        md_path.write_text(new, encoding="utf-8")
    return glued, spaced


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


def resume_point(md_text: str, pdf_name: str, num_pages: int) -> tuple[int, int] | None:
    """중단된 산출물을 이어 쓸 자리 — (다시 시작할 쪽, 잘라 낼 글자 위치). 못 이으면 None.

    완료 표식이 있으면 다 된 파일이라 잇지 않는다(새 이름으로 따로 만든다). 첫 줄의
    원본 이름이 다르면 다른 책이다. 마지막 쪽 절은 쓰다가 끊겼을 수 있으므로 그 쪽부터
    다시 만든다 — 그 앞의 장 제목('# …')은 다시 쓰이므로 함께 잘라 낸다.
    """
    if "> [변환 완료]" in md_text or not md_text.startswith(f"# {pdf_name}\n"):
        return None
    heads = list(_PAGE_HEAD_ANY.finditer(md_text))
    if not heads:
        return 1, len(md_text)          # 머리만 쓰고 멈췄다
    page = int(heads[-1].group(1))
    if page > num_pages:
        return None
    cut = heads[-1].start()
    before = md_text[:cut].rstrip("\n")
    line_start = before.rfind("\n") + 1
    if before[line_start:].startswith("# "):
        cut = line_start
    return page, cut


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


def prepare_page(page, force_scan: bool = False) -> dict:
    """쪽 하나의 입력을 메인 스레드에서 마련한다(pdfium은 스레드 불안전이다).

      image    기준 렌더링(RENDER_DPI) — 모든 좌표가 이 공간이다.
      hires    스캔 원본이 기준보다 높으면 원본 해상도(상한 HIRES_MAX_DPI) 렌더링.
               수식 크롭·그림 저장·글자 인식이 원본 화질을 쓴다.
      ocr      원본이 OCR_MIN_DPI보다 낮은 스캔 쪽을 그 해상도로 키운 렌더링 —
               글자 인식에만 쓴다. pdfium이 원본 이미지에서 바로 키워야 한다.
               기준 렌더링을 PIL로 다시 키우면 두 번 보간되어 이득이 절반으로
               준다(실측 전자기학 p309 문자 일치율: 원본 87.1%, PIL 확대 89.9%,
               pdfium 확대 92.1%).
      embedded 내장 텍스트층으로 본문을 읽는가(force_scan이면 아니다).
      bands    상단 머리말 띠의 내장 텍스트(인쇄 쪽번호용, HEADER_PROBE_RATIOS 순).
    """
    image = page.render(scale=RENDER_DPI / 72).to_pil()
    tp = page.get_textpage()
    try:
        embedded = has_embedded_text(tp) and not force_scan
        h, w = page.get_height(), page.get_width()
        bands = []
        for ratio in HEADER_PROBE_RATIOS:
            try:
                bands.append(tp.get_text_bounded(left=0, bottom=h * (1 - ratio),
                                                 right=w, top=h))
            except Exception:
                bands.append("")
    finally:
        tp.close()
    ndpi = min(native_scan_dpi(page), HIRES_MAX_DPI)
    hires = page.render(scale=ndpi / 72).to_pil() if ndpi > RENDER_DPI else None
    ocr = (page.render(scale=OCR_MIN_DPI / 72).to_pil()
           if not embedded and ndpi < OCR_MIN_DPI else None)
    return {"image": image, "hires": hires, "ocr": ocr,
            "embedded": embedded, "bands": bands}


def split_regions(regions: list[dict], page_image):
    """레이아웃 영역을 (그림, 본문, 버림 상자, 머리말 띠 높이)로 가른다."""
    image_regions = [r for r in regions if r["kind"] == "image"]
    layout_texts = [r for r in regions if r["kind"] == "text"]
    drop_boxes = [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in regions if r["kind"] == "drop"]
    # 레이아웃이 머리말을 TEXT로 잘못 남긴 페이지의 누수 차단: 페이지 상단 띠에
    # 완전히 들어간 텍스트 영역은 러닝 헤더로 보고 인식에서 가린다.
    # 임계 7.2%: 5권 실측에서 머리말은 y1≤6.8%H에서 끝나고, 본문 첫 줄은
    # (스캔북 포함) y0≥7.3%H에서 시작한다 — 양쪽 모두 여유가 있는 경계값.
    # 이 시점의 영역에는 아직 글자가 없어 길이로는 가를 수 없다(여기 있던 '짧을
    # 때만' 조건은 늘 참이었다). 상단 여백이 좁은 자료의 첫 줄 보호는 인식 뒤
    # 줄 단위로 한다 — process_page의 머리말 띠 필터. 띠에 완전히 들어간 텍스트
    # 영역은 스캔본 표본 150쪽에 한 건도 없었다(러닝 헤더는 레이아웃이 '버림'으로 잡음).
    header_band = HEADER_BAND_RATIO * page_image.height
    in_band = [r for r in layout_texts if r["y1"] <= header_band]
    drop_boxes += [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in in_band]
    _dropped = {id(r) for r in in_band}
    layout_texts = [r for r in layout_texts if id(r) not in _dropped]
    return image_regions, layout_texts, drop_boxes, header_band


def start_text_ocr(page_image, regions: list[dict], detections, ocr_hi,
                   tmp_dir: Path, page_no: int) -> dict:
    """스캔 경로의 전면 인식(Tesseract)을 띄운다 — 기다리지 않는다.

    pdfium을 쓰지 않으므로 선행 스레드가 다음 쪽 몫으로 부를 수 있다. 그래서
    다음 쪽의 Tesseract가 이번 쪽의 수식 인식·마무리와 겹쳐 돈다(스캔본은
    Tesseract가 쪽 시간의 대부분이다). 그림·수식·버림 영역은 흰색으로 가리고
    읽는다 — 환각으로 버려질 검출 상자까지 가리지만, 그런 영역은 그림 조각·
    잡음이라 본문이 아니다(기준 페이지 대조로 출력 동일성 검증됨).
    하단 버림 영역은 각주일 수 있어(레이아웃이 각주·꼬리말·워터마크를 같은
    클래스로 버림) 가리지 않고, 나중에 내용으로 판별해 각주만 살린다.
    반환: 결과를 거둘 때 필요한 것들 — 작업(main·rescue), 채택되지 않은 판독
    (줄 투표의 표), 칼럼, 각주 상자, 인식 배율.
    """
    image_regions, layout_texts, drop_boxes, _hb = split_regions(regions, page_image)
    page_w = page_image.width
    k = ocr_hi.width / page_w if ocr_hi is not None else 1.0
    dpi = round(RENDER_DPI * k)
    foot = [b for b in drop_boxes if b[1] > 0.8 * page_image.height]
    mask = [(r["x0"], r["y0"], r["x1"], r["y1"]) for r in image_regions]
    mask += [(b[0], b[1], b[2], b[3]) for _, b in detections]
    mask += [b for b in drop_boxes if b not in foot]
    bands = detect_columns(layout_texts, page_w)  # 다단이면 칼럼별로 따로 인식
    st = {"k": k, "dpi": dpi, "bands": bands, "foot": foot,
          "alt_main": [], "alt_resc": [], "rescue": None}
    if ocr_hi is None:
        st["main"] = _TESS_POOL.submit(tesseract_lines, page_image, mask, tmp_dir,
                                       bands, tag=f"p{page_no}")
        return st
    # 고해상 입력으로 인식(좌표는 결과 수신 후 환산)
    mask_hi = [tuple(v * k for v in b) for b in mask]
    bands_hi = [(x0 * k, x1 * k) for x0, x1 in bands] if bands else None
    st["main"] = _TESS_POOL.submit(tesseract_lines, ocr_hi, mask_hi, tmp_dir, bands_hi,
                                   dpi, tag=f"p{page_no}", alternates=st["alt_main"])
    # 고해상 사각지대 구조: 400dpi에서 Tesseract가 짧은 들여쓰기 줄을 PSM 불문
    # 놓치는 사례 실측(p567) — 기준 해상도로 한 번 더 읽어 주 결과에 없는 줄만
    # 보충한다. 이 판독은 줄 투표의 표로도 쓴다(vote_lines).
    st["rescue"] = _TESS_POOL.submit(tesseract_lines, page_image, mask, tmp_dir, bands,
                                     tag=f"p{page_no}r", alternates=st["alt_resc"])
    return st


def precompute_page(inp: dict, tmp_dir: Path, page_no: int):
    """쪽 하나의 선행 작업 — 레이아웃 분석 + 수식 검출, 그리고 외부 인식 띄우기.

    선행 스레드에서 다음 쪽 몫으로 돈다(렌더링·텍스트층은 prepare_page가 메인
    스레드에서 이미 마련했다). 반환: (영역, 수식 검출, 전면 인식 작업|None,
    머리말 띠 인식 작업|None). 머리말 띠는 내장 텍스트로 쪽번호를 못 읽을 때만
    미리 읽어 둔다 — read_printed_page가 어차피 그 순서로 읽는다.
    """
    import pdf_layout
    import pdf_math

    image = inp["image"]
    regions = pdf_layout.analyze(image)
    detections = pdf_math.find_formulas(image)
    started = None
    if not inp["embedded"]:
        ocr_hi = inp["ocr"] if inp["ocr"] is not None else inp["hires"]
        started = start_text_ocr(image, regions, detections, ocr_hi, tmp_dir, page_no)
    header = None
    if printed_page_number(inp["bands"][0], page_no) is None:
        header = _TESS_POOL.submit(pdf_text.ocr_top_band, image, tmp_dir,
                                   f"{page_no}_0", HEADER_PROBE_RATIOS[0])
    return regions, detections, started, header


_PROSE_HANGUL = re.compile(r"[가-힣]")
_PROSE_WORD = re.compile(r"[A-Za-z]{3,}")
TEXTBOX_MIN_AREA = 0.10     # 페이지 면적의 이만큼을 넘는 그림만 글상자 후보
TEXTBOX_MIN_HANGUL = 40     # 한글 산문으로 인정할 최소 글자수
TEXTBOX_MIN_WORDS = 15      # 영문 산문으로 인정할 최소 낱말수(3글자 이상)
TEXTBOX_LINE_LETTERS = 6    # 상자 안에서 '문장 줄'로 칠 최소 실질 글자수
TEXTBOX_MIN_TINT = 0.15     # 바탕이 이만큼 칠해져 있어야 글상자 후보
TEXTBOX_TABLE_MIN_TINT = 0.08  # 표로 잡힌 영역의 완화된 바탕 문턱(판정은 더 엄격)
TEXTBOX_MAX_PER_PAGE = 2    # 한 쪽에서 다시 읽어 볼 상자 수 상한


def looks_like_prose(text: str, strict: bool = False) -> bool:
    """크롭 OCR 결과가 '도표 라벨'이 아니라 '문장'인지 판정한다.

    색 배경 예제·정리 상자는 레이아웃 모델이 통째로 그림으로 잡는다. 상자를
    한 번 더 읽어 이 판정을 통과하면 글상자이므로 본문으로 되살린다. 도표는
    라벨이 짧고 흩어져 있어 통과하지 못한다 — 실측: 회로도 한 장의 실질
    글자는 약 25자('Vcc Vb Q3 Q4 Vout Vin1 Vin2 IEE'), 한글은 0자다.

    strict면 한글 산문만 인정한다. 표로 잡힌 영역에 쓴다 — 영문 낱말 기준을
    허용하면 진짜 표가 통과한다(실측: 발진기 비교표 한 장이 영단어 60개,
    한글 0자). 같은 책의 예제 상자는 한글 122~539자였다.
    """
    if len(_PROSE_HANGUL.findall(text)) >= TEXTBOX_MIN_HANGUL:
        return True
    return not strict and len(_PROSE_WORD.findall(text)) >= TEXTBOX_MIN_WORDS


def process_page(page, inp: dict, images_dir: Path, page_no: int,
                 tmp_dir: Path, pre=None) -> tuple[list[str], int, str, int | None]:
    """한 페이지를 인식해 (Markdown 줄, 수식 수, 본문 출처, 인쇄 쪽번호)를 반환한다.

    inp: prepare_page()가 메인 스레드에서 마련한 입력(렌더링·텍스트층 판정·머리말 띠).
      레이아웃·좌표 계산은 기준 해상도(image) 공간에서 하고, Tesseract 입력·수식
      크롭·그림 저장만 고해상(hires)을 써서 원본 화질을 살린다. 저해상 스캔을 키운
      렌더링(ocr)은 글자 인식에만 쓴다.
    pre: precompute_page()가 선행 스레드에서 미리 해 둔 몫(레이아웃·수식 검출·
      띄워 둔 인식 작업). 없으면 여기서 한다.
    """
    import pdf_math

    page_image, hires_image = inp["image"], inp["hires"]
    embedded = inp["embedded"]
    if pre is None:
        pre = precompute_page(inp, tmp_dir, page_no)
    regions, detections, started, header_ocr = pre
    page_w = page_image.width
    k = hires_image.width / page_image.width if hires_image is not None else 1.0
    hires_dpi = round(RENDER_DPI * k)
    image_regions, layout_texts, drop_boxes, header_band = split_regions(regions, page_image)

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

    # 스캔 쪽의 Tesseract는 precompute_page가 이미 띄웠다 — 수식 인식(MFR)과
    # 겹쳐 돈다. 스캔본은 Tesseract가 쪽 시간의 대부분이다(실측).
    textpage = page.get_textpage()
    foot_boxes = started["foot"] if started else []
    try:
        if hires_image is not None:  # 수식 크롭도 원본 해상도로
            latexes = pdf_math.recognize_latex(
                hires_image, [tuple(round(v * k) for v in box) for _, box in detections])
        else:
            latexes = pdf_math.recognize_latex(page_image, [box for _, box in detections])
    except Exception:
        # MFR 실패 시 떠 있는 인식을 거둔다 — Tesseract 자체 오류가 원인(MFR 예외)을
        # 가리지 않게 결과는 버린다.
        for fut in ((started or {}).get("main"), (started or {}).get("rescue"), header_ocr):
            if fut is not None:
                try:
                    fut.result()
                except Exception:
                    pass
        raise
    formulas = [
        {"kind": kind, "text": lx, "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]}
        for (kind, b), lx in ((d, clean_latex(x)) for d, x in zip(detections, latexes))
        if lx
    ]
    n_formulas = len(formulas)
    isolated = [f for f in formulas if f["kind"] == "isolated"]

    # 본문 줄 확보 (내장 텍스트 우선, 없으면 미리 띄운 Tesseract 결과)
    if embedded:
        lines, isolated = embedded_lines(page, textpage, formulas)
        weave: list[dict] = []  # 내장 경로는 줄에 인라인 수식이 이미 들어 있음
        source = "내장 텍스트"
    else:
        lines = started["main"].result()
        k_ocr, alt_main = started["k"], started["alt_main"]
        if k_ocr != 1.0:  # 고해상 좌표 → 기준 공간으로 환산
            for ln in lines + [l for r in alt_main for l in r]:
                for key in ("x0", "y0", "x1", "y1"):
                    ln[key] /= k_ocr
                # 단어 경계도 같은 공간으로 — 보충 줄(기준 해상도)과 섞이므로 필수
                if ln.get("words"):
                    ln["words"] = [(n, a / k_ocr, b / k_ocr) for n, a, b in ln["words"]]
        weave = [f for f in formulas if f["kind"] == "embedding"]  # 스캔: 인라인 수식 재삽입
        ocr_bands = started["bands"]
        source = "Tesseract" + (f"({len(ocr_bands)}단)" if ocr_bands else "")
        if k_ocr != 1.0:
            source += f"·{started['dpi']}dpi" + ("(확대)" if inp["ocr"] is not None else "")
        if started["rescue"] is not None:
            try:  # 기준 해상도 보충 — 실패해도 주 결과에는 영향 없음(보충일 뿐)
                resc = started["rescue"].result()
            except Exception:
                resc = []
            # 줄마다 네 판독(두 PSM × 두 해상도) 중 가장 합의된 것을 고른다
            n_voted = vote_lines(lines, alt_main + [resc] + started["alt_resc"])
            if n_voted:
                source += f"·투표{n_voted}줄"
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
    printed_no = read_printed_page(inp["bands"], page_image, tmp_dir, page_no, header_ocr)

    # 색 배경 글상자 되살리기: 교재의 예제·정리 상자는 배경에 색이 깔려 있어
    # 레이아웃 모델이 통째로 FIGURE로 잡는다. 그러면 전면 OCR 마스크가 상자
    # 안을 아예 읽지 않아, 문제문·풀이·식이 본문에서 통째로 사라진다 —
    # 전자회로 교재 실측 84쪽, 그 쪽들의 본문은 책 평균의 58%뿐이었다
    # (내용 자체는 그림 PNG에 남지만 검색이 되지 않는다).
    # 충분히 큰 그림 영역만 개별 크롭으로 한 번 더 읽어, 문장이 나오면
    # 글상자로 보고 본문으로 되살리고 그림 취급에서 뺀다. 표(TABLE)는 제외한다 —
    # 틀린 표는 표가 없는 것보다 나쁘므로 통째로 PNG에 남기는 원칙을 지킨다.
    textbox_recovered: list[dict] = []
    if not embedded:
        _page_area = page_image.width * page_image.height
        # 크롭 OCR은 페이지 한 장을 다시 읽는 것과 비슷한 비용이다 — 큰 그림을
        # 모두 다시 읽으면 변환 시간이 배로 뛴다. 그래서 후보를 먼저 좁힌다:
        # 바탕이 옅게 칠해진 영역만, 그것도 큰 것 두 개까지. 글상자는 바탕색으로
        # 본문과 구분되게 조판돼 있고, 흰 바탕의 도표는 이 문턱을 넘지 못한다
        # (실측: 예제 상자 59~60%, 흰 바탕 도표 0~5%).
        # 표로 잡힌 영역도 후보에 넣되(예제 상자의 절반 이상이 TABLE로 분류된다 —
        # 테두리와 가로줄이 표처럼 보이기 때문이다), 문턱을 다르게 건다: 상자의
        # 색 띠는 머리 부분뿐이라 전체 유색 비율이 낮게 나오므로 바탕 조건은
        # 낮추고, 대신 한글 산문만 인정한다(looks_like_prose strict). 여기서
        # 되살리는 것은 흐르는 글이지 표가 아니다 — '틀린 표는 표가 없는 것보다
        # 나쁘다'는 원칙은 격자를 가진 Markdown 표를 지어내지 않는다는 뜻이고,
        # 표 자체는 지금도 PNG로 그대로 남는다.
        _cands = []
        for r in image_regions:
            if (r["x1"] - r["x0"]) * (r["y1"] - r["y0"]) < TEXTBOX_MIN_AREA * _page_area:
                continue
            strict = r.get("type") == "TABLE"
            floor = TEXTBOX_TABLE_MIN_TINT if strict else TEXTBOX_MIN_TINT
            if tinted_ratio(page_image, r) <= floor:
                continue
            _cands.append((r, strict))
        _cands.sort(key=lambda rs: -(rs[0]["x1"] - rs[0]["x0"]) * (rs[0]["y1"] - rs[0]["y0"]))
        for _i, (r, _strict) in enumerate(_cands[:TEXTBOX_MAX_PER_PAGE]):
            rec = ocr_region_lines(
                hires_image if hires_image is not None else page_image,
                (r["x0"] * k, r["y0"] * k, r["x1"] * k, r["y1"] * k),
                tmp_dir, f"box{page_no}_{_i}", hires_dpi)
            # 도표 라벨('Q3' 'Vout' '(a)')은 짧다 — 문장 줄만 남긴다. 상자 안의
            # 회로도·그래프는 그림 PNG에 그대로 있으므로 본문에 옮길 이유가 없다.
            keep = [ln for ln in sorted(rec, key=lambda l: (l["y0"], l["x0"]))
                    if len(_WORDISH.findall(ln["text"])) >= TEXTBOX_LINE_LETTERS]
            text = clean_text(join_lines(ln["text"] for ln in keep))
            if not looks_like_prose(text, strict=_strict):
                continue
            textbox_recovered.append({"x0": r["x0"], "y0": r["y0"], "x1": r["x1"],
                                      "y1": r["y1"], "col": r.get("col", 1),
                                      "type": "TEXT", "text": text, "nocap": True})
            fig_boxes.remove((r["x0"], r["y0"], r["x1"], r["y1"]))

    # 그림 내부에 들어온 줄·수식 라벨 제거 (그림 PNG에 이미 포함되어 중복·잡음이 됨).
    # 머리말 띠 안의 줄도 버리되, 레이아웃이 띠 아래로 이어지는 본문 영역으로 잡은
    # 곳 안의 줄은 본문이다 — 상단 여백이 좁은 자료에서 첫 문단의 윗줄들이 띠 안에
    # 들어오는데, 한국어 본문 한 줄은 40~50자라 '60자 넘으면 본문' 보호에 걸리지
    # 않아 통째로 사라졌다(재현: 응용수학 p487 위 6%를 자르면 첫 문단 3줄 소실).
    # 러닝 헤더는 레이아웃이 '버림'으로 따로 잡는다(스캔본 표본 150쪽 전부).
    def in_body_region(ln: dict) -> bool:
        cx, cy = (ln["x0"] + ln["x1"]) / 2, (ln["y0"] + ln["y1"]) / 2
        return any(r["x0"] <= cx <= r["x1"] and r["y0"] <= cy <= r["y1"]
                   for r in layout_texts)

    lines = [ln for ln in lines
             if not in_figure(ln)
             and (ln["y1"] > header_band
                  or len(ln.get("text", "")) > HEADER_MAX_CHARS
                  or in_body_region(ln))]
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
    # 되살린 글상자를 본문 영역에 합류시킨다(위치가 있으므로 읽기 순서는 자동).
    # 상자 안의 독립 수식은 위 in_figure 단계에서 이미 살아남았다 — 상자를
    # 그림 목록에서 뺐기 때문이다(예제의 결과식이 함께 돌아온다).
    text_regions += textbox_recovered
    # 색 밴드(파란 소제목·예제 표지·강조 박스)의 흰 글씨는 전면 이진화로 사라져
    # 텍스트가 비면 아래 pool에서 탈락한다 — 해당 색 영역만 개별 크롭으로 재인식해
    # 소제목·표지 구조를 복원한다(스캔 경로 전용; 내장 경로는 텍스트 레이어가 있음).
    # 아래 두 복원은 스캔 경로 전용이다 — 내장(embedded) 책은 텍스트 레이어에서
    # 소제목·장 제목이 이미 온전히 나오므로 손대지 않는다(v5 출력과 동일 유지).
    if not embedded:
        base_img = hires_image if hires_image is not None else page_image
        for idx, r in enumerate(text_regions):
            cur = len(_WORDISH.findall(r["text"]))
            # 비어 있을 때만이 아니라 '망가진 티가 날 때'도 다시 읽는다. 색 상자
            # 안의 강조 글자는 전면 이진화에서 통째로 사라지는 대신 엉뚱한 낱말로
            # 뭉개지기도 한다 — 실측(전기회로이론 p314): 원본 '회로의 계단응답은
            # 전원이 …'가 '[ize 2810] …'로 나왔고, 상자 둘째 줄('이 될 수 있다')은
            # 아예 빠졌다. 텍스트가 비지 않았으므로 예전 조건에는 걸리지 않았다.
            # 같은 영역을 국소 이진화로 크롭해 읽으면 문장이 온전히 나온다.
            if cur >= 2 and not _CALLOUT_JUNK.search(r["text"]):
                continue
            if colored_ratio(page_image, r) <= CALLOUT_COLOR_RATIO:
                continue
            box = (r["x0"] * k, r["y0"] * k, r["x1"] * k, r["y1"] * k)
            rec = ocr_region_text(base_img, box, tmp_dir, f"band{page_no}_{idx}", hires_dpi)
            got = len(_WORDISH.findall(rec))
            # 글자가 뚜렷하게 더 많을 때만 바꾼다 — 크롭이 상자의 일부만 읽어
            # 오히려 짧아지는 경우에 본문을 잃지 않기 위해서다(실측: 같은 쪽의
            # '실전문제 7.9' 상자는 크롭이 더 짧아 기존 텍스트가 유지된다).
            if got >= 2 and got > max(cur * CALLOUT_REDO_GAIN, cur + 2):
                r["text"] = rec
        # 장 표지 제목 되살리기: 레이아웃이 상단의 '큰' 제목(사진 위에 박힌 장 제목
        # 등)을 러닝 헤더처럼 drop으로 버리는 경우가 있다. 러닝 헤더는 얇으므로(≈2%H),
        # 상단의 충분히 '큰'(>4.5%H) drop 영역만 개별 크롭으로 인식해, 페이지 어디에도
        # 없는(이미지에만 박힌) 제목이면 되살린다.
        page_txt = _squash(" ".join(r.get("text", "") for r in text_regions))
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
            if _squash(rec) in page_txt:  # 이미 본문/캡션에 있으면 중복 방지
                continue
            text_regions.append({"x0": r["x0"], "y0": r["y0"], "x1": r["x1"],
                                 "y1": r["y1"], "col": 1, "type": "TITLE", "text": rec})
            page_txt += _squash(rec)  # 같은 제목의 중복 복원 방지
        # 하단 각주 되살리기: 하단 drop(각주·워터마크·쪽번호)을 개별 크롭으로 인식해,
        # 각주 마커(* † ‡)·한글(4자+)·연도범위(1803-1853) 중 하나가 있고 아직 페이지에
        # 없으면 본문으로 살린다. 워터마크('Made with…')·쪽번호는 셋 다 없어 걸러진다.
        page_txt = _squash(" ".join(r.get("text", "") for r in text_regions))
        for fi, b in enumerate(foot_boxes):
            rec = ocr_region_text(base_img, tuple(v * k for v in b),
                                  tmp_dir, f"foot{page_no}_{fi}", hires_dpi)
            if not rec or _RUNNING_HDR.match(rec):
                continue
            if not (_FOOTNOTE_RE.match(rec)
                    or len(re.findall(r"[가-힣]", rec)) >= 4
                    or re.search(r"\(\s*1?\d{3}\s*[-~–]\s*1?\d{3}\s*\)", rec)):
                continue
            if _squash(rec) in page_txt:
                continue
            text_regions.append({"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3],
                                 "col": 1, "type": "TEXT", "text": rec})
            page_txt += _squash(rec)  # 같은 각주의 중복 복원 방지
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
            and not r.get("nocap")      # 되살린 글상자는 캡션이 아니다
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
    # 밴드는 읽기 순서(order_flow)와 같은 기준으로 오라벨 영역을 걸러 낸 뒤 구한다.
    col_bands = clean_column_bands(layout_texts + image_regions, page_w)

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
    page_chars = None               # 내장 표 셀의 글꼴 색인 — 표가 있는 쪽에서만 1회 만든다
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
            if page_chars is None:
                page_chars = _page_char_index(textpage)
            unresolved = [0]

            def _cell(box, _f=fig, _u=unresolved, page_chars=page_chars):
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
            grid = pdf_table.detect_grid(crop)   # 격자가 없으면 셀도 없다
            cells = [(cx0, ry0, cx1, ry1) for ry0, ry1 in grid[0]
                     for cx0, cx1 in grid[1]] if grid else []
            scan_cell, unreliable = make_scan_cell_fn(
                crop, tmp_dir, f"tbl{page_no}_{fig_idx}", cells, _TESS_POOL)
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
    out_path = None
    try:
        num_pages = len(pdf)
        # MD와 그림 폴더 둘 다 비어 있는 이름을 고른다 — MD만 지워진 잔존 폴더에
        # 새 그림이 섞여 들어가는 것을 막는다. 파일은 배타 생성('x')으로 연다 —
        # 동시 이중 실행이 같은 출력에 겹쳐 쓰는 것을 막는다(검토단 지적).
        base = output_dir / f"{pdf_path.stem}_OCR.md"
        out_path, n = base, 0
        out = None
        start_page = 1
        # 같은 책이 중단된 채 남아 있으면 이어 쓴다 — 천 쪽짜리 책을 중간에 멈추면
        # 처음부터 다시 돌려야 했다(실측 응용수학 488쪽에서 중지 → 1시간 반 손실).
        rp = (resume_point(base.read_text(encoding="utf-8", errors="replace"),
                           pdf_path.name, num_pages) if base.is_file() else None)
        if rp is not None:
            start_page, cut = rp
            done = base.read_text(encoding="utf-8", errors="replace")[:cut]
            base.write_text(done, encoding="utf-8")
            imgs = base.with_name(f"{base.stem}_images")
            if imgs.is_dir():  # 다시 만들 쪽의 옛 그림은 지운다(그림 수가 줄면 고아가 된다)
                for f in imgs.glob("p*_fig*.png"):
                    m = re.match(r"p(\d+)_fig", f.name)
                    if m and int(m.group(1)) >= start_page:
                        f.unlink()
            out = open(base, "a", encoding="utf-8")
            print(f"  [이어하기] 중단된 산출물을 {start_page}쪽부터 이어 씁니다")
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
        # 정리 오류는 무시한다 — 중단 순간 다음 쪽의 Tesseract가 임시 파일을 쥐고
        # 있으면 정리가 PermissionError를 내고, 그 예외가 Ctrl+C를 덮어써 '이 책
        # 실패'로 처리된 뒤 다음 책으로 넘어가 버린다(실측).
        with tempfile.TemporaryDirectory(dir=tmp_root(), ignore_cleanup_errors=True) as tmp, out:
            tmp_dir = Path(tmp)
            # 장 구분: 등록된 프로파일이 있으면 머리 목차 + 해당 쪽 앞 장 제목을
            # 자동으로 넣는다(없으면 기존처럼 장 헤딩 없이 진행).
            chapters = pdf_chapters.for_book(pdf_path.stem, num_pages)
            if pdf_chapters.for_book(pdf_path.stem) and not chapters:
                print("  [경고] 등록된 장 구분 프로파일이 이 문서의 쪽 수와 맞지"
                      " 않아 적용하지 않습니다 (같은 파일명의 다른 문서인지"
                      " 확인하세요).")
            if rp is None:   # 이어 쓸 때는 머리(제목·안내·목차)가 이미 있다
                out.write(f"# {pdf_path.name}\n\n")
                out.write(ai_preamble(pdf_path.name, images_dir.name,
                                      pdf_path.stat().st_size) + "\n\n")
                toc = pdf_chapters.toc_block(chapters)
                if toc:
                    out.write("\n".join(toc) + "\n")
            if chapters:
                print(f"  [장구분] 프로파일 적용: {len(chapters)}개 장")
            force_scan = pdf_chapters.force_scan(pdf_path.stem)
            if force_scan:
                print("  [본문] 내장 텍스트 레이어를 신뢰하지 않고 스캔 경로로 강제합니다"
                      " (force_scan)")
            else:
                agree = embedded_layer_agreement(pdf, tmp_dir)
                if agree is not None:
                    print(f"  [본문] 내장 텍스트 레이어 신뢰도 {agree * 100:.0f}%"
                          f" (기준 {EMBED_MIN_AGREE * 100:.0f}%)")
                    if agree < EMBED_MIN_AGREE:
                        force_scan = True
                        print("         낮아서 스캔 경로로 전환합니다"
                              " — 출판사 선행 OCR이 망가진 책입니다.")
            # 선행 파이프라인: 다음 쪽의 입력은 메인 스레드가 미리 마련하고
            # (pdfium 제약), 선행 스레드가 그 쪽의 레이아웃·수식 검출을 한 뒤
            # Tesseract까지 띄운다 — 스캔본은 다음 쪽의 인식이 이번 쪽의 수식
            # 인식·마무리와 겹쳐 돈다.
            nxt = nxt_future = None
            for page_no in range(start_page, num_pages + 1):
                if page_no in chapters:
                    out.write(f"# {chapters[page_no]}\n\n")
                # 쪽 제목은 인식이 끝난 뒤에 쓴다 — 머리말에서 읽어낸 인쇄
                # 쪽번호를 함께 넣기 위해서다.
                printed = None
                try:
                    page = pdf[page_no - 1]
                    inp = nxt if nxt is not None else prepare_page(page, force_scan)
                    pre = None
                    if nxt_future is not None:
                        try:
                            pre = nxt_future.result()
                        except Exception:
                            pre = None  # 선계산 실패 → 아래에서 동기 재계산
                    nxt = nxt_future = None
                    if pre is None:
                        # 프리페치를 던지기 전에 메인에서 동기 계산한다 — 같은 모델
                        # 싱글턴을 두 스레드가 동시에 돌리는 경합(첫 페이지·선계산
                        # 실패 페이지에서 발생)을 차단한다(검토단 지적).
                        pre = precompute_page(inp, tmp_dir, page_no)
                    if page_no < num_pages:  # 다음 페이지 몫을 미리 던져 둔다
                        try:
                            nxt = prepare_page(pdf[page_no], force_scan)
                            nxt_future = _PREFETCH_POOL.submit(
                                precompute_page, nxt, tmp_dir, page_no + 1)
                        except Exception:
                            nxt = nxt_future = None
                    page_md, n_formulas, source, printed = process_page(
                        page, inp, images_dir, page_no, tmp_dir, pre=pre)
                    # 조용한 전멸 방어: 잉크는 있는데 본문이 한 글자도 안 나온
                    # 쪽은 눕힌 스캔일 수 있다(레이아웃이 본문을 그림으로 오분류).
                    # OSD로 세워 한 번만 다시 인식한다.
                    if not body_chars(page_md) and has_ink(inp["image"]):
                        fixed, angle = upright_page(inp["image"], tmp_dir, page_no)
                        if angle:
                            print(f"    {page_no}페이지: 눕힌 쪽으로 판단해"
                                  f" {angle}도 세워 다시 인식합니다")
                            page_md, n_formulas, source, printed = process_page(
                                page, {**inp, "image": fixed, "hires": None, "ocr": None},
                                images_dir, page_no, tmp_dir)
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
            # 수식 수는 아래 확정 단계에서 파일 실측값으로 바뀐다(recount_marker).
            out.write(f"\n> [변환 완료] {num_pages}페이지, 검출 수식 {total_formulas}개"
                      + (f", 실패 {failed_pages}페이지" if failed_pages else "")
                      + (f", 본문 못 건진 쪽 {len(blank_pages)}개" if blank_pages else "")
                      + "\n")
    except BaseException:
        # 중단돼도 이음 표식만은 확정해 둔다 — 남으면 그 낱말을 grep이 못 찾는다
        if out_path is not None and out_path.is_file():
            try:
                settle_line_joins(out_path)
            except Exception:
                pass
        raise
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

    # 여기부터는 파일이 다 쓰인 뒤라야 할 수 있는 확정 단계다 — 책 전체가
    # 증거인 줄바꿈 이음, 이웃 쪽과 대조하는 인쇄 쪽번호, 뒤쪽 색인에서 뽑는 용어.
    try:
        glued, spaced = settle_line_joins(out_path)
        if glued or spaced:
            print(f"  [줄바꿈] 낱말 가운데서 끊긴 줄 {glued}곳을 붙이고 {spaced}곳은 띄웠습니다")
    except Exception as e:
        print(f"  [줄바꿈] 이음을 확정하지 못했습니다: {type(e).__name__}")

    try:  # 표식의 수식 수를 파일 실측으로 통일한다(스플라이스와 같은 뜻이 되게)
        out_path.write_text(
            pdf_audit.recount_marker(out_path.read_text(encoding="utf-8")),
            encoding="utf-8")
    except Exception as e:
        print(f"  [표식] 실측 갱신을 못 했습니다: {type(e).__name__}")

    try:  # 쪽마다 독립으로 읽은 인쇄 쪽번호에는 오탐이 섞인다 — 이웃과 대조한다
        n_drop, n_fill, n_fix = settle_page_numbers(out_path)
        if n_drop or n_fill or n_fix:
            print(f"  [쪽번호] 오탐 {n_drop}개 제거 · 앵커로 {n_fill}개 보충"
                  f" · {n_fix}개 바로잡음")
    except Exception as e:
        print(f"  [쪽번호] 검증을 실행하지 못했습니다: {type(e).__name__}")

    # 이 책이 쓰는 용어 목록을 머리말 뒤에 싣는다. 색인은 책 뒤쪽에 있으므로
    # 스트리밍 중에는 만들 수 없다 — 파일이 완성된 뒤 한 번 끼워 넣는다.
    try:
        insert_glossary(out_path)
    except Exception as e:  # 용어 목록 실패가 성공한 변환을 망치면 안 된다
        print(f"  [용어] 목록을 만들지 못했습니다: {type(e).__name__}")

    # 손으로 넣은 장 앵커가 실제 본문과 맞는지 대조한다(프로파일이 있을 때만).
    if chapters:
        try:
            for msg in pdf_chapters.check_anchors(out_path.read_text(encoding="utf-8"),
                                                  chapters):
                print(f"  [장구분] 확인 필요 — {msg}")
        except Exception as e:
            print(f"  [장구분] 앵커를 대조하지 못했습니다: {type(e).__name__}")

    # 즉시 점검한다. 발견이 있으면 리포트 파일을 MD 옆에 남긴다(없으면 안 남김).
    try:
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
        print(f"[알림] 완료 표식이 없는(중단된) 산출물 {len(stale)}개가 출력 폴더에"
              f" 있습니다: {', '.join(stale[:5])}")
        print("       같은 PDF를 다시 변환하면 끊긴 쪽부터 이어 씁니다.\n")

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
