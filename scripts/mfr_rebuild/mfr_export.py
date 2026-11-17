# -*- coding: utf-8 -*-
"""py3.14 partial-descriptor 문제를 패치한 뒤 with-past ONNX 수출 + int8 양자화."""
import functools, sys
from pathlib import Path

# ── py3.14: functools.partial이 디스크립터가 되어 클래스 속성 partial이 self를
#    바인딩한다. optimum의 NORMALIZED_CONFIG_CLASS가 전부 partial → 전멸.
#    with_args가 __get__ 없는 래퍼를 반환하게 먼저 패치한다 (import 순서 중요).
import optimum.utils.normalized_config as _nc

class _NoBind:
    def __init__(self, p):
        self._p = p
    def __call__(self, *a, **k):
        return self._p(*a, **k)

def _with_args(cls, allow_new: bool = False, **kwargs):
    return _NoBind(functools.partial(cls, allow_new=allow_new, **kwargs))

_nc.NormalizedConfig.with_args = classmethod(_with_args)

# ── torch 2.12: onnx.export 기본이 dynamo 경로로 바뀌어 optimum 1.24의
#    동적축 지정과 충돌 → 레거시(torchscript) 수출기로 고정.
import torch  # noqa: E402

_orig_export = torch.onnx.export

def _legacy_export(*args, **kwargs):
    kwargs.setdefault("dynamo", False)
    return _orig_export(*args, **kwargs)

torch.onnx.export = _legacy_export

from optimum.exporters.onnx import main_export  # noqa: E402  (패치 이후 import)

SCRATCH = Path(__file__).resolve().parent
main_export(
    model_name_or_path=str(SCRATCH / "mfr_pt"),
    output=str(SCRATCH / "mfr_kv_onnx"),
    task="image-to-text-with-past",
)
print("수출 완료:", *[p.name for p in (SCRATCH / "mfr_kv_onnx").glob("*.onnx")])

from onnxruntime.quantization import quantize_dynamic, QuantType  # noqa: E402
for dec in (SCRATCH / "mfr_kv_onnx").glob("decoder*.onnx"):
    if dec.stem.endswith("_int8"):
        continue
    q = dec.with_name(dec.stem + "_int8" + dec.suffix)
    quantize_dynamic(str(dec), str(q), weight_type=QuantType.QInt8)
    print(f"양자화 {dec.name}: {dec.stat().st_size//1048576}MB -> {q.stat().st_size//1048576}MB")
