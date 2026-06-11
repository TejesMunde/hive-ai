"""
Embedder — thin wrapper over fastembed ONNX.

Default model: bge-small-en-v1.5 (384-dim, 33MB, MIT, MTEB 62.2).
Model loaded lazily, instance cached per-process.

API:
    embed_one(text) -> np.ndarray[float32, (384,)]
    embed_batch(texts) -> np.ndarray[float32, (N, 384)]

Both return L2-normalized vectors so cosine = dot product.
"""

from __future__ import annotations

import threading
from typing import Iterable

import numpy as np

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384

_LOCK = threading.Lock()
_INSTANCE = None
_INSTANCE_MODEL = None


def _get(model_name: str = DEFAULT_MODEL):
    global _INSTANCE, _INSTANCE_MODEL
    if _INSTANCE is not None and _INSTANCE_MODEL == model_name:
        return _INSTANCE
    with _LOCK:
        if _INSTANCE is not None and _INSTANCE_MODEL == model_name:
            return _INSTANCE
        from fastembed import TextEmbedding
        _INSTANCE = TextEmbedding(model_name=model_name)
        _INSTANCE_MODEL = model_name
        return _INSTANCE


def embed_batch(texts: Iterable[str], model: str = DEFAULT_MODEL) -> np.ndarray:
    enc = _get(model)
    arr = np.array(list(enc.embed(list(texts))), dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
    return arr / norms


def embed_one(text: str, model: str = DEFAULT_MODEL) -> np.ndarray:
    return embed_batch([text], model=model)[0]
