"""
binning/algorithm.py — Pure binning logic. No Flask, no I/O.

Automatically uses the C extension (_monotonic) when available,
falls back to NumPy for the same results at ~10x slower speed.

500 columns x 4M rows benchmark (single-threaded):
    NumPy only  :  ~400s
    C extension :  ~40s
    C + parallel:  ~10s  (parallelism handled in jobs.py)
"""

import math
import numpy as np

# ── Try to load C extension, fall back gracefully ───────────────────
try:
    from binning._monotonic import equal_freq_bins_c, make_monotonic_c
    _USE_C = True
except ImportError:
    _USE_C = False


# ────────────────────────────────────────────────────────────────────
# WOE / IV
# ────────────────────────────────────────────────────────────────────

def woe_iv(events, non_events, total_events, total_non_events, smoothing=0.5):
    """WoE and IV contribution for one bin."""
    dist_e  = max(events,     smoothing) / max(total_events,     1)
    dist_ne = max(non_events, smoothing) / max(total_non_events, 1)
    woe = math.log(dist_e / dist_ne)
    iv  = (dist_e - dist_ne) * woe
    return round(woe, 6), round(iv, 6)


def enrich_bins(bins, total_events, total_non_events):
    """Attach woe, iv, event_rate, non_events to each bin dict."""
    out = []
    for b in bins:
        ev  = b["events"]
        ne  = b["n"] - ev
        woe, iv = woe_iv(ev, ne, total_events, total_non_events)
        out.append({
            **b,
            "non_events": ne,
            "event_rate": round(ev / b["n"], 6) if b["n"] > 0 else 0,
            "woe": woe,
            "iv":  iv,
        })
    return out


# ────────────────────────────────────────────────────────────────────
# EQUAL-FREQUENCY BINNING
# ────────────────────────────────────────────────────────────────────

def _equal_freq_bins_numpy(x, y, max_bins, min_pct):
    """NumPy fallback — data must already be sorted by x.

    max_bins is always honoured as the bin count.
    min_pct is enforced afterwards: any bin smaller than the threshold
    is merged into its smallest neighbour until all bins are large enough.
    This means the user gets exactly max_bins (or fewer if data is tiny),
    never silently fewer because of a high min_pct value.
    """
    n        = len(x)
    n_bins   = min(max_bins, n)
    bin_size = n // n_bins
    raw, i   = [], 0

    while i < n:
        chunk_x = x[i:] if len(raw) == n_bins - 1 else x[i:i+bin_size]
        chunk_y = y[i:] if len(raw) == n_bins - 1 else y[i:i+bin_size]
        raw.append({
            "min":    float(chunk_x[0]),
            "max":    float(chunk_x[-1]),
            "n":      int(len(chunk_x)),
            "events": int(chunk_y.sum()),
        })
        i += len(chunk_x)
        if i >= n:
            break

    # Enforce min_pct: merge undersized bins into their smallest neighbour.
    if min_pct > 0:
        min_count = max(1, int(n * min_pct / 100))
        changed = True
        while changed and len(raw) > 1:
            changed = False
            for idx in range(len(raw)):
                if raw[idx]["n"] < min_count:
                    if idx == 0:
                        j = 1
                    elif idx == len(raw) - 1:
                        j = idx - 1
                    else:
                        j = idx - 1 if raw[idx-1]["n"] <= raw[idx+1]["n"] else idx + 1
                    lo, hi = min(idx, j), max(idx, j)
                    merged = {
                        "min":    raw[lo]["min"],
                        "max":    raw[hi]["max"],
                        "n":      raw[lo]["n"] + raw[hi]["n"],
                        "events": raw[lo]["events"] + raw[hi]["events"],
                    }
                    raw = raw[:lo] + [merged] + raw[hi+1:]
                    changed = True
                    break

    return raw


def equal_width_bins(x, y, n_bins=10):
    """
    Simple equal-width (equal-cut) binning.
    Splits the value range into n_bins intervals of identical width.
    No monotonic enforcement — just a straight cut.
    x, y: numpy arrays with NaN rows already removed by caller.
    Returns list of bin dicts {min, max, n, events}.
    """
    if len(x) == 0:
        return []

    lo, hi  = float(x.min()), float(x.max())
    if lo == hi:
        return [{"min": lo, "max": hi, "n": len(x), "events": int(y.sum())}]

    edges = np.linspace(lo, hi, n_bins + 1)
    raw   = []

    for i in range(n_bins):
        edge_lo = edges[i]
        edge_hi = edges[i + 1]
        if i == 0:
            mask = (x >= edge_lo) & (x <= edge_hi)
        else:
            mask = (x > edge_lo) & (x <= edge_hi)
        n_bin = int(mask.sum())
        if n_bin == 0:
            continue
        raw.append({
            "min":    edge_lo,
            "max":    edge_hi,
            "n":      n_bin,
            "events": int(y[mask].sum()),
        })

    return raw


def equal_freq_bins(x, y, max_bins, min_pct):
    """
    Build equal-frequency initial bins.
    Sorts internally; delegates hot loop to C if available.
    """
    order  = np.argsort(x, kind="quicksort")
    xs, ys = x[order], y[order]

    if _USE_C:
        return equal_freq_bins_c(xs.tolist(), ys.tolist(), max_bins, min_pct)
    return _equal_freq_bins_numpy(xs, ys, max_bins, min_pct)


# ────────────────────────────────────────────────────────────────────
# MONOTONIC MERGE
# ────────────────────────────────────────────────────────────────────

def _try_monotonic_numpy(bins, direction):
    merged  = [dict(b) for b in bins]
    changed = True
    while changed and len(merged) > 2:
        changed = False
        rates   = [b["events"] / b["n"] if b["n"] else 0 for b in merged]
        worst_i, worst_v = -1, 0
        for i in range(1, len(merged)):
            viol = (rates[i-1] - rates[i]) if direction == "inc" \
                   else (rates[i] - rates[i-1])
            if viol > worst_v:
                worst_v, worst_i = viol, i
        if worst_i > -1:
            a, b = merged[worst_i-1], merged[worst_i]
            merged[worst_i-1:worst_i+1] = [{
                "min":    a["min"], "max": b["max"],
                "n":      a["n"]   + b["n"],
                "events": a["events"] + b["events"],
            }]
            changed = True
    return merged


def make_monotonic(bins):
    """
    Greedy merge until event-rate sequence is monotone.
    Tries both directions; returns whichever gives higher total IV.
    Uses C extension when available.
    """
    if _USE_C:
        result, _ = make_monotonic_c(bins)
        return result

    te  = sum(b["events"] for b in bins)
    tne = sum(b["n"] - b["events"] for b in bins)

    def total_iv(bs):
        return sum(b["iv"] for b in enrich_bins(bs, te, tne))

    inc = _try_monotonic_numpy(bins, "inc")
    dec = _try_monotonic_numpy(bins, "dec")
    return inc if total_iv(inc) >= total_iv(dec) else dec


# ────────────────────────────────────────────────────────────────────
# HIGH-LEVEL COLUMN BINNERS
# ────────────────────────────────────────────────────────────────────

def bin_numeric_equal_width(x, y, n_bins=10):
    """
    Equal-width binning pipeline for one column.
    x, y: numpy arrays with NaN rows already removed by caller.
    Returns dict: {type, bins, cuts, total_iv, bin_mode}
    """
    raw  = equal_width_bins(x, y, n_bins)
    te   = int(y.sum())
    tne  = len(y) - te
    bins = enrich_bins(raw, te, tne)
    cuts = sorted({b["min"] for b in bins[1:]})
    return {
        "type":     "numeric",
        "bins":     bins,
        "cuts":     cuts,
        "total_iv": round(sum(b["iv"] for b in bins), 6),
        "bin_mode": "equal_width",
    }


def bin_numeric(x, y, max_bins=20, min_pct=1.0):
    """
    Full monotonic binning pipeline for one column.
    x, y: numpy arrays with NaN rows already removed by caller.
    Returns dict: {type, bins, cuts, total_iv, bin_mode}
    """
    raw  = equal_freq_bins(x, y, max_bins, min_pct)
    mono = make_monotonic(raw)
    te   = int(y.sum())
    tne  = len(y) - te
    bins = enrich_bins(mono, te, tne)
    cuts = sorted({b["min"] for b in bins[1:]})
    return {
        "type":     "numeric",
        "bins":     bins,
        "cuts":     cuts,
        "total_iv": round(sum(b["iv"] for b in bins), 6),
        "bin_mode": "monotonic",
    }


def bin_categorical(categories, y):
    """
    Categorical binning: one bin per unique value, sorted by event count.
    categories, y: numpy arrays with NaN rows already removed by caller.
    Returns dict: {type, bins, total_iv}
    """
    unique_cats, inverse = np.unique(categories, return_inverse=True)
    te  = int(y.sum())
    tne = len(y) - te

    bins = []
    for idx, cat in enumerate(unique_cats):
        mask   = inverse == idx
        n      = int(mask.sum())
        events = int(y[mask].sum())
        bins.append({"label": str(cat), "n": n, "events": events})

    bins.sort(key=lambda b: b["events"])
    enriched = enrich_bins(bins, te, tne)
    return {
        "type":     "categorical",
        "bins":     enriched,
        "total_iv": round(sum(b["iv"] for b in enriched), 6),
    }


def using_c_extension():
    """Returns True if the C extension is loaded."""
    return _USE_C
