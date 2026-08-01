"""PDF 수식 인식 모듈 — 수식 위치 감지(MFD) + LaTeX 변환(MFR).

pix2text의 수식 감지/인식 모델만 직접 사용한다. pix2text 전체(Pix2Text 클래스)를
쓰지 않는 이유: 내장 텍스트 엔진(중국어 모델)까지 함께 로드되어 메모리와 시작
시간을 낭비하고, 한국어 본문 품질도 Tesseract보다 떨어지기 때문이다.
본문 OCR은 pdf_ocr.py가 Tesseract로 별도 수행한다.

성능:
- 모델은 첫 사용 시 한 번만 로드해 모든 파일/페이지에 재사용
- 수식 LaTeX 변환은 페이지 단위로 묶어 배치 인식
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 모델 다운로드 진행 막대 등 불필요한 출력 억제
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# MFR 가속판(KV캐시+int8 디코더) — 유일한 모델. 원본 ONNX엔 KV캐시가 없어
# 생성이 O(n^2)였고, ONNX initializer를 PyTorch로 이식해 with-past로
# 재수출+양자화한 것. 실측(공학수학1 5페이지 305수식 A/B): 인식 4.16배,
# 정규화 일치 90.8% (차이는 \biggl<->\left 등 렌더 동등 변형).
# 폴백 모델(fp32 원본·int8dec)은 컴팩트화로 삭제됨 — 폴더가 파손되면
# 동봉된 mfr_transplant.py / mfr_export.py로 재제작하거나 재다운로드한다.
_P2T = Path(os.environ.get("APPDATA", "")) / "pix2text" / "1.1"
MFR_KV_DIR = _P2T / "mfr-1.5-onnx-kvint8"

# 수식 감지 입력 크기. 전공책처럼 글자가 작고 빽빽한 페이지를 위해 1024 사용.
import tuning

MFD_RESIZED_SHAPE = tuning.get("formula", "mfd_resized_shape")
# LaTeX 일괄 인식 배치 크기
MFR_BATCH_SIZE = tuning.get("formula", "mfr_batch_size")
# 인식 score가 이보다 낮으면 환각으로 보고 버림 (정상 수식은 실측 0.7 이상)
MIN_MFR_SCORE = tuning.get("formula", "min_mfr_score")
# LaTeX 생성 토큰 상한. 실측 최장 정상 수식(~450토큰)보다 넉넉하게 잡았다.
# 그림을 수식으로 오인한 경우 모델이 한없이 생성하며 시간을 낭비하는 것을 차단한다.
MFR_MAX_TOKENS = tuning.get("formula", "mfr_max_tokens")
# 토큰이 이 개수를 넘는데 고유 토큰 비율이 이보다 낮으면 반복 루프 환각
REPEAT_MIN_TOKENS = 50
REPEAT_UNIQUE_RATIO = 0.15
# 같은 토큰이 10회 이상 연속되면 반복 루프 의심 — 즉시 버리지 않고
# 크롭 여백을 바꿔 1회 재인식한다(자기회귀 반복 루프는 입력 미세 변화에 잘 깨진다).
# array/matrix 열 지정자({c c c ...})는 정당한 문법이므로 검사 전에 제거한다.
_REPEAT_RUN = re.compile(r"(\S{1,8})(?:\s*\1){9,}")
# 4~16토큰 '구절'이 3회 이상 연속 반복 — 짧은 토큰 반복만 보는 _REPEAT_RUN의
# 사각지대(예: 행렬식 전개 항 통째 반복 환각). 정당한 반복(영행렬 행 등)은
# 재시도가 같은 내용을 재현하므로 원본이 유지된다.
_REPEAT_PHRASE = re.compile(r"(?:^|\s)((?:\S+\s+){3,15}\S+)(?:\s+\1){2,}(?:\s|$)")
# 연접 반복 판정은 정본(pdf_latex)의 패턴·구조 표지를 재사용한다 — 같은 개념을
# 두 곳에 복제하면 한쪽만 고쳐 어긋나므로(검토단 S4) 단일 출처로 통일한다.
# 순수 숫자·문자 반복(1010…)은 정당한 수치이므로 재시도 대상에서도 제외한다
# (구조적 환각 — LaTeX 명령/중괄호 묶음의 연발 — 만 재인식으로 회복 시도).
from pdf_latex import (  # noqa: E402
    _MATRIX_ENV,
    _SPACING_CMD,
    _STRUCT_TOKEN,
    _TANDEM_RUN as _TANDEM,
)
_COLSPEC = re.compile(r"\{\s*\|?\s*[crl](?:\s*\|?\s*[crl]){2,}\s*\|?\s*\}")


def _structural_tandem(s: str) -> bool:
    """구조적 환각으로 볼 연접 반복이 있는지 검사(순수 수치·간격 반복은 제외)."""
    for m in _TANDEM.finditer(s):
        core = _SPACING_CMD.sub("", m.group(1))
        if core.strip() and _STRUCT_TOKEN.search(core):
            return True
    return False
# 재시도 시 크롭 사방에 더하는 여백(픽셀)
RETRY_PAD = tuning.get("formula", "retry_pad")
# 재시도 여백 단계. 좁은 여백으로 안 깨지는 반복이 넓히면 회복되는 사례가 있어
# 점증 시도한다(실측: 전자기학 p362는 6px 실패 → 10px 이상에서 정상 회복).
RETRY_PADS = (RETRY_PAD, RETRY_PAD * 2, RETRY_PAD * 3)

Box = tuple[int, int, int, int]  # (x0, y0, x1, y1)

_mfd = None
_mfr = None


def _try_igpu_encoder(mfr, encoder_path: Path) -> None:
    """MFR 인코더 세션만 iGPU(DirectML)로 교체한다. 실패하면 CPU 그대로.

    실측(공학수학1 5페이지): 인코더 배치 1.60배 -> 파이프라인 전체 1.21배.
    디코더(int8 연산자는 DML 미지원)와 MFD(전송 오버헤드로 이득 상쇄)는 CPU 유지.
    onnxruntime-directml이 없거나 optimum 내부 구조가 바뀌면 조용히 CPU로 남는다.
    """
    try:
        import onnxruntime as _ort

        if "DmlExecutionProvider" not in _ort.get_available_providers():
            return
        session = _ort.InferenceSession(
            str(encoder_path),
            providers=["DmlExecutionProvider", "CPUExecutionProvider"],
        )
        # 첫 추론의 초기화 비용을 첫 페이지가 아닌 로드 단계에서 미리 치른다
        # (실측 재확인: 로드 전체가 ~1초 — 예전 주석의 '셰이더 ~7초'는 과장이었다)
        import numpy as _np
        name = session.get_inputs()[0].name
        session.run(None, {name: _np.zeros((1, 3, 384, 384), dtype=_np.float32)})
        mfr.model.encoder.session = session
    except Exception:
        pass


def load_models():
    """수식 감지/인식 모델을 lazy-load한다. 미설치 시 RuntimeError."""
    global _mfd, _mfr
    if _mfd is None:
        try:
            import logging

            logging.disable(logging.INFO)  # pix2text 계열의 장황한 INFO 로그 억제
            from pix2text.formula_detector import MathFormulaDetector
            from pix2text.latex_ocr import LatexOCR
        except ImportError as e:
            # pip 재설치를 안내하면 안 된다 — onnxruntime(CPU판)이 딸려 와
            # iGPU 가속용 onnxruntime-directml의 DLL을 덮어쓴다(실제로 겪음).
            raise RuntimeError(
                "수식 인식 라이브러리(pix2text)를 불러올 수 없습니다.\n"
                "백업에서 복원하세요: Documents\\PDF_Editor_백업\\\n"
                "pip으로 재설치하면 iGPU 가속 런타임이 깨질 수 있습니다."
            ) from e
        # 로컬 모델이 없으면 pix2text가 네트워크 다운로드를 시도한다 —
        # '완전 오프라인' 보증을 지키기 위해 먼저 확인하고 명확히 알린다.
        # 폴더가 아니라 파일 이름까지 확인한다 — 부분 손상이면 폴더는 있는데
        # 라이브러리가 외부로 나가려 든다(검토단이 실제 호출을 관측).
        if not os.environ.get("APPDATA"):
            raise RuntimeError("APPDATA 환경변수가 없어 모델 경로를 찾을 수 없습니다.")
        mfd_onnx = _P2T / "mfd-1.5-onnx" / "pix2text-mfd-1.5.onnx"
        if not mfd_onnx.is_file():
            raise RuntimeError(
                f"수식 감지 모델 파일이 없습니다: {mfd_onnx}\n"
                "백업에서 복원하거나, 인터넷이 되는 환경에서 1회 실행해 받으세요."
            )
        _mfd = MathFormulaDetector()
        if not (MFR_KV_DIR / "decoder_with_past_model.onnx").exists():
            raise RuntimeError(
                f"수식 인식 모델이 없습니다: {MFR_KV_DIR}\n"
                "폴더 안의 mfr_transplant.py / mfr_export.py로 재제작하거나, "
                "breezedeus/pix2text-mfr ONNX를 재다운로드해 재제작하세요."
            )
        # use_cache=True 필수 — pix2text 기본값(False)이면 KV캐시를 안 쓴다
        _mfr = LatexOCR(model_dir=MFR_KV_DIR,
                        more_model_configs={"use_cache": True})
        _try_igpu_encoder(_mfr, MFR_KV_DIR / "encoder_model.onnx")
    return _mfd, _mfr


def find_formulas(page_image) -> list[tuple[str, Box]]:
    """페이지 이미지에서 수식 영역을 찾아 (종류, 사각형) 목록으로 반환한다.

    종류는 'isolated'(독립 수식 줄) 또는 'embedding'(문장 속 수식)이다.
    """
    mfd, _ = load_models()
    results: list[tuple[str, Box]] = []
    for det in mfd.detect(page_image, resized_shape=MFD_RESIZED_SHAPE):
        kind = det.get("type")
        if kind not in ("isolated", "embedding"):
            continue
        points = det["box"]  # 꼭짓점 4개 좌표
        xs = [int(p[0]) for p in points]
        ys = [int(p[1]) for p in points]
        results.append((kind, (min(xs), min(ys), max(xs), max(ys))))
    return results


def _is_degenerate(latex: str, score: float) -> bool:
    """그림 등을 수식으로 잘못 읽어 생긴 환각 출력인지 판정한다.

    자기회귀 모델은 수식이 아닌 입력에서 같은 토큰을 무한 반복하는 경향이 있어,
    낮은 score 또는 비정상적으로 낮은 고유 토큰 비율로 걸러낼 수 있다.
    """
    if not latex or score < MIN_MFR_SCORE:
        return True
    tokens = latex.split()
    return (
        len(tokens) > REPEAT_MIN_TOKENS
        and len(set(tokens)) / len(tokens) < REPEAT_UNIQUE_RATIO
    )


def _encoder_on_igpu(mfr) -> bool:
    try:
        return mfr.model.encoder.session.get_providers()[0] == "DmlExecutionProvider"
    except Exception:
        return False


def _recognize_pipelined(mfr, crops: list, batch_size: int, max_tokens: int):
    """인코더(iGPU)와 디코더(CPU)를 배치 단위로 교차 실행한다.

    iGPU가 다음 배치를 인코딩하는 동안 CPU가 현재 배치를 디코딩한다.
    배치 구성이 순차 경로와 동일하므로 출력도 동일하다
    (실측 305크롭: 1.19배, 305/305 일치). 실패 시 호출부가 순차로 폴백.
    """
    import numpy as np
    import torch
    from concurrent.futures import ThreadPoolExecutor
    from pix2text.utils import prepare_imgs
    from transformers.modeling_outputs import BaseModelOutput

    sess = mfr.model.encoder.session
    in_name = sess.get_inputs()[0].name

    def encode(batch):
        px = mfr.processor(images=batch, return_tensors="pt").pixel_values.numpy()
        return torch.from_numpy(sess.run(None, {in_name: px.astype(np.float32)})[0])

    imgs = prepare_imgs(crops)
    batches = [imgs[i:i + batch_size] for i in range(0, len(imgs), batch_size)]
    results = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(encode, batches[0])
        for i in range(len(batches)):
            hidden = fut.result()
            if i + 1 < len(batches):
                fut = pool.submit(encode, batches[i + 1])
            outs = mfr.model.generate(
                encoder_outputs=BaseModelOutput(last_hidden_state=hidden),
                return_dict_in_generate=True, output_scores=True,
                max_new_tokens=max_tokens)
            probs = mfr._cal_scores(outs)
            texts = mfr.processor.batch_decode(outs.sequences,
                                               skip_special_tokens=True)
            results += [{"text": mfr.post_process(t), "score": p}
                        for t, p in zip(texts, probs)]
    return results


def _retry_box(box: Box, size: tuple[int, int], pad: int = RETRY_PAD) -> Box:
    """재시도용으로 사방 pad만큼 넓힌 크롭 사각형(이미지 경계로 잘라냄)."""
    w, h = size
    return (max(0, box[0] - pad), max(0, box[1] - pad),
            min(w, box[2] + pad), min(h, box[3] + pad))


def recognize_latex(page_image, boxes: list[Box]) -> list[str]:
    """수식 영역들을 잘라내 LaTeX 문자열 목록으로 일괄 인식한다.

    반복 루프 출력(고유 토큰 급감 또는 동일 토큰 10연속)은 크롭 여백을 바꿔
    1회 재인식하고, 재시도가 건강하면 그 결과로 교체한다 — 실수식이 반복
    루프로 오인돼 통째로 사라지던 손실을 복구한다. 저score(<MIN_MFR_SCORE)
    항목은 그림 오인이므로 재시도 없이 버린다.
    환각으로 최종 판정된 항목은 빈 문자열로 반환된다 (호출 쪽에서 버려진다).
    """
    if not boxes:
        return []
    _, mfr = load_models()
    crops = [page_image.crop(box) for box in boxes]
    outputs = None
    if _encoder_on_igpu(mfr):
        try:
            outputs = _recognize_pipelined(mfr, crops, MFR_BATCH_SIZE, MFR_MAX_TOKENS)
        except Exception:
            outputs = None  # 라이브러리 내부 구조 변화 등 — 순차 경로로 폴백
    if outputs is None:
        outputs = mfr.recognize(
            crops,
            batch_size=MFR_BATCH_SIZE,
            rec_config={"max_new_tokens": MFR_MAX_TOKENS},
        )
    if isinstance(outputs, dict):  # 입력이 1개면 dict 하나로 반환됨
        outputs = [outputs]
    parsed = [((out.get("text") or "").strip(), float(out.get("score") or 0.0))
              for out in outputs]

    def _repetitive(latex: str) -> bool:
        core = " ".join(_COLSPEC.sub(" ", latex).split())
        if (_is_degenerate(core, 1.0) or _REPEAT_RUN.search(core)
                or _REPEAT_PHRASE.search(core)):
            return True
        return not _MATRIX_ENV.search(core) and _structural_tandem(core)

    # 반복 루프 출력은 크롭 여백을 바꿔 재인식한다. 여백을 점점 넓혀 가며
    # 시도한다 — 실측(전자기학 p362)에서 여백 6px로는 안 깨지던 반복이
    # 10px 이상에서 정상 식으로 회복됐다. 이미 퇴화로 지목된 소수(실측 0.2%)만
    # 도는 경로라 여러 번 시도해도 전체 비용은 무시할 수준이다.
    pending = [i for i, (latex, score) in enumerate(parsed)
               if latex and score >= MIN_MFR_SCORE and _repetitive(latex)]
    for pad in RETRY_PADS:
        if not pending:
            break
        try:
            again = mfr.recognize(
                [page_image.crop(_retry_box(boxes[i], page_image.size, pad))
                 for i in pending],
                batch_size=MFR_BATCH_SIZE,
                rec_config={"max_new_tokens": MFR_MAX_TOKENS},
            )
            if isinstance(again, dict):
                again = [again]
        except Exception:
            break  # 재시도 실패 시 원래 판정대로 처리
        still: list[int] = []
        for i, out2 in zip(pending, again):
            latex2 = (out2.get("text") or "").strip()
            score2 = float(out2.get("score") or 0.0)
            if latex2 and score2 >= MIN_MFR_SCORE and not _repetitive(latex2):
                parsed[i] = (latex2, score2)
            else:
                still.append(i)
        pending = still
    return ["" if _is_degenerate(latex, score) else latex
            for latex, score in parsed]
