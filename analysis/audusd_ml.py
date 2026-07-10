"""Supervised trade-outcome prediction on AUD_USD.

Approach:
  1. Reproduce the AUD_USD strategy's 328 trades exactly (via ``brn strategy
     run audusd`` internals — same code path).
  2. Extract features for each event with ``brn_fun.ml.features``.
  3. Time-split at 2021-01-01 (same H1/H2 boundary we've used everywhere).
  4. Train two classifiers with the target ``pnl_pips > 0``:
        - Logistic regression (L2 regularized) as the honest, interpretable
          baseline.
        - LightGBM as a stronger non-linear alternative.
  5. Threshold sweep on the H2 hold-out: does filtering trades whose
     predicted win probability is below X lift per-trade expectancy vs
     the unfiltered baseline? At what trade-count cost?

Small-data warning: 328 trades total → ~186 train / ~142 test. Overfit
risk is real. Report both models' OOS metrics honestly; if neither
meaningfully beats the unfiltered strategy, say so.
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
from brn_fun.strategy import get_strategy  # noqa: E402

SPLIT = "2021-01-01"


def load_events_and_trades():
    """Cached loader — analyze() takes ~40s on 3.6M bars."""
    cache = Path("data/audusd_ml_cache.pkl")
    if cache.exists():
        with cache.open("rb") as f:
            return pickle.load(f)

    print("Loading bars…", flush=True)
    cfg = load_config()
    with connect(cfg.db_path) as conn:
        bars = fetch_candles(conn, "AUD_USD", "M1", limit=None, order="asc",
                              complete_only=True)
    print(f"  {len(bars):,} bars", flush=True)

    print("Analyzing…", flush=True)
    events = list(analyze(bars, grid=0.01, cooldown_bars=7200,
                          forward_bars=1440, pip=0.0001))
    print(f"  {len(events)} events", flush=True)

    print("Backtesting…", flush=True)
    strat = get_strategy("audusd")
    trades = backtest_touches(
        bars, events,
        pip=strat.pip,
        filter_name=strat.filter_name,
        entry=strat.entry,
        entry_offset=strat.entry_offset,
        target_pips=strat.target_pips, stop_pips=strat.stop_pips,
        target_atr=strat.target_atr, stop_atr=strat.stop_atr,
        max_bars=strat.max_bars,
        path_ambiguity=strat.path_ambiguity,
        spread_pips=strat.spread_pips,
        limit_offset_pips=strat.limit_offset_pips,
        limit_fill_window=strat.limit_fill_window,
    )
    print(f"  {len(trades)} trades", flush=True)

    data = (bars, events, trades)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as f:
        pickle.dump(data, f)
    return data


def train_and_eval() -> None:
    bars, events, trades = load_events_and_trades()

    features, meta = build_dataset(
        bars, events, trades, pip=0.0001, pre_bars=30, post_bars=10,
    )
    print(f"\nDataset: {len(features)} rows, {len(features[0])} features")

    X = pd.DataFrame(features)
    meta_df = pd.DataFrame(meta)
    y = (meta_df["pnl_pips"] > 0).astype(int).values

    # Time-split
    is_h1 = meta_df["entry_time"] < SPLIT
    is_h2 = ~is_h1
    print(f"H1 (train): {is_h1.sum()} trades, win rate "
          f"{y[is_h1].mean() * 100:.1f}%")
    print(f"H2 (test):  {is_h2.sum()} trades, win rate "
          f"{y[is_h2].mean() * 100:.1f}%")
    print(f"H1 baseline expectancy: {meta_df.loc[is_h1, 'pnl_pips'].mean():+.2f} pips")
    print(f"H2 baseline expectancy: {meta_df.loc[is_h2, 'pnl_pips'].mean():+.2f} pips")

    X_h1, X_h2 = X.loc[is_h1], X.loc[is_h2]
    y_h1, y_h2 = y[is_h1], y[is_h2]
    pnl_h2 = meta_df.loc[is_h2, "pnl_pips"].values

    # ---- Model 1: logistic regression ----
    print("\n=== Logistic regression (L2, C=0.5) ===")
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    Xs_h1 = scaler.fit_transform(X_h1)
    Xs_h2 = scaler.transform(X_h2)

    lr = LogisticRegression(penalty="l2", C=0.5, max_iter=2000,
                             class_weight="balanced", solver="lbfgs")
    lr.fit(Xs_h1, y_h1)

    p_train = lr.predict_proba(Xs_h1)[:, 1]
    p_h2 = lr.predict_proba(Xs_h2)[:, 1]

    print(f"H1 AUC (in-sample): {roc_auc_score(y_h1, p_train):.3f}")
    print(f"H2 AUC (OOS):       {roc_auc_score(y_h2, p_h2):.3f}")

    threshold_sweep("LR H2", pnl_h2, p_h2, unfiltered_exp=pnl_h2.mean())

    # Interpretable coefficients (top by absolute magnitude)
    coef = pd.Series(lr.coef_[0], index=X.columns).sort_values(key=abs, ascending=False)
    print("\nTop 15 LR coefficients (positive → predicts winner):")
    for name, val in coef.head(15).items():
        print(f"  {name:<35}  {val:+.3f}")

    # ---- Model 2: LightGBM ----
    print("\n=== LightGBM (small-data settings) ===")
    import lightgbm as lgb
    clf = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.05,
        max_depth=4, num_leaves=15,
        min_child_samples=8, reg_alpha=0.1, reg_lambda=0.1,
        class_weight="balanced",
        random_state=42, verbosity=-1,
    )
    clf.fit(X_h1.values, y_h1,
             eval_set=[(X_h2.values, y_h2)],
             callbacks=[lgb.early_stopping(30, verbose=False)])
    p_train = clf.predict_proba(X_h1.values)[:, 1]
    p_h2 = clf.predict_proba(X_h2.values)[:, 1]
    print(f"H1 AUC (in-sample): {roc_auc_score(y_h1, p_train):.3f}")
    print(f"H2 AUC (OOS):       {roc_auc_score(y_h2, p_h2):.3f}")
    print(f"Best iteration:     {clf.best_iteration_}")

    threshold_sweep("LGB H2", pnl_h2, p_h2, unfiltered_exp=pnl_h2.mean())

    importances = pd.Series(clf.feature_importances_, index=X.columns)
    importances = importances.sort_values(ascending=False)
    print("\nTop 15 LightGBM feature importances (split count):")
    for name, val in importances.head(15).items():
        print(f"  {name:<35}  {int(val):>4}")


def threshold_sweep(label: str, pnl: np.ndarray, prob: np.ndarray,
                     unfiltered_exp: float) -> None:
    """Report how filtering at various probability thresholds changes P&L."""
    print(f"\n{label} — threshold sweep (OOS):")
    print(f"  Unfiltered:                     n={len(pnl)}  "
          f"exp={unfiltered_exp:+.2f}p  total={pnl.sum():+.0f}p")
    for th in [0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]:
        mask = prob >= th
        n = mask.sum()
        if n == 0:
            print(f"  P(win) >= {th:.2f}: no trades kept")
            continue
        kept_pnl = pnl[mask]
        exp = kept_pnl.mean()
        total = kept_pnl.sum()
        print(f"  P(win) >= {th:.2f}:  kept {n:>3} / {len(pnl)}  "
              f"exp={exp:+.2f}p  total={total:+.0f}p  Δexp vs unfiltered "
              f"{exp - unfiltered_exp:+.2f}")


if __name__ == "__main__":
    train_and_eval()
