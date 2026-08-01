# -*- coding: utf-8 -*-
"""산출물 백업 — 변환 결과(MD + 그림 PNG)를 한 곳에 묶어 둔다.

도구·모델은 이미 'Documents\\PDF_Editor_백업\\'에 zip으로 있으나 산출물에는
백업이 없었다. 산출물은 원본 PDF + 도구로 재생성할 수 있지만 7시간이 걸린다 —
그 시간을 사는 것이 이 백업의 값이다.

사용법:
  python backup_outputs.py              # 기본 목록(교재 5권)을 백업
  python backup_outputs.py <폴더> [...]  # 지정한 폴더의 *_OCR.md 세트를 백업

PNG는 이미 압축된 형식이라 다시 압축해도 줄지 않는다 — 저장(store) 방식으로
묶어 시간을 아낀다. 같은 날 다시 돌리면 이름 뒤에 번호가 붙어 덮어쓰지 않는다.
"""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

DOCS = Path.home() / "Documents"
BACKUP_DIR = DOCS / "PDF_Editor_백업"
DEFAULT_BOOKS = ["대학수학", "전자기학", "응용수학", "대학물리", "전기회로이론"]


def collect(folders: list[Path]) -> list[tuple[Path, str]]:
    """(원본 경로, zip 안 경로) 목록. MD·감사 리포트·그림 폴더를 모은다."""
    items: list[tuple[Path, str]] = []
    for d in folders:
        if not d.is_dir():
            print(f"  [건너뜀] 폴더가 없습니다: {d}")
            continue
        for md in sorted(d.glob("*_OCR.md")):
            items.append((md, f"{d.name}/{md.name}"))
            rep = md.with_name(f"{md.stem}_감사.txt")
            if rep.is_file():
                items.append((rep, f"{d.name}/{rep.name}"))
            imgs = md.with_name(f"{md.stem}_images")
            if imgs.is_dir():
                for p in sorted(imgs.glob("*.png")):
                    items.append((p, f"{d.name}/{imgs.name}/{p.name}"))
    return items


def unique_path(base: Path) -> Path:
    """이미 있으면 뒤에 번호를 붙인다 — 기존 백업을 덮어쓰지 않는다."""
    if not base.exists():
        return base
    n = 2
    while True:
        cand = base.with_name(f"{base.stem}_{n}{base.suffix}")
        if not cand.exists():
            return cand
        n += 1


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    folders = ([Path(a) if Path(a).is_absolute() else DOCS / a for a in args]
               if args else [DOCS / b for b in DEFAULT_BOOKS])
    items = collect(folders)
    if not items:
        print("백업할 산출물을 찾지 못했습니다.")
        raise SystemExit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = unique_path(BACKUP_DIR / f"PDF_Editor_산출물_{time.strftime('%Y-%m-%d')}.zip")
    total = sum(p.stat().st_size for p, _ in items)
    print(f"산출물 {len(items):,}개 ({total / 1048576:,.0f} MB) → {out.name}")

    t0 = time.time()
    # PNG는 이미 압축돼 있어 ZIP_STORED가 시간 대비 이득이 크다.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for i, (src, arc) in enumerate(items, 1):
            z.write(src, arc)
            if i % 500 == 0 or i == len(items):
                print(f"  {i:,}/{len(items):,}", flush=True)
    size = out.stat().st_size / 1048576
    print(f"완료: {out}  ({size:,.0f} MB, {time.time() - t0:.0f}초)")
    print("복원: 이 zip을 Documents에 풀면 폴더 구조 그대로 되돌아갑니다.")


if __name__ == "__main__":
    main()
