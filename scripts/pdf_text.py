"""본문 텍스트 인식 계층 — Tesseract 호출·결과 해석과 본문 정제 원시 함수.

pdf_ocr가 쓰는 인식 I/O를 모아 둔 모듈이다. 페이지 전체 OCR(다단이면 칼럼별),
영역 크롭 OCR(색 밴드·각주 복원), 스캔 표 셀의 두 배율 합의 OCR, 그리고
줄 목록 병합·본문 정제 원시 함수가 여기 있다.

좌표는 모두 넘겨받은 이미지의 픽셀 공간이며, 해상도 환산은 호출부(pdf_ocr) 몫이다.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


# ─── 인식 상수 ───
import tuning

RENDER_DPI = tuning.get("recognition", "render_dpi")        # 기준 렌더 해상도(좌표 공간)
HIRES_MAX_DPI = tuning.get("recognition", "hires_max_dpi")  # 원본 해상도 활용 상한
TESS_LANG = tuning.get("recognition", "tess_lang")
MIN_LINE_CONF = tuning.get("recognition", "min_line_conf")  # 줄 평균 신뢰도 하한
# 보충 줄(다른 PSM/해상도만 읽은 줄)의 정밀도 기준 — 주 줄(35)보다 높다. 실측:
# 진짜 누락 본문 신뢰도 86~94, 밀집 스캔의 오독 파편 36~39로 간격이 커 60에서 분리.
RESCUE_MIN_CONF = tuning.get("recognition", "rescue_min_conf")
RESCUE_MIN_WORDISH = tuning.get("recognition", "rescue_min_wordish")
MASK_MARGIN = tuning.get("recognition", "mask_margin")
PAGE_OCR_TIMEOUT = tuning.get("recognition", "page_ocr_timeout")
# 페이지 분할 모드. 3=자동 레이아웃, 6=단일 블록. 글자 수 많은 쪽 자동 채택.
PSM_CANDIDATES = ("3", "6")


# 그림 주변에서 흔히 나오는 기호 노이즈 줄(글자 없이 | \ / — 등만 있는 줄)
_NOISE_ONLY = re.compile(r"^[\s|\\/_~`^—–\-=·.,'\"()\[\]{}<>]*$")

_WORDISH = re.compile(r"[가-힣A-Za-z]")

# 인라인/블록 수식 스팬($...$, $$...$$). 이 안은 MFR이 권위 있게 만든 LaTeX이므로
# 본문용 노이즈 정리를 적용하면 안 된다 — |(절댓값·조건부확률·브라켓), \(간격·명령)이
# 본문 잔재로 오인돼 파괴된다(검토단 B2 실측: 'P(A | B)' → 'P(A B)').
_MATH_SPAN = re.compile(r"\$\$.+?\$\$|(?<!\$)\$[^$\n]+?\$(?!\$)")
# 단어 사이에 낀 외톨이 세로줄/역슬래시 노이즈("abc | def" → "abc def"). 수식 밖에서만.
_BAR_NOISE = re.compile(r"\s+[|\\]\s+")


def clean_text(text: str) -> str:
    """안전한 수준의 본문 정제. 되돌릴 수 없는 손상이 없는 교정만 수행한다."""
    # 제어문자·BOM·치환문자 등 보이지 않는 잡문자 제거 (줄바꿈/탭은 이미 공백 처리됨)
    text = re.sub("[\x00-\x08\x0b-\x1f\x7f\ufeff\ufffe\uffff\ufffd]", "", text)
    # 글자(한글/영문/숫자) 없이 기호만 있는 조각은 그림 잔재이므로 제거
    if _NOISE_ONLY.match(text) and not re.search(r"[0-9A-Za-z가-힣]", text):
        return ""
    # 세로줄/역슬래시 노이즈 정리는 수식 스팬을 건너뛰고 수식 밖 텍스트에만 적용한다.
    parts, last = [], 0
    for m in _MATH_SPAN.finditer(text):
        parts.append(_BAR_NOISE.sub(" ", text[last:m.start()]))
        parts.append(m.group(0))               # 수식 스팬은 원문 그대로 보존
        last = m.end()
    parts.append(_BAR_NOISE.sub(" ", text[last:]))
    text = "".join(parts)
    # 중복 공백 정리
    return re.sub(r"\s{2,}", " ", text).strip()


# 스캔 앱 워터마크(Goodnotes 한정 — 설명서에 명시) 토큰. 전면 OCR 줄과
# 영역 크롭 OCR 두 경로가 같은 정책을 쓰도록 한 곳에 둔다.
_WATERMARK_RE = re.compile(r"(?i)(made\s+with\s+)?go+dnotes\s*/?")


def start_tesseract(image_path: Path, psm: str, out_base: Path,
                    dpi: int = RENDER_DPI) -> subprocess.Popen:
    """페이지 이미지 OCR을 백그라운드로 시작한다.

    한 번의 인식으로 out_base.txt(내용)와 out_base.tsv(위치·신뢰도)를 함께 만든다.
    줄 내용은 txt에서 가져온다 — Tesseract가 한글 띄어쓰기를 단어 좌표보다
    정확하게 판단하기 때문이다. tsv는 줄의 위치와 신뢰도 판정에만 쓴다.
    """
    return subprocess.Popen(
        [
            "tesseract", str(image_path), str(out_base),
            "-l", TESS_LANG, "--psm", psm, "--dpi", str(dpi), "txt", "tsv",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,  # 실패 원인을 삼키지 않는다(페이지 실패 메시지에 실림)
    )


_OSD_ANGLE = re.compile(r"Rotate:\s*(\d+)")


def detect_rotation(image_path: Path) -> int:
    """Tesseract OSD로 '똑바로 세우려면 몇 도 돌려야 하는가'를 알아낸다.

    눕거나 뒤집힌 스캔은 레이아웃 모델이 본문 덩어리를 그림으로 오분류해
    본문이 통째로 사라진다(검토단 실증: 90°·270°에서 본문 전멸, 감사는
    결함 0건). PDF의 /Rotate 플래그만으로는 판정할 수 없다 — 물리적으로
    눕혀 스캔한 쪽에는 플래그가 아예 없기 때문이다.

    실패하거나 판단이 흐리면 0을 돌려준다(원본 그대로 진행 — 추가적 방어).
    OSD는 비용이 있으므로 호출부가 의심스러운 쪽에만 부른다.
    """
    try:
        r = subprocess.run(["tesseract", str(image_path), "stdout",
                            "-l", "osd", "--psm", "0"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0
    m = _OSD_ANGLE.search(r.stdout or "")
    if not m:
        return 0
    return int(m.group(1)) % 360


def ocr_top_band(page_image, tmp_dir: Path, tag: str, ratio: float = 0.072) -> str:
    """페이지 상단 띠만 한 줄로 OCR한다 — 인쇄 쪽번호 회수용.

    내장 텍스트층에 머리말이 없는 책(응용수학 실측)에서 쓰는 폴백이다.
    띠가 작아 비용이 낮고, 실패하면 빈 문자열을 돌려 준다(추가적 방어).
    """
    strip = page_image.crop((0, 0, page_image.width,
                             max(1, int(page_image.height * ratio))))
    path = tmp_dir / f"hdr_{tag}.png"
    try:
        strip.save(path)
        r = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "7", "-l", TESS_LANG],
            capture_output=True, timeout=60)
        return (r.stdout or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        path.unlink(missing_ok=True)


def load_tesseract_result(out_base: Path) -> list[dict]:
    """txt와 tsv 출력을 줄 단위로 짝지어 [{"text","x0","y0","x1","y1"}]로 반환한다.

    줄 내용은 txt에서 취한다(한글 띄어쓰기가 더 정확). tsv는 상자·신뢰도용이다.
    두 출력의 줄 수가 같으면 순서대로 짝짓고(통상 경로), 어긋나면 공백을 뺀
    글자 내용으로 시퀀스 정렬해 짝을 복원한다 — 어긋난 지점 이후의 모든 줄이
    잘못된 상자와 짝지어지는 것을 막는다. 짝을 못 찾은 tsv 줄은 단어를 이어
    붙인 내용으로 보존한다(상자 없는 txt 줄만 버려진다).
    """
    recs: list[dict] = []  # tsv의 줄 단위 기록: 상자 + 단어들(상자 포함) + 신뢰도들
    for row in (out_base.with_suffix(".tsv")).read_text(encoding="utf-8").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) != 12:
            continue
        if cols[0] == "4":  # 줄
            left, top, width, height = (int(c) for c in cols[6:10])
            recs.append({"x0": left, "y0": top, "x1": left + width, "y1": top + height,
                         "words": [], "boxes": [], "confs": []})
        elif cols[0] == "5" and cols[11].strip() and recs:  # 단어
            recs[-1]["words"].append(cols[11].strip())
            # 단어별 x 범위를 보존한다 — 스캔 경로에서 문장 속 수식을 줄의 '제자리'에
            # 끼워 넣으려면 줄 상자만으로는 부족하고 단어 경계가 필요하다(검토단 C-4).
            wl, _wt, ww, _wh = (int(c) for c in cols[6:10])
            recs[-1]["boxes"].append((wl, wl + ww))
            try:
                recs[-1]["confs"].append(float(cols[10]))
            except ValueError:
                pass

    texts = [
        line.strip()
        for line in (out_base.with_suffix(".txt")).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if len(texts) == len(recs):
        pairs = list(zip(texts, recs))
    else:  # 줄 수 불일치 → 공백 제거 내용 기준 시퀀스 정렬로 짝 복원
        import difflib

        tkeys = [re.sub(r"\s+", "", t) for t in texts]
        rkeys = [re.sub(r"\s+", "", "".join(r["words"])) for r in recs]
        sm = difflib.SequenceMatcher(None, tkeys, rkeys, autojunk=False)
        pairs = []
        matched: set[int] = set()
        for a, b, size in sm.get_matching_blocks():
            for k in range(size):
                pairs.append((texts[a + k], recs[b + k]))
                matched.add(b + k)
        pairs += [(" ".join(r["words"]), r)  # 짝 없는 tsv 줄은 단어 내용으로 보존
                  for j, r in enumerate(recs) if j not in matched and r["words"]]

    lines = []
    for text, rec in pairs:
        conf = sum(rec["confs"]) / len(rec["confs"]) if rec["confs"] else 0.0
        if rec["confs"] and conf < MIN_LINE_CONF:
            continue
        # 스캔 앱 워터마크(Goodnotes)는 drop으로 안 잡힌 페이지에서 떠돌이 줄로
        # 새어 나온다 — 줄 원천에서 토큰을 제거한다(빈 줄이 되면 자연 탈락).
        text = _WATERMARK_RE.sub(" ", text).strip()
        if not text:
            continue
        # 본문의 '$'는 전부 오인식 잡음(수식은 이미 마스킹됨)이지만, 그대로 두면
        # 나중에 삽입되는 수식 구분자 $와 짝을 이뤄 본문이 수식으로 렌더링된다.
        # conf(줄 평균 신뢰도)는 보충 줄 게이트(merge_rescue_lines)가 쓴다.
        # words: [(단어글자수, x0, x1)] — 수식 제자리 삽입이 쓰는 단어 경계 정보.
        wordspans = [(len(w), b[0], b[1])
                     for w, b in zip(rec["words"], rec.get("boxes", []))]
        lines.append({"text": text.replace("$", r"\$"), "x0": rec["x0"], "y0": rec["y0"],
                      "x1": rec["x1"], "y1": rec["y1"], "conf": conf,
                      "words": wordspans})
    return lines


def detect_columns(layout_texts: list[dict], page_w: int) -> list[tuple[int, int]] | None:
    """레이아웃 칼럼 번호로 다단 여부를 판정해 칼럼별 x 범위(밴드)를 반환한다.

    스캔 페이지에서 Tesseract가 칼럼을 가로질러 읽는 것을 막기 위해 쓴다.
    다단으로 인정하는 조건(거짓 양성 방지):
      - 칼럼 번호가 2종 이상이고, 각 칼럼에 본문 영역이 2개 이상,
      - 인접 밴드가 서로 겹치지 않고(가운데 거터가 분명),
      - 각 밴드 폭이 페이지의 70% 미만.
    조건을 못 채우면 None(단일 칼럼으로 처리) → 단일 칼럼 스캔본은 영향 없음.
    """
    cols: dict[int, list[dict]] = {}
    for r in layout_texts:
        c = r.get("col", 1)
        if c >= 1:
            cols.setdefault(c, []).append(r)
    if len(cols) < 2 or any(len(rs) < 2 for rs in cols.values()):
        return None
    bands = sorted((min(r["x0"] for r in rs), max(r["x1"] for r in rs)) for rs in cols.values())
    for a, b in zip(bands, bands[1:]):
        if b[0] <= a[1]:                       # 밴드가 겹침 → 거터 불명확 → 단일 취급
            return None
    if any((x1 - x0) > 0.7 * page_w for x0, x1 in bands):
        return None
    return bands


def _ocr_image(img, tmp_dir: Path, name: str, x_off: int = 0, dpi: int = RENDER_DPI):
    """이미지 하나를 dual-PSM로 OCR한다. 반환: (줄 목록, 성공 여부, 오류 요약)."""
    path = tmp_dir / f"{name}.png"
    img.save(path)
    bases = [tmp_dir / f"{name}_psm{psm}" for psm in PSM_CANDIDATES]
    procs = [start_tesseract(path, psm, b, dpi) for psm, b in zip(PSM_CANDIDATES, bases)]
    candidates: list[list[dict]] = []
    ok = False
    err_txt = ""
    try:
        for proc, base in zip(procs, bases):
            # 정상 페이지 OCR은 1~3초다. 드물게 한 페이지가 병적으로 걸리면
            # 단일 워커 풀이 막혀 뒤 페이지가 줄줄이 대기하므로, 넉넉하되
            # 현실적인 상한(정상의 약 60배)에서 끊어 그 페이지만 실패시킨다.
            _, err = proc.communicate(timeout=PAGE_OCR_TIMEOUT)
            if proc.returncode == 0:
                ok = True
                candidates.append(load_tesseract_result(base))
            elif err:
                err_txt = err.decode("utf-8", "replace").strip()[-200:]
    except Exception:  # 시간 초과 등 — 남은 프로세스를 정리하고 페이지 실패로 넘긴다
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                try:  # kill 후 즉시 회수 — 종료 중인 자식이 파일 핸들을 물고
                    proc.communicate(timeout=5)  # 다음 저장을 막는 경합 방지
                except Exception:
                    pass
        raise
    best = max(candidates, key=lambda ls: sum(len(l["text"]) for l in ls)) if candidates else []
    # 승자독식이 '패자만 읽은 줄'을 버리는 무음 소실 차단(실측 p567: PSM6이 총
    # 글자 11자 차로 이기면서 PSM3만 읽은 불릿 2줄이 통째로 사라짐) —
    # 패자 전용 줄(승자와 위치가 안 겹치는 것)만 승자 결과에 보충한다.
    for cand in candidates:
        if cand is not best:
            merge_rescue_lines(best, cand)
    if x_off:
        for l in best:
            l["x0"] += x_off
            l["x1"] += x_off
            l["words"] = [(n, a + x_off, b + x_off) for n, a, b in l.get("words", ())]
    return best, ok, err_txt


def _prep_region_crop(image, box):
    """영역 크롭을 OCR 하기 좋게 다듬는다. 반환: (크롭, x0, y0, 확대배율) 또는 None.

    색 밴드(파란 소제목·예제 표지 등)의 흰 글씨는 전면 이진화로 사라지기 쉽다.
    영역만 잘라 국소 이진화하고, 배경이 어두우면(중앙 밝기<140) 반전해
    '어두운 글씨·밝은 배경'으로 만든다.
    """
    from PIL import ImageOps, ImageStat

    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1 = min(image.width, int(box[2]))
    y1 = min(image.height, int(box[3]))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    if ImageStat.Stat(crop).median[0] < 140:
        crop = ImageOps.invert(crop)
    # 소제목 밴드의 작은 번호(8.14 등)는 원배율에서 자주 깨진다 — 2배 확대하면
    # Tesseract 인식이 크게 좋아진다(실측: 'EXT'→'8.14'). 과대 이미지는 캡한다.
    scale = 1
    if crop.width < 1600 and crop.height < 400:
        crop = crop.resize((crop.width * 2, crop.height * 2))
        scale = 2
    return crop, x0, y0, scale


def ocr_region_lines(image, box, tmp_dir: Path, name: str,
                     dpi: int = RENDER_DPI) -> list[dict]:
    """영역 하나를 개별 크롭으로 OCR해 **줄 목록**으로 반환한다(좌표는 원본 공간).

    ocr_region_text가 한 줄로 뭉쳐 돌려주는 것과 달리, 줄 구조를 살린다 —
    색 배경 예제 상자를 본문으로 되살릴 때 도표 라벨('Q3', 'Vout')과 문장을
    가르려면 줄 단위 길이가 필요하다.
    """
    prep = _prep_region_crop(image, box)
    if prep is None:
        return []
    crop, x0, y0, scale = prep
    path = tmp_dir / f"{name}.png"
    crop.save(path)
    base = tmp_dir / f"{name}_l"
    proc = start_tesseract(path, "6", base, dpi)
    try:
        proc.communicate(timeout=PAGE_OCR_TIMEOUT)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return []
    if proc.returncode != 0:
        return []
    try:
        lines = load_tesseract_result(base)
    except OSError:
        return []
    for ln in lines:
        ln["x0"] = ln["x0"] / scale + x0
        ln["x1"] = ln["x1"] / scale + x0
        ln["y0"] = ln["y0"] / scale + y0
        ln["y1"] = ln["y1"] / scale + y0
        ln.pop("words", None)          # 좌표계가 달라진 단어 경계는 버린다
    return lines


def ocr_region_text(image, box, tmp_dir: Path, name: str, dpi: int = RENDER_DPI) -> str:
    """영역 하나를 개별 크롭으로 OCR해 한 줄 텍스트로 반환한다."""
    prep = _prep_region_crop(image, box)
    if prep is None:
        return ""
    crop = prep[0]
    path = tmp_dir / f"{name}.png"
    crop.save(path)
    base = tmp_dir / f"{name}_o"
    proc = start_tesseract(path, "6", base, dpi)
    try:
        proc.communicate(timeout=120)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return ""
    if proc.returncode != 0:
        return ""
    try:
        txt = base.with_suffix(".txt").read_text(encoding="utf-8")
    except OSError:  # 외부 임시폴더 청소 등 — 복원만 생략하고 페이지는 계속
        return ""
    txt = " ".join(txt.split())
    # 스캔 앱 워터마크(Goodnotes)는 파란 로고가 색 영역으로 잡혀 여기로 들어온다 —
    # 토큰만 제거한다(실제 캡션에 붙어 나온 경우 캡션 본문은 보존).
    txt = _WATERMARK_RE.sub(" ", txt)
    # 본문 정책과 동일하게 '$'는 이스케이프 — 수식 구분자와 짝을 이루면 안 된다.
    return clean_text(" ".join(txt.split())).replace("$", r"\$")


# 스캔 표 셀 합의 게이트 파라미터
SCAN_CELL_MIN_CONF = tuning.get("table", "scan_cell_min_conf")  # 두 배율 모두 이상이어야 채택
SCAN_CELL_SCALES = (2, 3)  # 셀 크롭을 이 두 배율로 각각 인식해 결과가 같아야 채택


def _ocr_cell_scaled(image, box, scale: int, tmp_dir: Path, name: str):
    """셀 크롭 하나를 주어진 배율로 OCR한다. 반환: (공백 제거 텍스트, 평균 신뢰도).

    스캔 표 셀 전용 — 작은 셀은 국소 이진화·반전 후 확대해 단일 줄(PSM7)로 읽는다.
    """
    from PIL import ImageOps, ImageStat

    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(image.width, int(box[2])), min(image.height, int(box[3]))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return "", 0.0
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    if ImageStat.Stat(crop).median[0] < 140:
        crop = ImageOps.invert(crop)
    crop = crop.resize((crop.width * scale, crop.height * scale))
    path = tmp_dir / f"{name}.png"
    crop.save(path)
    base = tmp_dir / f"{name}_c"
    proc = start_tesseract(path, "7", base, RENDER_DPI * scale)
    try:
        proc.communicate(timeout=60)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return "", 0.0
    if proc.returncode != 0:
        return "", 0.0
    confs = []
    try:
        for row in base.with_suffix(".tsv").read_text(encoding="utf-8").splitlines()[1:]:
            c = row.split("\t")
            if len(c) == 12 and c[0] == "5" and c[11].strip():
                try:
                    confs.append(float(c[10]))
                except ValueError:
                    pass
        txt = base.with_suffix(".txt").read_text(encoding="utf-8")
    except OSError:
        return "", 0.0
    mean = sum(confs) / len(confs) if confs else 0.0
    return "".join(txt.split()), mean


def make_scan_cell_fn(crop, tmp_dir: Path, name: str):
    """스캔 표 셀 함수 + 불안정 카운터를 만든다.

    셀을 두 배율로 OCR해 정규화 결과가 완전히 같고 두 신뢰도가 모두 높을 때만
    그 텍스트를 돌려주고, 아니면 빈 문자열을 돌려주며 불안정 카운터를 올린다.
    호출부는 카운터가 0일 때만 표를 채택한다 — 위첨자·범위값 등 불안정하게 읽히는
    셀이 하나라도 있으면 표 전체를 버려 '틀린 수치를 표로 내보내는' 위험을 막는다.
    (실측: 정수·소수 셀은 두 배율 일치, '3×10⁶'·'2.3-4.0'은 불일치로 거부됨)
    """
    counter = [0]
    seq = [0]

    def fn(box):
        seq[0] += 1
        s1, s2 = SCAN_CELL_SCALES
        t1, c1 = _ocr_cell_scaled(crop, box, s1, tmp_dir, f"{name}_{seq[0]}a")
        t2, c2 = _ocr_cell_scaled(crop, box, s2, tmp_dir, f"{name}_{seq[0]}b")
        if not t1 and not t2:
            return ""                       # 양쪽 다 빈 셀 — 진짜 빈칸(불안정 아님)
        if t1 == t2 and min(c1, c2) >= SCAN_CELL_MIN_CONF:
            return t1.replace("|", r"\|").replace("$", r"\$")
        counter[0] += 1                     # 불일치/저신뢰 → 표 폐기 신호
        return ""

    return fn, counter


def tesseract_lines(page_image, mask_boxes, tmp_dir: Path,
                    bands: list[tuple[int, int]] | None = None,
                    dpi: int = RENDER_DPI, tag: str = "") -> list[dict]:
    """그림·수식 영역을 가린 페이지를 Tesseract로 인식해 본문 줄 목록을 반환한다.

    다단(bands)이 주어지면 칼럼별로 잘라 따로 인식한다 — 스캔 페이지에서 Tesseract가
    칼럼을 가로질러 읽어 좌·우단이 한 줄에 섞이는 것을 방지한다.
    좌표(mask_boxes/bands/반환 줄 상자)는 모두 page_image의 픽셀 공간이다.

    ★ tag는 호출자마다 반드시 달라야 한다 — tmp_dir는 책 한 권 전체가 공유하므로,
    tag가 겹치면 서로 다른 쪽이 같은 임시 PNG(`band{tag}{i}.png`)에 겹쳐 쓴다.
    그러면 A쪽 OCR이 B쪽 이미지를 읽어 **조용히 틀린 본문**이 나온다(예외도 안 난다).
    현재 호출부는 `p{page_no}` / `p{page_no}r`을 넘긴다. 쪽 간 파이프라이닝을
    도입하더라도 이 규칙만 지키면 임시 파일 충돌은 발생하지 않는다.
    """
    from PIL import ImageDraw

    margin = round(MASK_MARGIN * dpi / RENDER_DPI)
    masked = page_image.copy()
    draw = ImageDraw.Draw(masked)
    for x0, y0, x1, y1 in mask_boxes:
        draw.rectangle(
            (x0 - margin, y0 - margin, x1 + margin, y1 + margin),
            fill="white",
        )

    if bands:
        all_lines: list[dict] = []
        failed: list[str] = []
        for i, (bx0, bx1) in enumerate(bands):
            cx0 = max(0, int(bx0) - margin)
            cx1 = min(masked.width, int(bx1) + margin)
            crop = masked.crop((cx0, 0, cx1, masked.height))
            lines, ok, err = _ocr_image(crop, tmp_dir, f"band{tag}{i}", x_off=cx0, dpi=dpi)
            all_lines += lines
            if not ok:
                failed.append(f"{i + 1}단" + (f"({err})" if err else ""))
        if failed:  # 한 단이라도 통째 실패면 반 페이지 무음 소실 — 페이지 실패로 알린다
            raise RuntimeError("Tesseract 칼럼 인식 실패: " + ", ".join(failed))
        return all_lines

    lines, ok, err = _ocr_image(masked, tmp_dir, f"page{tag}", dpi=dpi)
    if not ok:
        raise RuntimeError("Tesseract 본문 인식 실패" + (f": {err}" if err else ""))
    return lines


_SHINGLE_CH = re.compile(r"[0-9a-z가-힣]")
SHINGLE_LEN = 8          # 메아리 판정에 쓰는 글자 토막 길이
RESCUE_VOVERLAP = 0.5    # 세로로 이만큼 겹치면 같은 줄로 본다
RESCUE_ECHO = 0.5        # 이미 읽은 토막을 이만큼 재탕하면 메아리다


def _shingles(text: str) -> set[str]:
    """글자만 남긴 뒤 길이 SHINGLE_LEN 토막 집합으로 만든다(공백·문장부호 무시)."""
    s = "".join(_SHINGLE_CH.findall(text.lower()))
    return {s[i:i + SHINGLE_LEN] for i in range(len(s) - SHINGLE_LEN + 1)}


def merge_rescue_lines(primary: list[dict], rescue: list[dict]) -> int:
    """기준 해상도 OCR(rescue)에서만 잡힌 줄을 주 결과(primary)에 보충한다.

    고해상(400dpi) 입력에서 Tesseract 레이아웃 분석이 짧은 들여쓰기 줄(불릿 항목
    등)을 PSM 불문 통째로 놓치는 사례가 실측됐다(공학수학 p567 — 두 줄 무음 소실).
    기준 해상도에서는 같은 줄이 정상 인식되므로, 주 결과에 없는 줄만 추가한다.
    두 결과는 같은 좌표 공간(기준 해상도)이어야 한다. 반환: 보충한 줄 수.

    보충 줄은 주 결과에 없던 것이라 검증 상대가 없으므로, 주 줄(신뢰도 35 통과)보다
    높은 정밀도 기준을 요구한다 — 신뢰도 RESCUE_MIN_CONF 미만이거나 실질 글자가
    너무 적은(<RESCUE_MIN_WORDISH) 파편은 버린다. 밀집 2단 스캔에서 다른 PSM/해상도가
    이미 읽은 내용을 조각으로 잘못 덧읽는 것('닌 모', 'ATH TE To')을 차단한다.

    '같은 줄'을 중심점 포함으로 보던 예전 판정은 새는 문이었다 — 두 PSM이 줄을
    다르게 자르면 보충 줄의 중심이 주 줄 사이 틈에 떨어져 중복이 통과했고, 같은
    문장이 서로 다르게 깨진 채 문단 한가운데 끼어들었다(반도체 교재 40.3%,
    전자회로 27.3%의 쪽에서 검출). 그래서 두 겹으로 막는다:
      ① 세로 겹침 비율 — 짧은 쪽 높이의 RESCUE_VOVERLAP 이상 겹치면 같은 줄이다.
      ② 내용 메아리 — 이미 읽은 글의 글자 토막을 RESCUE_ECHO 이상 재탕하면,
         위치가 어긋나 있어도 같은 내용의 다른 판독이다. 위치 판정이 못 잡는
         '어긋난 중복'은 이쪽에서 걸린다.
    """
    added = 0
    seen = set()
    for p in primary:
        seen |= _shingles(p.get("text", ""))
    for s in rescue:
        if s.get("conf", 100) < RESCUE_MIN_CONF:
            continue
        if len(_WORDISH.findall(s["text"])) < RESCUE_MIN_WORDISH:
            continue
        h = max(1.0, s["y1"] - s["y0"])
        dup = any(min(p["x1"], s["x1"]) > max(p["x0"], s["x0"])
                  and (min(p["y1"], s["y1"]) - max(p["y0"], s["y0"]))
                  >= RESCUE_VOVERLAP * min(h, max(1.0, p["y1"] - p["y0"]))
                  for p in primary)
        if not dup:
            sh = _shingles(s["text"])
            if sh and len(sh & seen) >= RESCUE_ECHO * len(sh):
                dup = True                    # 위치는 달라도 내용이 메아리다
        if not dup:
            primary.append(s)
            seen |= _shingles(s["text"])      # 보충 줄끼리의 중복도 막는다
            added += 1
    return added


def native_scan_dpi(page) -> int:
    """페이지에 내장된 이미지의 최대 원본 해상도(DPI)를 반환한다. 없거나 실패하면 0.

    스캔 PDF는 페이지가 통짜 이미지라서, 이 값이 기준 해상도(RENDER_DPI)보다
    높으면 원본 해상도로 렌더링해 OCR·수식·그림 품질을 높일 수 있다.
    """
    best = 0
    try:
        for obj in page.get_objects(max_depth=2):
            if obj.type == 3:  # 이미지 객체
                meta = obj.get_metadata()
                best = max(best, int(min(meta.horizontal_dpi, meta.vertical_dpi)))
    except Exception:
        return 0
    return best
