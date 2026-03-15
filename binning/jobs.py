"""
binning/jobs.py — Background job queue with per-column progress.

Each /api/bin request spawns a thread that bins columns one-by-one,
updating shared job state after each column. The frontend polls
/api/job/<job_id> to show a live progress bar.

With 500 columns the job runs for ~40s (C) or ~400s (NumPy).
The UI stays responsive throughout — no timeouts, no frozen browser.

Thread-safety: all job state mutations go through _LOCK.
"""

import io
import uuid
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np

# Polars for fast file I/O; falls back to pandas if not installed
try:
    import polars as pl
    _USE_POLARS = True
except ImportError:
    import pandas as pd
    _USE_POLARS = False

from binning.algorithm import bin_numeric, bin_numeric_equal_width, bin_categorical, using_c_extension

# ────────────────────────────────────────────────────────────────────
# JOB STORE
# ────────────────────────────────────────────────────────────────────

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

# How many columns to bin in parallel.
# C extension is GIL-free for its inner loops so true parallelism is
# achieved.  NumPy releases the GIL for most operations too.
# Rule of thumb: min(cpu_count, 8) — avoid thrashing on small machines.
import os
_WORKERS = min(os.cpu_count() or 4, 8)


def _new_job(total_cols: int) -> str:
    job_id = str(uuid.uuid4())
    with _LOCK:
        _JOBS[job_id] = {
            "id":          job_id,
            "status":      "running",   # running | done | error
            "total":       total_cols,
            "completed":   0,
            "results":     {},
            "error":       None,
            "started_at":  datetime.utcnow().isoformat(),
            "finished_at": None,
            "backend":     "C" if using_c_extension() else "NumPy",
            "io_backend":  "Polars" if _USE_POLARS else "Pandas",
        }
    return job_id


def _update(job_id: str, col: str, result: dict) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job["results"][col]  = result
        job["completed"]    += 1


def _finish(job_id: str, summary: dict) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job.update(summary)
        job["status"]      = "done"
        job["finished_at"] = datetime.utcnow().isoformat()


def _fail(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job["status"]      = "error"
        job["error"]       = error
        job["finished_at"] = datetime.utcnow().isoformat()


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        # Return a shallow copy so the caller never holds the lock
        return {
            "id":          job["id"],
            "status":      job["status"],
            "total":       job["total"],
            "completed":   job["completed"],
            "pct":         round(job["completed"] / max(job["total"], 1) * 100, 1),
            "error":       job["error"],
            "started_at":  job["started_at"],
            "finished_at": job["finished_at"],
            "backend":     job["backend"],
            "io_backend":  job["io_backend"],
            # Only include results when done to keep poll responses small
            "results":     job["results"] if job["status"] == "done" else {},
            "outcome_col":     job.get("outcome_col"),
            "total_events":    job.get("total_events"),
            "total_n":         job.get("total_n"),
            "avg_event_rate":  job.get("avg_event_rate"),
        }


# ────────────────────────────────────────────────────────────────────
# FILE LOADING  (Polars >> Pandas)
# ────────────────────────────────────────────────────────────────────

def _load_file(file_bytes: bytes, filename: str):
    """
    Load CSV or Excel into a column-accessible structure.
    Returns (getter, columns, n_rows) where getter(col) -> np.ndarray.
    """
    fname = filename.lower()

    if _USE_POLARS:
        if fname.endswith(".csv"):
            df = pl.read_csv(io.BytesIO(file_bytes), infer_schema_length=10000)
        else:
            # Polars doesn't support Excel natively; fall back for xlsx
            import pandas as _pd
            pdf = _pd.read_excel(io.BytesIO(file_bytes))
            df  = pl.from_pandas(pdf)

        def getter(col):
            return df[col].to_numpy()

        return getter, df.columns, len(df)

    else:
        import pandas as _pd
        if fname.endswith(".csv"):
            df = _pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = _pd.read_excel(io.BytesIO(file_bytes))

        def getter(col):
            return df[col].to_numpy()

        return getter, list(df.columns), len(df)


def _column_info(getter, columns):
    """
    Return per-column metadata (dtype, nunique, nulls, guess).
    Same logic as the original /api/upload endpoint.
    """
    import pandas as _pd
    cols = []
    for col in columns:
        arr = getter(col)
        # Use pandas for nunique / dtype detection (fast enough at this stage)
        s   = _pd.Series(arr)
        nu  = int(s.nunique())
        dt  = str(s.dtype)
        guess = "numeric" if _pd.api.types.is_numeric_dtype(s) and nu > 10 \
                else "categorical"
        cols.append({
            "name":    col,
            "dtype":   dt,
            "nunique": nu,
            "nulls":   int(s.isna().sum()),
            "guess":   guess,
        })
    return cols


# ────────────────────────────────────────────────────────────────────
# WORKER — bins one column, called from thread pool
# ────────────────────────────────────────────────────────────────────

def _bin_column(col, col_type, getter, target_arr, max_bins, min_pct, bin_mode="equal_width"):
    """Bin a single column. Returns (col, result_dict)."""
    raw_x = getter(col)
    mask  = ~(
        (raw_x  != raw_x) |
        (target_arr != target_arr)
    )
    try:
        import pandas as _pd
        mask = ~(_pd.isna(raw_x) | _pd.isna(target_arr))
    except Exception:
        pass

    x = raw_x[mask].astype(float if col_type == "numeric" else object)
    y = target_arr[mask].astype(float)

    if col_type == "categorical":
        return col, bin_categorical(x, y)

    # Numeric — respect bin_mode
    if bin_mode == "monotonic":
        return col, bin_numeric(x, y, max_bins, min_pct)
    else:
        return col, bin_numeric_equal_width(x, y, max_bins)


# ────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ────────────────────────────────────────────────────────────────────

def rebin_column(job_id: str, col: str, bin_mode: str,
                 max_bins: int = 20, min_pct: float = 1.0) -> dict:
    """
    Re-bin a single column from an already-completed job.
    Uses the cached file bytes stored on the job.
    Returns the new column result dict, or raises on error.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id} not found")
        file_bytes   = job.get("_file_bytes")
        filename     = job.get("_filename")
        outcome_col  = job.get("outcome_col")
        col_type     = job["results"][col]["type"]

    if file_bytes is None:
        raise RuntimeError("File cache not available — re-upload the file")

    getter, columns, _ = _load_file(file_bytes, filename)
    target_arr = getter(outcome_col).astype(float)

    _, result = _bin_column(col, col_type, getter, target_arr,
                            max_bins, min_pct, bin_mode)

    # Update stored result in place
    with _LOCK:
        _JOBS[job_id]["results"][col] = result

    return result


def start_binning_job(file_bytes: bytes,
                      filename: str,
                      config: dict) -> str:
    """
    Launch a background binning job and return its job_id immediately.
    The caller should poll GET /api/job/<job_id> for progress.
    """
    outcome_col  = config.get("outcome")
    numeric_cols = [c for c in config.get("numeric",     []) if c != outcome_col]
    cat_cols     = [c for c in config.get("categorical", []) if c != outcome_col]
    max_bins     = int(config.get("max_bins", 20))
    min_pct      = float(config.get("min_pct", 1))
    # col_modes: per-column override dict {"col_name": "equal_width"|"monotonic"}
    # Falls back to global bin_mode, which itself falls back to "equal_width"
    global_mode  = config.get("bin_mode", "equal_width")
    col_modes    = config.get("col_modes", {})

    total_cols = len(numeric_cols) + len(cat_cols)
    job_id     = _new_job(total_cols)

    # Cache file bytes so rebin_column can re-use without re-upload
    with _LOCK:
        _JOBS[job_id]["_file_bytes"] = file_bytes
        _JOBS[job_id]["_filename"]   = filename

    def _run():
        try:
            getter, columns, n_rows = _load_file(file_bytes, filename)

            if outcome_col not in columns:
                _fail(job_id, f"Outcome column '{outcome_col}' not found in file")
                return

            target_arr = getter(outcome_col).astype(float)
            te         = int(np.nansum(target_arr))
            tne        = int(np.sum(~np.isnan(target_arr))) - te

            # Cache full DataFrame for model step (WoE transform needs raw values)
            import pandas as _pd
            if filename.lower().endswith(".csv"):
                _df_full = _pd.read_csv(io.BytesIO(file_bytes))
            else:
                _df_full = _pd.read_excel(io.BytesIO(file_bytes))
            with _LOCK:
                _JOBS[job_id]["_df"] = _df_full

            # Build work list: (col, type)
            work = (
                [(c, "numeric")     for c in numeric_cols if c in columns] +
                [(c, "categorical") for c in cat_cols     if c in columns]
            )

            # Parallel execution — per-column mode overrides global
            with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
                futures = {
                    pool.submit(
                        _bin_column, col, col_type, getter, target_arr,
                        max_bins, min_pct,
                        col_modes.get(col, global_mode)   # per-col override
                    ): col
                    for col, col_type in work
                }
                for future in as_completed(futures):
                    col, result = future.result()
                    _update(job_id, col, result)

            avg_event_rate = round(te / max(te + tne, 1), 6)
            _finish(job_id, {
                "outcome_col":    outcome_col,
                "total_events":   te,
                "total_n":        n_rows,
                "avg_event_rate": avg_event_rate,
            })

        except Exception as exc:
            _fail(job_id, f"{exc}\n{traceback.format_exc()}")

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def upload_and_profile(file_bytes: bytes, filename: str) -> dict:
    """
    Fast file profiling for the /api/upload endpoint.
    Returns rows, columns metadata, and a 5-row preview.
    """
    import pandas as _pd

    getter, columns, n_rows = _load_file(file_bytes, filename)
    col_info = _column_info(getter, columns)

    # Preview: read first 5 rows via pandas (small, fine to use here)
    if filename.lower().endswith(".csv"):
        preview_df = _pd.read_csv(io.BytesIO(file_bytes), nrows=5)
    else:
        preview_df = _pd.read_excel(io.BytesIO(file_bytes), nrows=5)

    return {
        "rows":    n_rows,
        "columns": col_info,
        "preview": preview_df.fillna("").astype(str).to_dict(orient="records"),
    }


# ────────────────────────────────────────────────────────────────────
# MODEL JOB
# ────────────────────────────────────────────────────────────────────

_MODEL_JOBS: dict[str, dict] = {}
_MODEL_LOCK = threading.Lock()


def _new_model_job() -> str:
    job_id = str(uuid.uuid4())
    with _MODEL_LOCK:
        _MODEL_JOBS[job_id] = {
            "id":       job_id,
            "status":   "running",
            "progress": 0,
            "message":  "Starting…",
            "result":   None,
            "error":    None,
        }
    return job_id


def get_model_job(job_id: str) -> dict | None:
    with _MODEL_LOCK:
        job = _MODEL_JOBS.get(job_id)
        if job is None:
            return None
        return {
            "id":       job["id"],
            "status":   job["status"],
            "progress": job["progress"],
            "message":  job["message"],
            "result":   job["result"] if job["status"] == "done" else None,
            "error":    job["error"],
        }


def start_model_job(bin_job_id: str, config: dict) -> str:
    """
    Launch a background model fitting job.
    bin_job_id: the completed binning job id (to access cached df + results)
    config: dict with keys alpha, l1_ratio, auto_tune, cv_folds,
                          base_score, pdo, score_min, score_max,
                          selected_cols, outcome_col
    """
    # Read what we need from the binning job
    with _LOCK:
        bin_job = _JOBS.get(bin_job_id)
        if bin_job is None:
            raise KeyError(f"Binning job {bin_job_id} not found")
        df             = bin_job.get("_df")
        binning_results = dict(bin_job.get("results", {}))
        outcome_col    = bin_job.get("outcome_col")

    if df is None:
        raise RuntimeError("Cached dataframe not found — re-run binning first")

    model_job_id = _new_model_job()

    def _progress(step, total, message):
        pct = int(step / total * 100)
        with _MODEL_LOCK:
            _MODEL_JOBS[model_job_id]["progress"] = pct
            _MODEL_JOBS[model_job_id]["message"]  = message

    def _run():
        try:
            from binning.model import run_model_pipeline

            result = run_model_pipeline(
                df              = df,
                binning_results = binning_results,
                outcome_col     = outcome_col,
                selected_cols   = config.get("selected_cols", list(binning_results.keys())),
                alpha           = float(config.get("alpha",     1.0)),
                l1_ratio        = float(config.get("l1_ratio",  0.5)),
                auto_tune       = bool(config.get("auto_tune",  False)),
                cv_folds        = int(config.get("cv_folds",    5)),
                base_score      = int(config.get("base_score",  1500)),
                pdo             = int(config.get("pdo",         20)),
                score_min       = int(config.get("score_min",   1001)),
                score_max       = int(config.get("score_max",   1999)),
                progress_cb     = _progress,
            )

            with _MODEL_LOCK:
                _MODEL_JOBS[model_job_id]["status"]   = "done"
                _MODEL_JOBS[model_job_id]["progress"] = 100
                _MODEL_JOBS[model_job_id]["message"]  = "Done"
                _MODEL_JOBS[model_job_id]["result"]   = result

        except Exception as exc:
            import traceback as _tb
            with _MODEL_LOCK:
                _MODEL_JOBS[model_job_id]["status"] = "error"
                _MODEL_JOBS[model_job_id]["error"]  = f"{exc}\n{_tb.format_exc()}"

    threading.Thread(target=_run, daemon=True).start()
    return model_job_id
