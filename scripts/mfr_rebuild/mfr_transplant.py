# -*- coding: utf-8 -*-
"""MFR ONNX -> PyTorch 가중치 이식 -> KV캐시 포함 ONNX 재수출 -> int8 양자화.

원본 mfr-1.5 ONNX 디코더는 KV캐시가 없어 생성이 O(n^2)이다. PyTorch 가중치가
비공개라, ONNX initializer(이름이 모듈 경로를 보존함)를 적출해 동일 구조의
transformers 모델에 이식한 뒤 with-past로 재수출한다. 각 단계에서 수치 동등성 검증.
"""
import json, os, shutil, subprocess, sys
from pathlib import Path

import numpy as np
import onnx
from onnx.numpy_helper import to_array
import torch

# 모델은 도구 폴더 안에 있다(common.P2T_MODEL_DIR 와 같은 위치). 예전에는
# %APPDATA%\pix2text 를 봤으나, 도구가 자기 폴더 밖을 건드리지 않도록 옮겼다.
# 여기서는 common 을 import 하지 않고 __file__ 기준으로 직접 계산한다 —
# 이 스크립트는 sys.path 설정 없이 단독 실행되는 일회성 유틸리티다.
# (원본 fp32 mfr-1.5-onnx 는 컴팩트화로 삭제됐으므로 재제작 시 재다운로드가 필요하다.)
SRC = (Path(__file__).resolve().parent.parent
       / "models" / "pix2text" / "1.1" / "mfr-1.5-onnx")
SCRATCH = Path(__file__).resolve().parent
PT_DIR = SCRATCH / "mfr_pt"
KV_DIR = SCRATCH / "mfr_kv_onnx"

print("[1] PyTorch 모델 구성...", flush=True)
from transformers import VisionEncoderDecoderConfig, VisionEncoderDecoderModel

cfg = VisionEncoderDecoderConfig.from_pretrained(SRC)
cfg.decoder.use_cache = True          # KV 캐시 해금
model = VisionEncoderDecoderModel(cfg)
model.eval()
sd = model.state_dict()

print("[2] ONNX 가중치 적출 + 그래프 추적 매핑...", flush=True)
weights, unassigned = {}, []

def _find_key(key_base: str):
    """접두사 후보 -> 접미사 유일 일치 순으로 모델 키를 찾는다."""
    for key in (key_base, "decoder." + key_base, "encoder." + key_base):
        if key in sd:
            return key
    parts = key_base.split(".")
    for k in range(len(parts), 1, -1):
        suffix = "." + ".".join(parts[-k:])
        matches = [mk for mk in sd if mk.endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None  # 모호 — 더 짧은 접미사는 더 모호하므로 중단
    return None

def try_assign(key_base: str, arr: np.ndarray, from_matmul: bool):
    """MatMul 출신은 전치를 우선 시도(정방행렬 오배정 방지)."""
    key = _find_key(key_base)
    if key is None:
        return False
    t = torch.from_numpy(arr.copy())
    cands = [t]
    if t.ndim == 2:
        cands = [t.T.contiguous(), t] if from_matmul else [t, t.T.contiguous()]
    for c in cands:
        if sd[key].shape == c.shape:
            weights[key] = c
            return True
    return False

for f in ("encoder_model.onnx", "decoder_model.onnx"):
    g = onnx.load(str(SRC / f)).graph
    inits = {i.name: to_array(i) for i in g.initializer}
    used = set()
    # 1) MatMul/Gemm 노드에 물린 개명 가중치 — 노드 이름에서 모듈 경로 복원
    for node in g.node:
        if node.op_type not in ("MatMul", "Gemm"):
            continue
        for inp in node.input[1:]:
            if inp in inits:
                key_base = (node.name.strip("/").replace("/", ".")
                            .rsplit("." + node.op_type, 1)[0] + ".weight")
                if try_assign(key_base, inits[inp], True):
                    used.add(inp)
                else:
                    unassigned.append((f, node.name, inp, inits[inp].shape))
    # 2) 이름이 보존된 initializer (bias, LayerNorm, 임베딩, Conv 등)
    for name, arr in inits.items():
        if name in used or name.startswith("onnx::"):
            continue
        if not try_assign(name, arr, False):
            unassigned.append((f, "(direct)", name, arr.shape))

missing = [k for k in sd if k not in weights and "pooler" not in k]  # pooler는 미사용 모듈
print(f"    배정 {len(weights)} / 모델 필요 {len(sd)} | 미배정 ONNX {len(unassigned)} | 미충족 모델키 {len(missing)}")
for row in unassigned[:10]:
    print("    미배정:", row)
for k in missing[:10]:
    print("    미충족:", k, tuple(sd[k].shape))
model.load_state_dict(weights, strict=False)

print("[3] 수치 동등성 검증 (이식 PT vs 원본 ONNX)...", flush=True)
import onnxruntime as ort

# 디코더: 같은 입력에서 로짓 비교
dec_sess = ort.InferenceSession(str(SRC / "decoder_model.onnx"),
                                providers=["CPUExecutionProvider"])
ids = np.array([[1, 5, 9, 23, 7]], dtype=np.int64)
enc_hs = np.random.RandomState(0).randn(1, 578, cfg.encoder.hidden_size).astype(np.float32)
onnx_logits = dec_sess.run(None, {"input_ids": ids, "encoder_hidden_states": enc_hs})[0]
with torch.no_grad():
    pt_logits = model.decoder(input_ids=torch.from_numpy(ids),
                              encoder_hidden_states=torch.from_numpy(enc_hs)).logits.numpy()
d_err = float(np.abs(onnx_logits - pt_logits).max())
d_rel = d_err / float(np.abs(onnx_logits).max())
argmax_ok = bool((onnx_logits.argmax(-1) == pt_logits.argmax(-1)).all())
print(f"    디코더 로짓 최대 오차: {d_err:.2e} (상대 {d_rel:.2e}, argmax 일치={argmax_ok})")
d_err = d_rel if argmax_ok else 1.0  # 그리디 디코딩 동등성이 실질 기준

enc_sess = ort.InferenceSession(str(SRC / "encoder_model.onnx"),
                                providers=["CPUExecutionProvider"])
enc_in = enc_sess.get_inputs()[0]
pp = json.loads((SRC / "preprocessor_config.json").read_text(encoding="utf-8"))
size = pp.get("size", 384)
if isinstance(size, dict):
    h, w = size.get("height", 384), size.get("width", 384)
else:
    h = w = int(size)
shape = [d if isinstance(d, int) else {0: 1, 1: 3, 2: h, 3: w}[i]
         for i, d in enumerate(enc_in.shape)]
px = np.random.RandomState(1).randn(*shape).astype(np.float32)
onnx_enc = enc_sess.run(None, {enc_in.name: px})[0]
with torch.no_grad():
    pt_enc = model.encoder(pixel_values=torch.from_numpy(px)).last_hidden_state.numpy()
e_err = float(np.abs(onnx_enc - pt_enc).max())
print(f"    인코더 은닉 최대 오차: {e_err:.2e}")

if d_err > 1e-3 or e_err > 1e-3 or missing:
    print("!! 동등성/이식 실패 — 중단")
    sys.exit(1)

print("[4] PT 저장 + 보조 파일 복사...", flush=True)
if PT_DIR.exists():
    shutil.rmtree(PT_DIR)
model.save_pretrained(PT_DIR)
for f in ("preprocessor_config.json", "tokenizer.json", "tokenizer_config.json",
          "special_tokens_map.json", "generation_config.json"):
    if (SRC / f).exists():
        shutil.copy2(SRC / f, PT_DIR / f)

print("[5] with-past ONNX 수출...", flush=True)
if KV_DIR.exists():
    shutil.rmtree(KV_DIR)
r = subprocess.run(
    [sys.executable, "-m", "optimum.exporters.onnx",
     "--model", str(PT_DIR), "--task", "image-to-text-with-past", str(KV_DIR)],
    capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout[-1500:] if r.stdout else "")
if r.returncode != 0:
    print("수출 실패:", r.stderr[-2000:])
    sys.exit(1)
print("    산출:", *[p.name for p in KV_DIR.glob('*.onnx')])

print("[6] 디코더 int8 동적 양자화...", flush=True)
from onnxruntime.quantization import quantize_dynamic, QuantType
for dec in KV_DIR.glob("decoder*.onnx"):
    q = dec.with_name(dec.stem + "_int8" + dec.suffix)
    quantize_dynamic(str(dec), str(q), weight_type=QuantType.QInt8)
    print(f"    {dec.name}: {dec.stat().st_size//1048576}MB -> {q.stat().st_size//1048576}MB")

print("\n완료 — 다음 단계: 실제 크롭 A/B 검증")
