"""장 구분 프로파일 — 변환 중 '## N페이지' 절 앞에 장 제목을 넣는다.

장 감지법은 책마다 이질적이라(러닝 헤더·표지·목차 앵커·절 목록 등) 자동 감지를
파이프라인에 넣지 않는다. 대신 확인된 결과를 'PDF Editor\\장구분.toml'에 데이터로
두고, 변환할 때 그대로 재현한다 — 재산출할 때마다 손으로 다시 넣던 일이 사라진다.

새 책은 같은 방법론으로 한 번 조사한 뒤 프로파일에 등록하면 이후 자동이다.
프로파일이 없으면 장 헤딩 없이 변환한다(기존 동작).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent.parent / "장구분.toml"


def _parse_profiles(data: dict) -> tuple[dict[str, dict[int, str]], set[str]]:
    """TOML 파싱 결과(dict)에서 (책→{페이지:제목}, force_scan 책 집합)을 뽑는다.

    파일 IO와 분리한 순수 함수 — 픽스처로 직접 검증한다(검토단 지적). 잘못된 항목은
    건너뛰되 나머지는 살린다.
    """
    out: dict[str, dict[int, str]] = {}
    force_scan: set[str] = set()
    for book in data.get("book", []):
        name = book.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        # 내장 텍스트 레이어가 있어도(타 도구가 입힌 저품질 OCR층 등) 신뢰하지 않고
        # 스캔 경로(Tesseract)로 강제하는 책. 정상 한글이지만 오독인 층은 정상 글자율
        # 게이트를 통과하므로(검토단 C-1) 자동 감지가 아니라 명시적 플래그로 다룬다.
        if book.get("force_scan") is True:
            force_scan.add(name.strip())
        pages: dict[int, str] = {}
        for ch in book.get("chapters", []):
            page, title = ch.get("page"), ch.get("title")
            if isinstance(page, int) and page >= 1 and isinstance(title, str) and title.strip():
                pages[page] = title.strip()
            else:
                print(f"[장구분] '{name}'의 잘못된 항목을 건너뜁니다: {ch!r}",
                      file=sys.stderr)
        if pages:
            out[name.strip()] = pages
    return out, force_scan


def _load() -> tuple[dict[str, dict[int, str]], set[str]]:
    if not PROFILE_PATH.exists():
        return {}, set()
    try:
        import tomllib

        with open(PROFILE_PATH, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"[장구분] '{PROFILE_PATH.name}'을 읽지 못해 장 헤딩 없이 진행합니다: {e}",
              file=sys.stderr)
        return {}, set()
    return _parse_profiles(data)


_PROFILES, _FORCE_SCAN = _load()


def for_book(pdf_stem: str, num_pages: int | None = None) -> dict[int, str]:
    """책 이름(확장자 뺀 PDF 파일명)에 해당하는 {페이지: 장 제목}. 없으면 빈 dict.

    프로파일은 파일명으로만 짝지으므로, 같은 이름의 다른 문서를 넣으면 존재하지
    않는 쪽을 가리키는 목차가 통째로 박힌다(검토단 실증: 2쪽짜리 PDF에 29~929쪽
    목차 19개). num_pages를 주면 문서 밖을 가리키는 장은 버리고, 절반 이상이
    문서 밖이면 프로파일 자체를 적용하지 않는다 — 잘못 짝지어진 것으로 본다.
    """
    prof = dict(_PROFILES.get(pdf_stem, {}))
    if not prof or num_pages is None:
        return prof
    inside = {p: t for p, t in prof.items() if 1 <= p <= num_pages}
    if len(inside) * 2 < len(prof):
        return {}
    return inside


def force_scan(pdf_stem: str) -> bool:
    """이 책은 내장 텍스트 레이어를 버리고 스캔 경로로 강제하는가."""
    return pdf_stem in _FORCE_SCAN


def toc_block(chapters: dict[int, str]) -> list[str]:
    """머리 목차 블록(문서 앞에 넣을 줄 목록). 장이 없으면 빈 목록."""
    if not chapters:
        return []
    lines = ["## 장 구분(자동 감지)", ""]
    lines += [f"- {title} — {page}페이지" for page, title in sorted(chapters.items())]
    lines.append("")
    return lines
