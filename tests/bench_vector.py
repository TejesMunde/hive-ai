"""
Vector search bench — numpy cosine baseline.

Synthetic float32 unit vectors at dim=384. Measures per-engine:
  - build time
  - p50 / p95 query latency
  - memory footprint

Ruvector numbers captured separately via `npx ruvector benchmark`.
"""

import gc
import time
import numpy as np

DIM = 384
QUERIES = 200
K = 10
SCALES = [1_000, 10_000, 50_000]
SEED = 42


def gen_corpus(n: int, dim: int) -> np.ndarray:
    rng = np.random.default_rng(SEED)
    x = rng.standard_normal((n, dim)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True) + 1e-9
    return x


def percentile(xs, p):
    xs = sorted(xs)
    return xs[min(int(len(xs) * p / 100), len(xs) - 1)]


def bench_numpy(corpus: np.ndarray, queries: np.ndarray, k: int):
    t0 = time.perf_counter()
    db = np.ascontiguousarray(corpus)
    build_ms = (time.perf_counter() - t0) * 1000

    lats = []
    for q in queries:
        t0 = time.perf_counter()
        scores = db @ q
        top = np.argpartition(-scores, k)[:k]
        _ = top[np.argsort(-scores[top])]
        lats.append((time.perf_counter() - t0) * 1000)

    return {
        "build_ms": round(build_ms, 2),
        "p50_ms":   round(percentile(lats, 50), 3),
        "p95_ms":   round(percentile(lats, 95), 3),
        "mem_mb":   round(db.nbytes / (1024 * 1024), 2),
    }


def bench_numpy_batched(corpus: np.ndarray, queries: np.ndarray, k: int, batch: int = 32):
    """Realistic for hive: batched query (think MMR / multi-agent recall)."""
    t0 = time.perf_counter()
    db = np.ascontiguousarray(corpus)
    build_ms = (time.perf_counter() - t0) * 1000

    lats = []
    for i in range(0, len(queries), batch):
        qb = queries[i:i + batch]
        t0 = time.perf_counter()
        scores = qb @ db.T
        top = np.argpartition(-scores, k, axis=1)[:, :k]
        lats.append((time.perf_counter() - t0) * 1000 / len(qb))

    return {
        "build_ms": round(build_ms, 2),
        "p50_ms":   round(percentile(lats, 50), 3),
        "p95_ms":   round(percentile(lats, 95), 3),
    }


def main():
    rng = np.random.default_rng(SEED + 1)
    print(f"{'engine':<22}{'N':>8}{'build_ms':>12}{'p50_ms':>10}{'p95_ms':>10}{'mem_MB':>10}")
    print("-" * 72)

    for n in SCALES:
        corpus = gen_corpus(n, DIM)
        q_idx = rng.choice(n, size=min(QUERIES, n), replace=False)
        queries = corpus[q_idx] + rng.standard_normal((len(q_idx), DIM)).astype(np.float32) * 0.05
        queries /= np.linalg.norm(queries, axis=1, keepdims=True) + 1e-9

        s = bench_numpy(corpus, queries, K)
        print(f"{'numpy (single)':<22}{n:>8}{s['build_ms']:>12}{s['p50_ms']:>10}{s['p95_ms']:>10}{s['mem_mb']:>10}")

        b = bench_numpy_batched(corpus, queries, K)
        print(f"{'numpy (batch=32)':<22}{n:>8}{b['build_ms']:>12}{b['p50_ms']:>10}{b['p95_ms']:>10}{'-':>10}")

        gc.collect()

if __name__ == "__main__":
    main()
