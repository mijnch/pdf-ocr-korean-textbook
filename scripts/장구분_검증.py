"""장구분.toml의 장 앵커가 실제 산출물의 어느 쪽에 있는지 전수 검증한다.

장 제목은 손으로 조사해 넣는 데이터라(pdf_chapters 참조) 틀려도 아무도 알려주지 않는다.
실제로 공학수학1 제3장 앵커가 13쪽 밀려 있던 것을 이 검증으로 잡았다(2026-08-01).

    python scripts\\장구분_검증.py

읽기 전용이다 — 아무것도 고치지 않고 보고만 한다.
  OK        앵커 쪽 본문에 제목이 그대로 있다
  ★ 실제 N  앵커 ±3쪽 안의 다른 쪽에서 찾았다. N = 앵커-1 이면 장 표지 쪽이라 정상이다
  미검출    창 안에서 못 찾았다. OCR 손상이거나 앵커가 틀렸다 — 눈으로 확인하라
  제목 결손 title 에 장 번호만 있고 제목이 없다
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PROFILE = Path(__file__).resolve().parent.parent / "장구분.toml"
DOCS = Path.home() / "Documents"

def book_dirs() -> dict[str, str]:
    """PDF stem -> 산출물이 있는 과목 폴더. 산출물에서 찾아낸다.

    코드에 박아 두면 책을 늘릴 때마다 고쳐야 하고, 어떤 교재를 다루는지도
    드러난다. Documents 아래의 '*_OCR.md'를 훑어 스스로 알아내게 한다.
    """
    return {md.stem[:-4]: d.name
            for d in sorted(p for p in DOCS.iterdir() if p.is_dir())
            for md in sorted(d.glob("*_OCR.md"))}

PAGE_RE = re.compile(r"^##\s+(\d+)페이지")
WINDOW = 3  # 앵커 앞뒤 몇 쪽까지 찾아볼지


def split_pages(path: Path) -> dict[int, str]:
    """'## N페이지' 절 단위로 본문을 쪼갠다."""
    pages: dict[int, str] = {}
    cur: int | None = None
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PAGE_RE.match(line)
        if m:
            if cur is not None:
                pages[cur] = "\n".join(buf)
            cur, buf = int(m.group(1)), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        pages[cur] = "\n".join(buf)
    return pages


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not PROFILE.exists():
        print(f"장구분.toml 없음: {PROFILE}")
        return 1

    data = tomllib.loads(PROFILE.read_text(encoding="utf-8"))
    bad = 0
    for book in data.get("book", []):
        name = book.get("name", "")
        folder = book_dirs().get(name)
        md = DOCS / folder / f"{name}_OCR.md" if folder else None
        if md is None or not md.exists():
            print(f"\n===== {name} ===== 산출물 없음 — 건너뜀")
            continue

        pages = split_pages(md)
        print(f"\n===== {name}  (쪽 {min(pages)}~{max(pages)}) =====")
        for ch in book.get("chapters", []):
            anchor, title = ch["page"], ch["title"]
            body = title.split(" ", 1)[1] if " " in title else ""
            if not body:
                print(f"  {title:<36} 앵커 {anchor:>5}  ← 제목 결손")
                bad += 1
                continue
            needle = norm(body)
            hits = [p for p in range(anchor - WINDOW, anchor + WINDOW)
                    if p in pages and needle in norm(pages[p])]
            if anchor in hits:
                print(f"  {title:<36} 앵커 {anchor:>5}  OK")
            elif hits:
                print(f"  {title:<36} 앵커 {anchor:>5}  ★ 실제 {hits}")
            else:
                print(f"  {title:<36} 앵커 {anchor:>5}  · 미검출")
                bad += 1

    print(f"\n확인 필요: {bad}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
