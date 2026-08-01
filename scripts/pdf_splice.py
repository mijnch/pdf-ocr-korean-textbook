"""선별 재처리(스플라이스) — 특정 페이지만 다시 인식해 기존 MD의 절을 교체한다.

책 전체 재산출(수 시간) 없이, 원본 PDF의 지정 페이지만 현행 코드로 다시 인식해
기존 '<책>_OCR.md'의 해당 '## N페이지' 절을 정확히 바꿔 넣는다(페이지당 수십 초).
과거 라운드들에서 임시 스크립트로 반복해 온 확립된 전례를 정식 도구로 만든 것.

사용법:
  python pdf_splice.py <원본.pdf> <기존_OCR.md> <페이지> [...]
  <페이지>는 번호(567) 또는 범위(100-105).

동작 보증:
  - 교체 전 MD를 '<이름>.md.bak'으로 백업한다(잘못되면 되돌리기).
  - 장 헤딩(# 제N장)·완료 표식·다른 절은 건드리지 않는다(절 경계 정규식).
  - 해당 페이지의 그림 PNG는 새로 저장되고, 새 절이 참조하지 않게 된 이전
    PNG(그림 수가 줄어든 경우)는 지워 고아 파일을 남기지 않는다.
  - 끝나면 완료 표식의 페이지·수식 수를 실측으로 갱신하고 자가 감사를 돌린다.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from common import exit_with_message, setup_external_tools
from pdf_ocr import (
    HIRES_MAX_DPI,
    RENDER_DPI,
    native_scan_dpi,
    process_page,
)
import pdf_audit


def parse_pages(args: list[str]) -> list[int]:
    pages: set[int] = set()
    for a in args:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", a)
        if not m:
            exit_with_message(f"페이지 지정이 잘못됐습니다: {a} (번호 또는 시작-끝)")
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        if lo < 1 or hi < lo:
            exit_with_message(f"페이지 범위가 잘못됐습니다: {a}")
        pages.update(range(lo, hi + 1))
    return sorted(pages)


def _section_re(pno: int) -> re.Pattern:
    # 절의 끝 = 다음 장 헤딩 / 다음 페이지 절 / 완료 표식 / 파일 끝.
    # 장 헤딩·완료 표식을 경계로 넣지 않으면 교체가 그것들을 삼킨다(과거 실증).
    # 장 헤딩은 '# 제N장'만이 아니라 단일 '#' 헤딩 전체를 경계로 본다 — 새 책의
    # 장 제목이 '제N장' 형식이 아니어도('# Chapter 2', '# 2장') 삼키지 않는다
    # (검토단 B4: 형식 하드코딩이 새 책에서 헤딩을 삼키던 잠복 결함). 페이지 앵커는
    # '## '(두 겹)이라 '^# '(한 겹+공백) 경계에 걸리지 않는다.
    # 쪽 제목에는 인쇄 쪽번호가 뒤따를 수 있다('## 400페이지 (인쇄 392쪽)').
    return re.compile(
        rf"(^## {pno}페이지(?: \(인쇄 [^)]*\))?$\n)"
        r"(.*?)(?=^# |^## \d+페이지(?: \(인쇄 [^)]*\))?$|^> \[변환 완료\]|\Z)",
        re.M | re.S)


def splice(pdf_path: Path, md_path: Path, pages: list[int]) -> None:
    import pypdfium2 as pdfium

    import pdf_layout
    import pdf_math

    text = md_path.read_text(encoding="utf-8")
    missing = [p for p in pages if not _section_re(p).search(text)]
    if missing:
        exit_with_message(
            f"MD에 해당 절이 없습니다: {', '.join(f'{p}페이지' for p in missing)}")

    images_dir = md_path.with_name(f"{md_path.stem}_images")

    print("(레이아웃·수식 인식 모델을 로드하는 중입니다...)")
    pdf_layout.load_parser()
    pdf_math.load_models()

    # 되돌리기 백업 — 타임스탬프로 슬롯을 나눠 매번 새로 남긴다. 단일 '.bak'에
    # 덮어쓰면 무관한 페이지를 고친 2회차가 1회차의 되돌리기를 파괴한다(검토단 실측).
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = md_path.with_suffix(md_path.suffix + f".{stamp}.bak")
    shutil.copy2(md_path, backup)
    print(f"[백업] {backup.name}")

    pdf = pdfium.PdfDocument(str(pdf_path))
    replaced = 0
    try:
        if max(pages) > len(pdf):
            exit_with_message(f"원본 PDF는 {len(pdf)}페이지입니다: {max(pages)}페이지 없음")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for pno in pages:
                page = pdf[pno - 1]
                page_image = page.render(scale=RENDER_DPI / 72).to_pil()
                hires = None
                ndpi = min(native_scan_dpi(page), HIRES_MAX_DPI)
                if ndpi > RENDER_DPI:
                    hires = page.render(scale=ndpi / 72).to_pil()
                before = (set(p.name for p in images_dir.glob(f"p{pno}_fig*.png"))
                          if images_dir.is_dir() else set())
                try:
                    page_md, n_f, source, _printed = process_page(
                        page, page_image, images_dir, pno, tmp_dir,
                        pre=None, hires_image=hires)
                except Exception as e:
                    print(f"  {pno}페이지 실패 — 기존 절 유지: {type(e).__name__}: {e}")
                    continue
                body = "\n" + ("\n".join(page_md) + "\n" if page_md else "")
                text = _section_re(pno).sub(
                    lambda m: m.group(1) + body, text, count=1)
                referenced = set(re.findall(rf"p{pno}_fig\d+\.png", body))
                for stale in sorted(before - referenced):  # 참조가 끊긴 이전 그림 정리
                    (images_dir / stale).unlink(missing_ok=True)
                replaced += 1
                print(f"  {pno}페이지 교체 (본문: {source}, 수식 {n_f}개)")
    finally:
        pdf.close()

    text = pdf_audit.recount_marker(text)
    md_path.write_text(text, encoding="utf-8")
    summary, _, _ = pdf_audit.audit_file(md_path)
    print(f"[완료] {replaced}/{len(pages)}페이지 교체 → {md_path.name}")
    print(f"[감사] {summary}")


def main() -> None:
    if len(sys.argv) < 4:
        exit_with_message(
            "사용법: python pdf_splice.py <원본.pdf> <기존_OCR.md> <페이지> [...]")
    pdf_path, md_path = Path(sys.argv[1]), Path(sys.argv[2])
    if not pdf_path.is_file():
        exit_with_message(f"원본 PDF가 없습니다: {pdf_path}")
    if not md_path.is_file():
        exit_with_message(f"MD 파일이 없습니다: {md_path}")
    try:
        setup_external_tools()
    except RuntimeError as e:
        exit_with_message(str(e))
    splice(pdf_path, md_path, parse_pages(sys.argv[3:]))


if __name__ == "__main__":
    main()
