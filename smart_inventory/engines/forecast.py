"""E3 Forecast Service: class-aware 1-day / 2-day demand forecasts.

  FAST      champion/challenger: seasonal EWMA (baseline) vs a global gradient-
            boosted model on scaled lag features. The winner on a rolling
            back-test (default 8 weeks, WAPE) serves today's forecast.
  MEDIUM    Croston-SBA vs TSB intermittent-demand models, same champion rule;
            very sparse SKUs are pooled with their composition group.
  CRITICAL  bounce-adjusted demand (fulfilled + lost + suppressed) x uplift.
  SLOW / SPORADIC / NON_MOVING   no forecast: reorder only on live demand.

Run date semantics: `ws.today` is the business day being planned. History ends
yesterday. horizon 1 = demand on the run date ("tomorrow" for the evening
team), horizon 2 = cumulative demand for the run date and the next day.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..config import Policy
from ..models import SkuClass
from .data import WorkingSet

log = logging.getLogger(__name__)
MODEL_VERSION = "forecast-1.0"
FORECAST_CLASSES = {SkuClass.FAST.value, SkuClass.MEDIUM.value, SkuClass.CRITICAL_HARD_TO_SOURCE.value}


@dataclass
class SeriesBlock:
    keys: list[tuple[str, str]]
    Y: np.ndarray  # n_series x T, dense daily demand ending yesterday
    days: pd.DatetimeIndex


def build_matrix(ws: WorkingSet, keys: list[tuple[str, str]], history_days: int) -> SeriesBlock:
    days = pd.date_range(end=pd.Timestamp(ws.today - timedelta(days=1)), periods=history_days, freq="D")
    Y = np.zeros((len(keys), len(days)))
    if keys:
        dd = ws.daily_demand(history_days)
        if not dd.empty:
            agg = dd.groupby(["sku_id", "warehouse_id", "day"]).qty.sum()
            pos = {k: i for i, k in enumerate(keys)}
            day_pos = {d: j for j, d in enumerate(days)}
            for (sku, wh, day), q in agg.items():
                i = pos.get((sku, wh))
                j = day_pos.get(pd.Timestamp(day))
                if i is not None and j is not None:
                    Y[i, j] = q
    return SeriesBlock(keys, Y, days)


# --------------------------------------------------------------- statistical
def dow_factors(Y: np.ndarray, days: pd.DatetimeIndex, window: int = 56, shrink: float = 4.0) -> np.ndarray:
    """Per-series day-of-week multipliers (n x 7), shrunk toward 1 for sparse series."""
    Yw, dw = Y[:, -window:], days[-window:].dayofweek
    overall = Yw.mean(axis=1, keepdims=True) + 1e-9
    f = np.ones((Y.shape[0], 7))
    for d in range(7):
        mask = dw == d
        if mask.any():
            n = mask.sum()
            raw = Yw[:, mask].mean(axis=1, keepdims=True) / overall
            f[:, d] = ((raw * n + shrink) / (n + shrink)).ravel()
    return f


def seasonal_ewma(Y: np.ndarray, days: pd.DatetimeIndex, alpha: float = 0.15) -> tuple[np.ndarray, np.ndarray]:
    """Returns (one_step_preds n x T where preds[:, t] forecasts Y[:, t], final level n)."""
    f = dow_factors(Y, days)
    dow = days.dayofweek
    n, T = Y.shape
    preds = np.zeros((n, T))
    level = Y[:, : min(14, T)].mean(axis=1)
    for t in range(T):
        preds[:, t] = level * f[:, dow[t]]
        level = alpha * (Y[:, t] / np.maximum(f[:, dow[t]], 0.2)) + (1 - alpha) * level
    return preds, level


def croston_sba(Y: np.ndarray, alpha: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    n, T = Y.shape
    z = np.full(n, np.nan)
    p = np.full(n, np.nan)
    q = np.ones(n)
    preds = np.zeros((n, T))
    for t in range(T):
        rate = np.where(np.isnan(z), 0.0, (1 - alpha / 2) * z / np.maximum(p, 1.0))
        preds[:, t] = rate
        d = Y[:, t]
        hit = d > 0
        first = hit & np.isnan(z)
        z = np.where(first, d, z)
        p = np.where(first, q, p)
        upd = hit & ~first
        z = np.where(upd, z + alpha * (d - z), z)
        p = np.where(upd, p + alpha * (q - p), p)
        q = np.where(hit, 1.0, q + 1.0)
    final = np.where(np.isnan(z), 0.0, (1 - alpha / 2) * z / np.maximum(p, 1.0))
    return preds, final


def tsb(Y: np.ndarray, alpha: float = 0.1, beta: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    n, T = Y.shape
    nz = Y > 0
    first = nz.argmax(axis=1)
    z = np.array([Y[i, first[i]] if nz[i].any() else 0.0 for i in range(n)])
    prob = np.clip(nz[:, : min(28, T)].mean(axis=1), 0.01, 1.0)
    preds = np.zeros((n, T))
    for t in range(T):
        preds[:, t] = prob * z
        d = Y[:, t]
        hit = d > 0
        prob = prob + beta * (hit.astype(float) - prob)
        z = np.where(hit, z + alpha * (d - z), z)
    return preds, prob * z


# ------------------------------------------------------------------- GBM
def _gbm_features(Y: np.ndarray, j: int, days: pd.DatetimeIndex, target_day: pd.Timestamp) -> tuple[np.ndarray, np.ndarray]:
    """Features to predict column j (info up to j-1). Returns (X, scale)."""
    hist = Y[:, max(0, j - 28): j]
    scale = hist.mean(axis=1) + 0.1
    lags = np.stack([Y[:, j - k] if j - k >= 0 else np.zeros(Y.shape[0]) for k in range(1, 8)], axis=1) / scale[:, None]
    r7 = Y[:, max(0, j - 7): j].mean(axis=1) / scale
    r14 = Y[:, max(0, j - 14): j].mean(axis=1) / scale
    nzr = (hist > 0).mean(axis=1)
    dow = np.full(Y.shape[0], target_day.dayofweek)
    dom = np.full(Y.shape[0], target_day.day)
    X = np.column_stack([lags, r7, r14, nzr, dow, dom, np.log1p(scale)])
    return X, scale


def _threads():
    """Cap OpenMP threads: on a shared host sklearn's default spin-waiting threads can make a
    0.2s fit take minutes (measured 400x slowdown under contention)."""
    from threadpoolctl import threadpool_limits

    return threadpool_limits(limits=int(os.environ.get("ML_THREADS", "1")))


class GlobalGBM:
    name = "GBM_GLOBAL"

    def __init__(self) -> None:
        self.model = None

    def fit(self, Y: np.ndarray, days: pd.DatetimeIndex, end: int, train_days: int = 120) -> bool:
        try:
            from sklearn.ensemble import HistGradientBoostingRegressor
        except ImportError:  # pragma: no cover
            return False
        Xs, ys = [], []
        for j in range(max(28, end - train_days), end):
            X, scale = _gbm_features(Y, j, days, days[j])
            Xs.append(X)
            ys.append(Y[:, j] / scale)
        if not Xs:
            return False
        X = np.vstack(Xs)
        y = np.concatenate(ys)
        if len(y) > 400_000:
            idx = np.random.default_rng(0).choice(len(y), 400_000, replace=False)
            X, y = X[idx], y[idx]
        self.model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.06, max_leaf_nodes=31, loss="poisson")
        with _threads():
            self.model.fit(X, y)
        return True

    def predict_col(self, Y: np.ndarray, j: int, days: pd.DatetimeIndex, target_day: pd.Timestamp) -> np.ndarray:
        X, scale = _gbm_features(Y, j, days, target_day)
        with _threads():
            return np.maximum(self.model.predict(X), 0) * scale

    def one_step(self, Y: np.ndarray, days: pd.DatetimeIndex, start: int) -> np.ndarray:
        preds = np.zeros_like(Y)
        for j in range(start, Y.shape[1]):
            preds[:, j] = self.predict_col(Y, j, days, days[j])
        return preds

    def forecast_two(self, Y: np.ndarray, days: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
        T = Y.shape[1]
        d1, d2 = days[-1] + pd.Timedelta(days=1), days[-1] + pd.Timedelta(days=2)
        Yx = np.concatenate([Y, np.zeros((Y.shape[0], 2))], axis=1)
        dx = days.append(pd.DatetimeIndex([d1, d2]))
        f1 = self.predict_col(Yx, T, dx, d1)
        Yx[:, T] = f1
        f2 = self.predict_col(Yx, T + 1, dx, d2)
        return f1, f2


def wape(actual: np.ndarray, pred: np.ndarray) -> tuple[float | None, float | None]:
    denom = np.abs(actual).sum()
    if denom <= 0:
        return None, None
    return float(np.abs(actual - pred).sum() / denom), float((pred - actual).sum() / denom)


# ---------------------------------------------------------------- service
def run_forecasts(
    ws: WorkingSet,
    policy: Policy,
    classes: dict[tuple[str, str], str],
    bounce_profiles: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cfg = policy.section("forecast")
    hist = int(policy.get("classification.history_days", 180))
    bt = int(cfg.get("backtest_weeks", 8)) * 7
    z90 = float(cfg.get("p90_z", 1.2816))
    rows: list[dict[str, Any]] = []
    accuracy: list[dict[str, Any]] = []
    whs = sorted({k[1] for k in classes})

    def emit(keys, rate1, rate2, std, method, cls):
        for i, (sku, wh) in enumerate(keys):
            r1, r2, s = float(max(rate1[i], 0)), float(max(rate2[i], 0)), float(max(std[i], 0))
            for h, p50 in ((1, r1), (2, r1 + r2)):
                rows.append({
                    "sku_id": sku, "warehouse_id": wh, "date": ws.today, "horizon": h,
                    "p50": round(p50, 3), "p90": round(p50 + z90 * s * np.sqrt(h), 3),
                    "daily_rate": round((r1 + r2) / 2, 4), "daily_std": round(s, 4),
                    "method": method, "model_version": MODEL_VERSION, "sku_class": cls,
                })

    for wh in whs:
        # ---------------- FAST: champion / challenger
        keys = sorted(k for k, c in classes.items() if c == SkuClass.FAST.value and k[1] == wh)
        if keys:
            blk = build_matrix(ws, keys, hist)
            Y, days = blk.Y, blk.days
            T = Y.shape[1]
            start = max(28, T - bt)
            std = Y[:, -56:].std(axis=1)
            ew_preds, ew_level = seasonal_ewma(Y, days)
            ew_wape, ew_bias = wape(Y[:, start:], ew_preds[:, start:])
            candidates = {"SEASONAL_EWMA": ew_wape if ew_wape is not None else np.inf}
            gbm = GlobalGBM()
            gbm_ok = len(keys) >= 3 and T >= int(cfg.get("min_history_days_ml", 90)) and gbm.fit(Y, days, start)
            gbm_wape = gbm_bias = None
            if gbm_ok:
                gbm_preds = gbm.one_step(Y, days, start)
                gbm_wape, gbm_bias = wape(Y[:, start:], gbm_preds[:, start:])
                candidates["GBM_GLOBAL"] = gbm_wape if gbm_wape is not None else np.inf
            champion = min(candidates, key=candidates.get)
            for m, (w, b) in {"SEASONAL_EWMA": (ew_wape, ew_bias), "GBM_GLOBAL": (gbm_wape, gbm_bias)}.items():
                if m in candidates:
                    accuracy.append({"date": ws.today, "warehouse_id": wh, "sku_class": SkuClass.FAST.value, "method": m,
                                     "role": "champion" if m == champion else "challenger", "wape": w, "bias": b, "n_skus": len(keys)})
            if champion == "GBM_GLOBAL":
                f1, f2 = gbm.forecast_two(Y, days)
                gbm.model = None  # retrain on the full window for serving
                if gbm.fit(Y, days, T):
                    f1, f2 = gbm.forecast_two(Y, days)
            else:
                fac = dow_factors(Y, days)
                d1 = (days[-1] + pd.Timedelta(days=1)).dayofweek
                d2 = (days[-1] + pd.Timedelta(days=2)).dayofweek
                f1, f2 = ew_level * fac[:, d1], ew_level * fac[:, d2]
            emit(keys, f1, f2, std, champion, SkuClass.FAST.value)

        # ---------------- MEDIUM: Croston-SBA vs TSB
        keys = sorted(k for k, c in classes.items() if c == SkuClass.MEDIUM.value and k[1] == wh)
        if keys:
            blk = build_matrix(ws, keys, hist)
            Y = blk.Y
            T = Y.shape[1]
            start = max(28, T - bt)
            alpha, beta = float(cfg.get("croston_alpha", 0.1)), float(cfg.get("tsb_beta", 0.1))
            sba_p, sba_f = croston_sba(Y, alpha)
            tsb_p, tsb_f = tsb(Y, alpha, beta)
            sw, sb_ = wape(Y[:, start:], sba_p[:, start:])
            tw, tb_ = wape(Y[:, start:], tsb_p[:, start:])
            champion = "CROSTON_SBA" if (sw if sw is not None else np.inf) <= (tw if tw is not None else np.inf) else "TSB"
            for m, w, b in (("CROSTON_SBA", sw, sb_), ("TSB", tw, tb_)):
                accuracy.append({"date": ws.today, "warehouse_id": wh, "sku_class": SkuClass.MEDIUM.value, "method": m,
                                 "role": "champion" if m == champion else "challenger", "wape": w, "bias": b, "n_skus": len(keys)})
            rate = sba_f if champion == "CROSTON_SBA" else tsb_f
            # pooling by composition for very sparse series
            nz = (Y > 0).sum(axis=1)
            comp = ws.skus.set_index("sku_id").composition.to_dict() if not ws.skus.empty else {}
            groups: dict[str, list[int]] = {}
            for i, (sku, _) in enumerate(keys):
                groups.setdefault(comp.get(sku) or sku, []).append(i)
            pooled = rate.copy()
            for idx in groups.values():
                if len(idx) > 1:
                    g_rate = rate[idx].mean()
                    for i in idx:
                        if nz[i] < 5:
                            pooled[i] = 0.5 * rate[i] + 0.5 * g_rate
            std = np.sqrt(np.maximum(Y[:, -56:].var(axis=1), pooled))  # at least Poisson variance
            emit(keys, pooled, pooled, std, champion, SkuClass.MEDIUM.value)

        # ---------------- CRITICAL: bounce-adjusted demand
        keys = sorted(k for k, c in classes.items() if c == SkuClass.CRITICAL_HARD_TO_SOURCE.value and k[1] == wh)
        if keys:
            blk = build_matrix(ws, keys, 90)
            uplift = float(cfg.get("critical_uplift", 1.15))
            supp = np.array([bounce_profiles.get(k, {}).get("suppressed_demand_est", 0.0) for k in keys])
            rate = (blk.Y.sum(axis=1) + supp) / 90.0 * uplift
            std = np.sqrt(np.maximum(blk.Y.var(axis=1), rate))
            emit(keys, rate, rate, std, "BOUNCE_ADJUSTED", SkuClass.CRITICAL_HARD_TO_SOURCE.value)
    return rows, accuracy


def score_live_forecasts(forecasts_yesterday: pd.DataFrame, actual: pd.DataFrame, day, warehouse_ids) -> list[dict[str, Any]]:
    """WAPE/bias of yesterday's horizon-1 forecasts vs realised demand, by class."""
    out = []
    if forecasts_yesterday.empty:
        return out
    f = forecasts_yesterday[forecasts_yesterday.horizon == 1]
    a = actual.groupby(["sku_id", "warehouse_id"]).qty.sum() if not actual.empty else pd.Series(dtype=float)
    f = f.assign(actual=[float(a.get((r.sku_id, r.warehouse_id), 0.0)) for r in f.itertuples()])
    for (wh, cls), g in f.groupby(["warehouse_id", "sku_class"]):
        w, b = wape(g.actual.to_numpy(), g.p50.to_numpy())
        out.append({"date": day, "warehouse_id": wh, "sku_class": cls, "method": "LIVE", "role": "live",
                    "wape": w, "bias": b, "n_skus": len(g)})
    return out
