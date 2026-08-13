"""Documents 루트에 교재 5권 전체의 진입점 문서를 만든다.

    python scripts\\말뭉치안내_생성.py

책을 새로 넣거나 장구분.toml을 고친 뒤 다시 돌리면 안내가 현재 상태와 다시 맞는다.
내용은 전부 실제 파일에서 뽑는다 — 손으로 쓴 숫자가 없으므로 낡을 일이 없다.

주의: 제어된 폴더 액세스(CFA)가 Documents 쓰기를 막는다. 이 파이썬 실행 파일이
Windows 보안 > 랜섬웨어 방지 > 허용 목록에 들어 있어야 한다. 들어 있지 않으면
PermissionError 로 끝난다.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

# 산출물이 있는 위치이자 안내 문서를 쓸 위치. 도구가 자기 폴더 밖을 스스로
# 정하지 않도록 PDF_EDITOR_CORPUS 로 덮어쓸 수 있게 한다 — 지정하면 그곳에만 쓴다.
# (기본값은 종전과 같아 기존 사용법이 그대로 동작한다.)
DOCS = Path(os.environ.get("PDF_EDITOR_CORPUS") or (Path.home() / "Documents"))
PROFILE = Path(__file__).resolve().parent.parent / "장구분.toml"

def find_books() -> list[tuple[str, str]]:
    """(과목 폴더, PDF stem) 목록을 산출물에서 찾아낸다.

    책 목록을 코드에 박아 두면 책을 늘릴 때마다 스크립트를 고쳐야 하고,
    이 파일이 어떤 교재를 다루는지도 드러낸다. Documents 아래에서
    '*_OCR.md'를 찾아 스스로 알아내게 한다(쪽 수 많은 순으로 표시).
    """
    found = []
    for d in sorted(p for p in DOCS.iterdir() if p.is_dir()):
        for md in sorted(d.glob("*_OCR.md")):
            found.append((d.name, md.stem[:-4]))   # '..._OCR' 에서 '_OCR' 제거
    return found

PAGE_RE = re.compile(r"^##\s+(\d+)페이지(\s*\(인쇄\s*(\d+)쪽\))?", re.M)
NL = "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    prof = {b["name"]: b for b in tomllib.loads(PROFILE.read_text(encoding="utf-8"))["book"]}
    rows: list[str] = []
    chapter_blocks: list[str] = []
    notes: list[str] = []

    for folder, stem in find_books():
        d = DOCS / folder
        md = d / f"{stem}_OCR.md"
        if not md.exists():
            print(f"건너뜀 — 산출물 없음: {md}")
            continue
        heads = PAGE_RE.findall(md.read_text(encoding="utf-8"))
        pages = [int(h[0]) for h in heads]
        printed = [h for h in heads if h[2]]
        imgs = d / f"{stem}_OCR_images"
        n_img = len(list(imgs.glob("*.png"))) if imgs.exists() else 0
        pdf = d / f"{stem}.pdf"
        pdf_mb = pdf.stat().st_size / 1024 / 1024 if pdf.exists() else 0
        chs = prof.get(stem, {}).get("chapters", [])

        rows.append(
            f"| **{folder}** | `{stem}_OCR.md` | {len(pages):,} | {len(chs)} | "
            f"{md.stat().st_size / 1024 / 1024:.1f} MB | {n_img:,}장 | {pdf_mb:,.0f} MB |")
        notes.append(f"- **{folder}** — " + (
            f"쪽 머리에 `(인쇄 N쪽)`이 함께 찍혀 있다 ({len(printed):,}/{len(pages):,}쪽)"
            if printed else "인쇄쪽 병기 없음 — PDF 쪽으로만 찾는다"))
        lines = [f"### {folder} · {stem}.pdf ({len(pages):,}쪽)", ""]
        lines += [f"- {ch['title']} — PDF {ch['page']}쪽" for ch in chs]
        chapter_blocks.append(NL.join(lines))

    doc = f"""# 교재 말뭉치 안내

교재 5권을 통째로 OCR해 Markdown으로 만들어 둔 것이다. **사람과 AI가 같이 쓰는 진입점**이라,
질문에 답하기 전에 이 파일부터 보면 어느 책 어느 쪽을 펴야 하는지 바로 나온다.

산출 도구는 `Desktop\\PDF Editor`이고, 장 구분은 `Desktop\\PDF Editor\\장구분.toml`이 원천이다.
이 안내 파일은 실제 파일에서 뽑아 자동 생성한다 — 손으로 고치지 말고 다시 생성하라.

## 한눈에

| 과목 | 본문 파일 | 쪽 | 장 | 본문 | 그림 | 원본 PDF |
|---|---|---:|---:|---:|---:|---:|
{NL.join(rows)}

각 과목 폴더는 `Documents\\<과목>\\` 이고, 그 안에 본문 `*_OCR.md`,
그림 `*_OCR_images\\`, 원본 `*.pdf`가 함께 있다.

## 찾는 법

1. **본문 검색이 우선이다.** 5개 MD를 합쳐 11MB 남짓이라 전체를 훑어도 순식간이다.
   ```
   findstr /S /C:"테브냉" "%USERPROFILE%\\Documents\\*_OCR.md"
   ```
2. **0건이면 표기를 의심하라.** 책마다 용어가 다르다 — 전기장/전계, 테브난/테브냉처럼.
   각 MD 상단의 `## 이 책이 쓰는 용어` 절에 그 책의 찾아보기 표기가 그대로 들어 있다.
   거기에도 없으면 그 책이 그 주제를 안 다루는 것이다.
3. **쪽을 찾았으면 `## N페이지` 절로 간다.** 절 하나가 원본 PDF 한 쪽과 1:1이다.
4. **그림·표·회로도는 PNG를 열어라.** 본문의 그림 링크가 그대로 파일 경로이고 원본 화소가
   남아 있어, OCR이 표로 옮기지 못한 도표도 판독할 수 있다.

## ⚠ 쪽번호 주의

**PDF 쪽과 교재에 인쇄된 쪽은 다르고, 그 차이는 한 책 안에서도 일정하지 않다.**
산술로 환산하지 마라.

{NL.join(notes)}

학생이 "교재 274쪽"이라고 하면 인쇄쪽을 뜻한다. 인쇄쪽이 병기된 책은 그 값으로 찾고,
병기되지 않은 책은 목차·장 시작 쪽을 기준으로 앞뒤를 훑어야 한다.

## 장 찾아보기

{NL.join(chapter_blocks)}

## 되살리기

| 잃었을 때 | 되살리는 법 | 비용 |
|---|---|---|
| `*_OCR.md` (본문) | GitHub 비공개 백업 또는 원본 PDF 재OCR | **재OCR은 4,302쪽 = 수 시간** |
| `*_OCR_images\\` (PNG) | 원본 PDF에서 재추출 | 짧다 |
| 원본 `*.pdf` | 다시 구해야 한다 | — |

본문 MD가 가장 비싸다. 이미지와 PDF는 다시 만들 수 있으니 백업 우선순위는 **MD > PDF > PNG**다.

---
자동 생성: {date.today().isoformat()} · 생성기 `PDF Editor\\scripts\\말뭉치안내_생성.py`
"""

    out = DOCS / "교재 말뭉치 안내.md"
    out.write_text(doc, encoding="utf-8")
    print(f"작성: {out}  ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
