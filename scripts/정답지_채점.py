# -*- coding: utf-8 -*-
"""정답지 채점 — 원본 쪽을 사람이 옮겨 적은 정답지와 산출물을 맞대어 잰다.

    python scripts\\정답지_채점.py            # 지금 있는 산출물(MD)을 채점
    python scripts\\정답지_채점.py --rerun    # 같은 쪽을 현재 코드로 다시 인식해 함께 채점

자가 감사는 원본과 대조하지 않는다. 품질을 올렸는지 떨어뜨렸는지는 이 채점으로만
말할 수 있다 — 코드를 바꿨으면 --rerun으로 확인한 뒤 들인다.

정답지는 교재 내용이므로 저장소에 넣지 않는다('PDF Editor\\정답지\\', .gitignore).
  정답지\\책.toml       [[book]] tag = "circ"  pdf = '...\\책.pdf'  (md는 생략하면 PDF 옆 _OCR.md)
  정답지\\circ_314.txt  그 책 PDF 314쪽의 본문 전사(수식·그림 안 글자는 빼고 옮긴다)

지표 두 가지:
  문자 일치율 — 한글·영숫자만 남겨 이은 글자열의 SequenceMatcher 비율(순서·누락·오독)
  낱말 회수율 — 정답지 낱말(한글 2자+, 영문 4자+)이 산출물에 있는 비율(grep 관점)
수식($…$)과 그림 링크는 빼고 센다 — 정답지가 수식을 옮겨 적지 않기 때문이다.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import tempfile
import tomllib
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
GT_DIR = HERE.parent / "정답지"

_PAGE = re.compile(r"(?m)^## (\d+)페이지(?: \(인쇄 \d+쪽\))?$")
_MATH = re.compile(r"\$\$.*?\$\$|\$[^$\n]*\$", re.S)
_IMG = re.compile(r"!\[[^\]]*\]\(<?[^)]*>?\)")
_KEEP = re.compile(r"[0-9A-Za-z가-힣]+")
_WORD = re.compile(r"[A-Za-z]{4,}|[가-힣]{2,}")


def _norm(s: str) -> str:
    return "".join(_KEEP.findall(unicodedata.normalize("NFKC", s).lower()))


def section(md_text: str, page: int) -> str | None:
    """MD에서 '## N페이지' 절 하나의 본문(수식·그림 링크 제외)."""
    heads = list(_PAGE.finditer(md_text))
    for i, m in enumerate(heads):
        if int(m.group(1)) == page:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(md_text)
            return _MATH.sub(" ", _IMG.sub(" ", md_text[m.end():end]))
    return None


def score(gt: str, body: str) -> tuple[float, float, int]:
    """(문자 일치율, 낱말 회수율, 정답지 글자 수)."""
    a, b = _norm(gt), _norm(body)
    char = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    words = _WORD.findall(unicodedata.normalize("NFKC", gt).lower())
    have = set(_WORD.findall(unicodedata.normalize("NFKC", body).lower()))
    recall = sum(w in have for w in words) / len(words) if words else 1.0
    return char, recall, len(a)


def load_books() -> dict[str, dict]:
    cfg = GT_DIR / "책.toml"
    if not cfg.is_file():
        sys.exit(f"정답지 설정이 없습니다: {cfg}")
    books = {}
    for b in tomllib.loads(cfg.read_text(encoding="utf-8")).get("book", []):
        pdf = Path(b["pdf"])
        md = Path(b["md"]) if b.get("md") else pdf.with_name(pdf.stem + "_OCR.md")
        books[b["tag"]] = {"pdf": pdf, "md": md}
    return books


def rerun(books: dict, pages: dict[str, list[int]]) -> dict[tuple[str, int], str]:
    """정답지 쪽을 현재 코드로 다시 인식한다(변환과 같은 판정·렌더링·이음 확정)."""
    import common
    common.setup_external_tools()
    import pypdfium2 as pdfium
    import pdf_chapters
    import pdf_layout
    import pdf_math
    import pdf_ocr

    pdf_layout.load_parser()
    pdf_math.load_models()
    out: dict[tuple[str, int], str] = {}
    with tempfile.TemporaryDirectory(dir=common.tmp_root()) as tmp:
        tmp_dir = Path(tmp)
        for tag, plist in pages.items():
            book = books[tag]
            pdf = pdfium.PdfDocument(str(book["pdf"]))
            try:
                force = pdf_chapters.force_scan(book["pdf"].stem)
                if not force:
                    agree = pdf_ocr.embedded_layer_agreement(pdf, tmp_dir)
                    force = agree is not None and agree < pdf_ocr.EMBED_MIN_AGREE
                texts = {}
                for p in plist:
                    page = pdf[p - 1]
                    md, _n, source, _pr = pdf_ocr.process_page(
                        page, pdf_ocr.prepare_page(page, force), tmp_dir / "img", p, tmp_dir)
                    texts[p] = f"## {p}페이지\n\n" + "\n".join(md) + "\n"
                    print(f"  {tag} {p}쪽 다시 인식 ({source})", flush=True)
            finally:
                pdf.close()
            # 줄바꿈 이음은 책 전체가 증거다 — 기존 산출물이 있으면 증거로 보탠다
            evidence = book["md"].read_text(encoding="utf-8") if book["md"].is_file() else ""
            joined, _g, _s = pdf_ocr.resolve_joins("\n".join(texts.values()), evidence)
            for p in plist:
                out[(tag, p)] = section(joined, p) or ""
    return out


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="정답지 채점")
    ap.add_argument("--rerun", action="store_true", help="현재 코드로 다시 인식해 함께 채점")
    args = ap.parse_args()

    books = load_books()
    pages: dict[str, list[int]] = {}
    for f in sorted(GT_DIR.glob("*_*.txt")):
        tag, _, p = f.stem.rpartition("_")
        if tag in books and p.isdigit():
            pages.setdefault(tag, []).append(int(p))
    if not pages:
        sys.exit(f"정답지 쪽이 없습니다: {GT_DIR}\\<tag>_<쪽>.txt")

    fresh = rerun(books, pages) if args.rerun else {}
    cols = ["산출물"] + (["현재 코드"] if args.rerun else [])
    print(f"\n{'책':10}{'쪽':>6}" + "".join(f"{c + ' 문자':>14}{'낱말':>8}" for c in cols))
    total = [[0.0, 0.0, 0] for _ in cols]
    for tag, plist in pages.items():
        md_text = books[tag]["md"].read_text(encoding="utf-8") if books[tag]["md"].is_file() else ""
        for p in plist:
            gt = (GT_DIR / f"{tag}_{p}.txt").read_text(encoding="utf-8")
            bodies = [section(md_text, p)] + ([fresh.get((tag, p))] if args.rerun else [])
            line = f"{tag:10}{p:>6}"
            for i, body in enumerate(bodies):
                if body is None:
                    line += f"{'-':>14}{'-':>8}"
                    continue
                c, r, n = score(gt, body)
                total[i][0] += c * n
                total[i][1] += r * n
                total[i][2] += n
                line += f"{c * 100:>13.1f}%{r * 100:>7.1f}%"
            print(line)
    line = f"{'가중평균':10}{'':>6}"
    for c, r, n in total:
        line += (f"{c / n * 100:>13.1f}%{r / n * 100:>7.1f}%" if n else f"{'-':>14}{'-':>8}")
    print(line)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
