"""4-pair pooled trade-outcome ML.

Same idea as ``audusd_ml.py`` but pools trades from all 4 portfolio pairs
(AUD_USD, USD_CAD, EUR_JPY, GBP_USD). Each pair uses its own settled
strategy config (target/stop/max_bars/entry_offset/spread), so labels
reflect what each pair would actually produce as a trade.

Pair identity is one-hot-encoded as a feature so the model knows which
config each trade came from. Everything else is either pip-denominated
(pair-agnostic) or already scale-invariant.

Roughly 4× more training samples than the AUD_USD-only run — the whole
motivation for pooling is that 328 samples was too few to generalize.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from brn_fun.analyze import analyze  # noqa: E402
from brn_fun.backtest import backtest_touches  # noqa: E402
from brn_fun.config import load_config  # noqa: E402
from brn_fun.db import connect, fetch_candles  # noqa: E402
from brn_fun.ml.features import build_dataset  # noqa: E402

SPLIT = "2021-01-01"

# Same portfolio configs used across findings.md. Pool all four.
PORTFOLIO = [
    dict(pair="AUD_USD", pip=0.0001, target_pips=60, stop_pips=30,
         max_bars=1440, entry_offset=14, spread_pips=1.0, grid=0.01),
    dict(pair="USD_CAD", pip=0.0001, target_pips=15, stop_pips=20,
         max_bars=240,  entry_offset=0,  spread_pips=1.0, grid=0.01),
    dict(pair="EUR_JPY", pip=0.01,   target_pips=30, stop_pips=15,
         max_bars=120,  entry_offset=59, spread_pips=1.5, grid=1.0),
    dict(pair="GBP_USD", pip=0.0001, target_pips=30, stop_pips=15,
         max_bars=120,  entry_offset=59, spread_pips=1.2, grid=0.01),
]


def load_pooled():
    """Cached loader — one full analyze+backtest per pair."""
    cache = Path("data/portfolio_ml_cache.pkl")
    if cache.exists():
        with cache.open("rb") as f:
            return pickle.load(f)

    cfg = load_config()
    per_pair = {}
    for row in PORTFOLIO:
        pair = row["pair"]
        print(f"Loading {pair}…", flush=True)
        with connect(cfg.db_path) as conn:
            bars = fetch_candles(conn, pair, "M1", limit=None, order="asc",
                                  complete_only=True)
        print(f"  {len(bars):,} bars", flush=True)

        print(f"Analyzing {pair}…", flush=True)
        events = list(analyze(bars, grid=row["grid"], cooldown_bars=7200,
                              forward_bars=1440, pip=row["pip"]))
        print(f"  {len(events)} events", flush=True)

        print(f"Backtesting {pair}…", flush=True)
        trades = backtest_touches(
            bars, events, pip=row["pip"], filter_name="all", entry="confirm",
            entry_offset=row["entry_offset"],
            target_pips=row["target_pips"], stop_pips=row["stop_pips"],
            max_bars=row["max_bars"], path_ambiguity="worst",
            spread_pips=row["spread_pips"],
            limit_offset_pips=2.0, limit_fill_window=60,
        )
        print(f"  {len(trades)} trades", flush=True)
        per_pair[pair] = (bars, events, trades, row)

    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as f:
        pickle.dump(per_pair, f)
    return per_pair


def train_and_eval() -> None:
    per_pair = load_pooled()

    # Build one big DataFrame with pair one-hot columns added.
    all_features: list[dict] = []
    all_meta: list[dict] = []
    for pair, (bars, events, trades, row) in per_pair.items():
        features, meta = build_dataset(
            bars, events, trades, pip=row["pip"], pre_bars=30, post_bars=10,
        )
        for f in features:
            for p in per_pair:
                f[f"pair_{p}"] = int(pair == p)
        for m in meta:
            m["pair"] = pair
        all_features.extend(features)
        all_meta.extend(meta)
        print(f"  {pair}: {len(features)} feature rows extracted")

    X = pd.DataFrame(all_features)
    meta_df = pd.DataFrame(all_meta)
    y = (meta_df["pnl_pips"] > 0).astype(int).values
    print(f"\nDataset: {len(X)} rows, {X.shape[1]} features")

    # Per-pair sanity check
    print(f"\nPer-pair sample sizes & baseline win rates:")
    for p in per_pair:
        mask = meta_df["pair"] == p
        n = mask.sum()
        wr = y[mask].mean() * 100
        exp = meta_df.loc[mask, "pnl_pips"].mean()
        print(f"  {p:<9}  n={n:>4}  win={wr:>4.1f}%  exp={exp:+.2f}p")

    # Time split (same 2021-01-01 boundary as everywhere else)
    is_h1 = meta_df["entry_time"] < SPLIT
    is_h2 = ~is_h1

    print(f"\nH1 (train): {is_h1.sum()} trades, win rate {y[is_h1].mean()*100:.1f}%, "
          f"expectancy {meta_df.loc[is_h1, 'pnl_pips'].mean():+.2f}p")
    print(f"H2 (test):  {is_h2.sum()} trades, win rate {y[is_h2].mean()*100:.1f}%, "
          f"expectancy {meta_df.loc[is_h2, 'pnl_pips'].mean():+.2f}p")

    X_h1 = X.loc[is_h1]
    X_h2 = X.loc[is_h2]
    y_h1 = y[is_h1]
    y_h2 = y[is_h2]
    pnl_h2 = meta_df.loc[is_h2, "pnl_pips"].values
    pair_h2 = meta_df.loc[is_h2, "pair"].values

    # ---- Model 1: logistic regression ----
    print("\n=== Logistic regression (L2, C=0.5) ===")
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    Xs_h1 = scaler.fit_transform(X_h1)
    Xs_h2 = scaler.transform(X_h2)

    lr = LogisticRegression(C=0.5, max_iter=2000,
                             class_weight="balanced", solver="lbfgs")
    lr.fit(Xs_h1, y_h1)
    p_train = lr.predict_proba(Xs_h1)[:, 1]
    p_h2 = lr.predict_proba(Xs_h2)[:, 1]

    print(f"H1 AUC (in-sample): {roc_auc_score(y_h1, p_train):.3f}")
    print(f"H2 AUC (OOS):       {roc_auc_score(y_h2, p_h2):.3f}")

    threshold_sweep("LR H2", pnl_h2, p_h2, pair_h2,
                     unfiltered_exp=pnl_h2.mean())

    coef = pd.Series(lr.coef_[0], index=X.columns).sort_values(key=abs, ascending=False)
    print("\nTop 20 LR coefficients (positive → predicts winner):")
    for name, val in coef.head(20).items():
        print(f"  {name:<35}  {val:+.3f}")

    # ---- Model 2: LightGBM ----
    print("\n=== LightGBM (small-data settings) ===")
    import lightgbm as lgb
    clf = lgb.LGBMClassifier(
        n_estimators=500, learning_rate=0.03,
        max_depth=5, num_leaves=31,
        min_child_samples=15, reg_alpha=0.1, reg_lambda=0.1,
        class_weight="balanced",
        random_state=42, verbosity=-1,
    )
    clf.fit(X_h1.values, y_h1,
             eval_set=[(X_h2.values, y_h2)],
             callbacks=[lgb.early_stopping(40, verbose=False)])
    p_train = clf.predict_proba(X_h1.values)[:, 1]
    p_h2 = clf.predict_proba(X_h2.values)[:, 1]
    print(f"H1 AUC (in-sample): {roc_auc_score(y_h1, p_train):.3f}")
    print(f"H2 AUC (OOS):       {roc_auc_score(y_h2, p_h2):.3f}")
    print(f"Best iteration:     {clf.best_iteration_}")

    threshold_sweep("LGB H2", pnl_h2, p_h2, pair_h2,
                     unfiltered_exp=pnl_h2.mean())

    importances = pd.Series(clf.feature_importances_, index=X.columns)
    importances = importances.sort_values(ascending=False)
    print("\nTop 20 LightGBM feature importances (split count):")
    for name, val in importances.head(20).items():
        print(f"  {name:<35}  {int(val):>4}")


def threshold_sweep(label: str, pnl: np.ndarray, prob: np.ndarray,
                     pair_ids: np.ndarray, unfiltered_exp: float) -> None:
    """Report OOS P&L at various probability thresholds, plus per-pair breakdown."""
    print(f"\n{label} — threshold sweep (OOS):")
    print(f"  Unfiltered:                    n={len(pnl)}  "
          f"exp={unfiltered_exp:+.2f}p  total={pnl.sum():+.0f}p")
    for th in [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]:
        mask = prob >= th
        n = mask.sum()
        if n < 20:
            print(f"  P(win) >= {th:.2f}: only {n} trades — skipping")
            continue
        kept_pnl = pnl[mask]
        exp = kept_pnl.mean()
        total = kept_pnl.sum()
        d = exp - unfiltered_exp
        # Per-pair breakdown at this threshold
        per_pair_bits = []
        for p in ["AUD_USD", "USD_CAD", "EUR_JPY", "GBP_USD"]:
            pair_mask = (pair_ids == p) & mask
            if pair_mask.sum() > 0:
                per_pair_bits.append(
                    f"{p}:{pair_mask.sum()} ({pnl[pair_mask].mean():+.1f})"
                )
        per_pair_str = "  ".join(per_pair_bits)
        print(f"  P(win) >= {th:.2f}:  kept {n:>4} / {len(pnl)}  "
              f"exp={exp:+.2f}p  total={total:+.0f}p  Δ={d:+.2f}")
        print(f"    ↳  {per_pair_str}")


if __name__ == "__main__":
    train_and_eval()
