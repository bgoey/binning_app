"""
binning/model.py — Elastic Net scorecard model

Pure functions — no Flask, no I/O.

Pipeline:
  1. apply_woe()        — WoE-encode a DataFrame using binning results
  2. fit_elastic_net()  — fit ElasticNet logistic regression, optionally with CV
  3. scale_scores()     — convert predicted probabilities to PDO credit scores
  4. compute_metrics()  — Gini/AUC, KS, calibration, performance table, coefficients
"""

import math
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score, roc_curve


# ────────────────────────────────────────────────────────────────────
# WoE TRANSFORM
# ────────────────────────────────────────────────────────────────────

def apply_woe(df: pd.DataFrame, binning_results: dict,
              selected_cols: list) -> pd.DataFrame:
    """
    WoE-encode selected columns using the binning results from BinStudio.

    Returns a DataFrame with one column per selected variable (WoE values),
    plus NaN where the bin is unknown.
    """
    out = {}
    for col in selected_cols:
        info = binning_results.get(col)
        if info is None:
            continue

        col_type = info.get("type", "numeric")
        bins     = info.get("bins", [])
        series   = df[col] if col in df.columns else None
        if series is None:
            continue

        woe_values = pd.Series(np.nan, index=df.index, name=col)

        if col_type == "numeric":
            cuts = info.get("cuts", [])
            for i, b in enumerate(bins):
                lo = float("-inf") if i == 0 else (cuts[i-1] if i-1 < len(cuts) else float("inf"))
                hi = cuts[i] if i < len(cuts) else float("inf")
                if i == 0:
                    mask = series <= hi
                elif i == len(bins) - 1:
                    mask = series > lo
                else:
                    mask = (series > lo) & (series <= hi)
                woe_values[mask] = b["woe"]

        else:  # categorical
            woe_map = {b["label"]: b["woe"] for b in bins}
            woe_values = series.astype(str).map(woe_map)

        out[col] = woe_values

    return pd.DataFrame(out)


# ────────────────────────────────────────────────────────────────────
# ELASTIC NET FIT
# ────────────────────────────────────────────────────────────────────

def fit_elastic_net(X: pd.DataFrame, y: pd.Series,
                    alpha: float = 1.0,
                    l1_ratio: float = 0.5,
                    auto_tune: bool = False,
                    cv_folds: int = 5,
                    max_iter: int = 1000,
                    progress_cb=None) -> dict:
    """
    Fit an elastic net logistic regression.

    Parameters
    ----------
    X          : WoE-encoded feature matrix (no NaNs — imputed to 0 before fitting)
    y          : binary target (0/1)
    alpha      : regularisation strength (manual mode)
    l1_ratio   : mix between L1 (1.0) and L2 (0.0)
    auto_tune  : if True, run CV to find optimal C (1/alpha)
    cv_folds   : number of CV folds
    progress_cb: optional callable(step, total, message) for progress updates

    Returns
    -------
    dict with keys: model, scaler, coef_df, cv_results, feature_names
    """
    # Fill NaN WoE values with 0 (neutral WoE — treated as unknown bin)
    X_filled = X.fillna(0.0)
    feature_names = list(X_filled.columns)

    scaler  = StandardScaler()
    X_scaled = scaler.fit_transform(X_filled)

    cv = StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=42)

    if auto_tune:
        if progress_cb: progress_cb(1, 3, "Cross-validating hyperparameters…")

        # Search over C values (inverse of alpha)
        Cs = np.logspace(-3, 2, 20)
        model = LogisticRegressionCV(
            Cs=Cs,
            solver="saga",
            l1_ratios=[l1_ratio],
            cv=cv,
            max_iter=max_iter,
            scoring="roc_auc",
            n_jobs=-1,
            random_state=42,
        )
        if progress_cb: progress_cb(2, 3, "Fitting model…")
        model.fit(X_scaled, y)

        best_C      = float(model.C_[0])
        best_alpha  = 1.0 / best_C
        cv_results  = {
            "best_C":     best_C,
            "best_alpha": best_alpha,
            "l1_ratio":   l1_ratio,
            "method":     "auto (CV)",
        }

    else:
        if progress_cb: progress_cb(1, 3, f"Fitting elastic net (alpha={alpha}, l1_ratio={l1_ratio})…")
        C = 1.0 / max(alpha, 1e-6)
        model = LogisticRegression(
            solver="saga",
            C=C,
            l1_ratio=l1_ratio,
            max_iter=max_iter,
            random_state=42,
        )
        model.fit(X_scaled, y)
        cv_results = {
            "best_C":     C,
            "best_alpha": alpha,
            "l1_ratio":   l1_ratio,
            "method":     "manual",
        }

    if progress_cb: progress_cb(3, 3, "Computing metrics…")

    # Coefficient table
    coef_df = pd.DataFrame({
        "variable":   feature_names,
        "coefficient": model.coef_[0],
        "abs_coef":   np.abs(model.coef_[0]),
    }).sort_values("abs_coef", ascending=False).reset_index(drop=True)
    coef_df["in_model"] = coef_df["coefficient"].abs() > 1e-6

    return {
        "model":         model,
        "scaler":        scaler,
        "coef_df":       coef_df,
        "cv_results":    cv_results,
        "feature_names": feature_names,
    }


# ────────────────────────────────────────────────────────────────────
# CROSS-VALIDATION PERFORMANCE
# ────────────────────────────────────────────────────────────────────

def cross_validate_performance(X: pd.DataFrame, y: pd.Series,
                                model_result: dict,
                                cv_folds: int = 5) -> dict:
    """
    Run stratified k-fold CV and return per-fold Gini scores.
    """
    X_filled = X.fillna(0.0)
    scaler   = StandardScaler()
    X_scaled = scaler.fit_transform(X_filled)

    cv      = StratifiedKFold(n_splits=cv_folds, shuffle=True, random_state=42)
    m       = model_result["model"]

    fold_ginis = []
    fold_ks    = []

    for train_idx, val_idx in cv.split(X_scaled, y):
        X_tr, X_val = X_scaled[train_idx], X_scaled[val_idx]
        y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

        m_fold = LogisticRegression(
            solver="saga",
            C=m.C, l1_ratio=m.l1_ratio,
            max_iter=1000, random_state=42,
        )
        m_fold.fit(X_tr, y_tr)
        p_val = m_fold.predict_proba(X_val)[:, 1]

        auc = roc_auc_score(y_val, p_val)
        fold_ginis.append(round((2 * auc - 1) * 100, 2))

        # KS
        fpr, tpr, _ = roc_curve(y_val, p_val)
        fold_ks.append(round(float(np.max(tpr - fpr)) * 100, 2))

    return {
        "fold_ginis": fold_ginis,
        "mean_gini":  round(float(np.mean(fold_ginis)), 2),
        "std_gini":   round(float(np.std(fold_ginis)), 2),
        "fold_ks":    fold_ks,
        "mean_ks":    round(float(np.mean(fold_ks)), 2),
    }


# ────────────────────────────────────────────────────────────────────
# SCORE SCALING  (PDO scorecard)
# ────────────────────────────────────────────────────────────────────

def scale_scores(probs: np.ndarray,
                 base_score: int = 1500,
                 pdo: int = 20,
                 base_odds: float = None,
                 score_min: int = 1001,
                 score_max: int = 1999) -> np.ndarray:
    """
    Convert predicted probabilities to PDO credit scores.

    Formula:
        factor = PDO / ln(2)
        offset = base_score - factor × ln(base_odds)
        score  = offset - factor × ln(p / (1-p))

    Where base_odds = (1-p_portfolio) / p_portfolio if not specified.

    Scores are clipped to [score_min, score_max].
    """
    # Clip probs to avoid log(0)
    p = np.clip(probs, 1e-6, 1 - 1e-6)

    # Portfolio odds if not provided
    if base_odds is None:
        portfolio_p = float(np.mean(p))
        base_odds   = (1 - portfolio_p) / portfolio_p

    factor = pdo / math.log(2)
    offset = base_score - factor * math.log(base_odds)

    log_odds = np.log(p / (1 - p))
    scores   = offset - factor * log_odds

    return np.clip(np.round(scores).astype(int), score_min, score_max)


# ────────────────────────────────────────────────────────────────────
# METRICS
# ────────────────────────────────────────────────────────────────────

def compute_gini(y_true, y_prob):
    auc = roc_auc_score(y_true, y_prob)
    return round((2 * auc - 1) * 100, 4)


def compute_ks(y_true, y_prob):
    fpr, tpr, thresholds = roc_curve(y_true, y_prob)
    ks = float(np.max(tpr - fpr))
    ks_threshold = float(thresholds[np.argmax(tpr - fpr)])
    return round(ks * 100, 4), round(ks_threshold, 6)


def compute_roc_curve(y_true, y_prob):
    fpr, tpr, _ = roc_curve(y_true, y_prob)
    # Downsample to max 200 points for frontend
    step = max(1, len(fpr) // 200)
    return {
        "fpr": fpr[::step].tolist(),
        "tpr": tpr[::step].tolist(),
    }


def compute_calibration(y_true, y_prob, n_bins=10):
    """
    Group records into n_bins by predicted probability,
    return mean predicted vs mean observed per bin.
    """
    df = pd.DataFrame({"prob": y_prob, "actual": y_true})
    df["bin"] = pd.qcut(df["prob"], q=n_bins, duplicates="drop")

    rows = []
    for b, grp in df.groupby("bin", observed=True):
        rows.append({
            "bin_mid":    float(grp["prob"].mean()),
            "pred_rate":  float(grp["prob"].mean()),
            "actual_rate": float(grp["actual"].mean()),
            "n":          len(grp),
        })
    return sorted(rows, key=lambda r: r["bin_mid"])


def compute_performance_table(y_true, scores, n_bands=20):
    """
    Cut scores into n_bands equal-count ventiles.
    Sorted LOW score → HIGH score (band 1 = highest risk = most events).
    This gives a cumulative capture curve that rises steeply then flattens,
    always above the random diagonal.

    Columns: band, score_from, score_to, n, events, event_rate,
             cum_n, cum_events, cum_event_rate, pct_of_total
    """
    df = pd.DataFrame({"score": scores, "actual": y_true})
    df = df.sort_values("score", ascending=True).reset_index(drop=True)

    # Cut into n_bands equal-count groups
    df["band"] = pd.qcut(df.index, q=n_bands,
                         labels=range(1, n_bands+1))

    total_n      = len(df)
    total_events = int(df["actual"].sum())

    rows = []
    cum_n = 0
    cum_ev = 0

    for band in range(1, n_bands + 1):
        grp = df[df["band"] == band]
        n   = len(grp)
        ev  = int(grp["actual"].sum())
        cum_n  += n
        cum_ev += ev

        rows.append({
            "band":           band,
            "score_from":     int(grp["score"].min()),
            "score_to":       int(grp["score"].max()),
            "n":              n,
            "events":         ev,
            "event_rate":     round(ev / n, 6) if n else 0,
            "cum_n":          cum_n,
            "cum_events":     cum_ev,
            "cum_event_rate": round(cum_ev / cum_n, 6) if cum_n else 0,
            "pct_of_total":   round(cum_n / total_n, 6),
            "pct_events_captured": round(cum_ev / total_events, 6) if total_events else 0,
        })

    return rows


# ────────────────────────────────────────────────────────────────────
# FULL PIPELINE
# ────────────────────────────────────────────────────────────────────

def run_model_pipeline(df: pd.DataFrame,
                       binning_results: dict,
                       outcome_col: str,
                       selected_cols: list,
                       alpha: float = 1.0,
                       l1_ratio: float = 0.5,
                       auto_tune: bool = False,
                       cv_folds: int = 5,
                       base_score: int = 1500,
                       pdo: int = 20,
                       score_min: int = 1001,
                       score_max: int = 1999,
                       progress_cb=None) -> dict:
    """
    Full model pipeline. Returns a results dict suitable for JSON serialisation.
    """
    def _cb(step, total, msg):
        if progress_cb: progress_cb(step, total, msg)

    _cb(1, 8, "Applying WoE transform…")
    X = apply_woe(df, binning_results, selected_cols)
    y = df[outcome_col].astype(float)

    # Align index
    valid = X.notna().any(axis=1) & y.notna()
    X, y  = X[valid], y[valid]

    _cb(2, 8, "Fitting elastic net…")
    fit = fit_elastic_net(X, y,
                          alpha=alpha, l1_ratio=l1_ratio,
                          auto_tune=auto_tune, cv_folds=cv_folds,
                          progress_cb=lambda s, t, m: _cb(s+1, 8, m))

    _cb(5, 8, "Cross-validating performance…")
    cv_perf = cross_validate_performance(X, y, fit, cv_folds=cv_folds)

    _cb(6, 8, "Computing in-sample metrics…")
    X_scaled = fit["scaler"].transform(X.fillna(0.0))
    probs    = fit["model"].predict_proba(X_scaled)[:, 1]
    scores   = scale_scores(probs,
                            base_score=base_score, pdo=pdo,
                            score_min=score_min, score_max=score_max)

    gini        = compute_gini(y, probs)
    ks, ks_thr  = compute_ks(y, probs)
    roc         = compute_roc_curve(y, probs)
    calibration = compute_calibration(y, probs)
    perf_table  = compute_performance_table(y, scores)

    _cb(7, 8, "Building coefficient table…")
    coef_df     = fit["coef_df"]

    _cb(8, 8, "Done")

    return {
        "status":       "done",
        # Hyperparameters
        "cv_results":   fit["cv_results"],
        "cv_folds":     cv_folds,
        # In-sample metrics
        "gini":         gini,
        "ks":           ks,
        "ks_threshold": ks_thr,
        # CV performance
        "cv_gini_mean": cv_perf["mean_gini"],
        "cv_gini_std":  cv_perf["std_gini"],
        "cv_gini_folds":cv_perf["fold_ginis"],
        "cv_ks_mean":   cv_perf["mean_ks"],
        # Charts / tables
        "roc_curve":    roc,
        "calibration":  calibration,
        "perf_table":   perf_table,
        "coefficients": coef_df[["variable","coefficient","abs_coef","in_model"]]
                               .to_dict(orient="records"),
        # Score config
        "base_score":   base_score,
        "pdo":          pdo,
        "score_min":    score_min,
        "score_max":    score_max,
        "n_records":    int(len(y)),
        "n_events":     int(y.sum()),
        "portfolio_rate": round(float(y.mean()), 6),
        "selected_cols":  selected_cols,
        "n_in_model":   int((coef_df["coefficient"].abs() > 1e-6).sum()),
    }
