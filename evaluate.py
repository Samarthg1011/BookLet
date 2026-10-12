"""
BooKlet — offline evaluation (leave-one-out, full-catalogue ranking).

goodbooks-10k has no timestamps, so for each sampled reader one random rating
of >= 4 stars is held out. The collaborative model is REBUILT from the
remaining ratings so the held-out book cannot leak into the neighbours, the
reader's other ratings become their profile, and we check whether the
held-out book lands in the top-10 of the whole catalogue.

Metrics: HR@10, NDCG@10, catalogue coverage.

    python evaluate.py        # 500 sampled readers
    python evaluate.py 1500
"""

import math
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.preprocessing import normalize

import engine as eng

ROOT = Path(__file__).parent
K, TOP_K, USER_MOD = 10, 30, 4          # USER_MOD matches prepare_data.py's sample


def build_cf(train):
    train = train.copy()
    train["c"] = train["rating"] - train.groupby("user_id")["rating"].transform("mean")
    uid = {u: i for i, u in enumerate(train["user_id"].unique())}
    X = csr_matrix((train["c"], (train["book_id"] - 1, train["user_id"].map(uid))),
                   shape=(10000, len(uid)))
    Xn = normalize(X)
    out = {}
    for start in range(0, 10000, 500):
        sims = (Xn[start:start + 500] @ Xn.T).toarray()
        for off in range(sims.shape[0]):
            i, row = start + off, sims[off]
            row[i] = -1.0
            idx = np.argpartition(row, -TOP_K)[-TOP_K:]
            idx = idx[np.argsort(row[idx])[::-1]]
            out[i + 1] = [[int(j + 1), float(row[j])] for j in idx if row[j] > 0.04]
    return out


def main():
    n_users = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    kept = [c[c["user_id"] % USER_MOD == 0]
            for c in pd.read_csv(ROOT / "data/raw/ratings.csv", chunksize=1_000_000)]
    r = pd.concat(kept, ignore_index=True)
    sizes = r.groupby("user_id").size()
    eligible = [u for u in sizes.index[sizes >= 12]]
    rng = random.Random(7)
    users = rng.sample(eligible, min(n_users, len(eligible)))
    print(f"{len(eligible):,} eligible readers, evaluating {len(users)}")

    held = {}
    sub = r[r["user_id"].isin(users) & (r["rating"] >= 4)]
    for u, g in sub.groupby("user_id"):
        held[int(u)] = int(g.iloc[rng.randrange(len(g))]["book_id"])
    users = [u for u in users if u in held]
    key = set(held.items())
    mask = pd.Series([(u, b) in key for u, b in zip(r["user_id"], r["book_id"])], index=r.index)
    train = r[~mask]
    profiles = {int(u): g.set_index("book_id")["rating"].to_dict()
                for u, g in train[train["user_id"].isin(users)].groupby("user_id")}

    print("rebuilding the collaborative model from training ratings only ...")
    cf = build_cf(train)

    e = eng.Engine()
    shipped = e.neighbors
    e.neighbors = {b: {"cf": cf.get(b, []), "cb": shipped.get(b, {}).get("cb", [])}
                   for b in e.by_id}

    def run(label, fn):
        hits = ndcg = 0.0
        seen = set()
        for u in users:
            ids = [c["id"] for c in fn(profiles[u])][:K]
            seen.update(ids)
            if held[u] in ids:
                hits += 1
                ndcg += 1.0 / math.log2(ids.index(held[u]) + 2)
        return label, hits / len(users), ndcg / len(users), len(seen) / len(e.by_id)

    rows = [run("Popularity only", lambda p: e.popular(K, exclude=set(p)))]
    for name, w_cf, w_cb in [("Collaborative only", 1.0, 0.0), ("Content only", 0.0, 1.0),
                             ("Hybrid 0.9 / 0.1", 0.9, 0.1), ("Hybrid 0.8 / 0.2", 0.8, 0.2),
                             ("Hybrid 0.7 / 0.3", 0.7, 0.3), ("Hybrid 0.6 / 0.4 (shipped)", 0.6, 0.4),
                             ("Hybrid 0.5 / 0.5", 0.5, 0.5)]:
        eng.W_CF, eng.W_CB = w_cf, w_cb
        rows.append(run(name, lambda p: e.recommend(p, n=K)["results"]))

    print(f"\n| Model | HR@{K} | NDCG@{K} | Coverage |\n|---|---|---|---|")
    for label, hr, nd, cov in rows:
        print(f"| {label} | {hr:.3f} | {nd:.3f} | {cov:.1%} |")


if __name__ == "__main__":
    main()
