"""
app.py — Flask routes only. Zero business logic.

Each route handler does exactly three things:
  1. Parse / validate the request
  2. Call into binning/ modules
  3. Return JSON

Structure
---------
app.py                   ← you are here (routes only)
binning/
    algorithm.py         ← pure math: WoE, equal-freq bins, monotonic merge
    jobs.py              ← background workers, Polars I/O, progress tracking
    export.py            ← code generation, CSV export
    monotonic_c.c        ← C extension (optional, auto-detected)
    setup.py             ← build the C extension
"""

import json
import traceback

from flask import Flask, jsonify, request, send_from_directory

from binning.jobs   import start_binning_job, get_job, upload_and_profile, rebin_column, start_model_job, get_model_job
from binning.export import generate_python_code, generate_woe_csv, generate_woe_excel, generate_apps_script
from binning.algorithm import enrich_bins

app = Flask(__name__, static_folder="static", template_folder="templates")


# ── CORS ─────────────────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


# ── Static / SPA ─────────────────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve(path):
    if path and (path.startswith("static/") or path.startswith("api/")):
        return send_from_directory(".", path)
    return send_from_directory("templates", "index.html")


# ────────────────────────────────────────────────────────────────────
# UPLOAD  —  profile the file, return column metadata + preview
# ────────────────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST", "OPTIONS"])
def upload():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "No file uploaded"}), 400
        fname = file.filename.lower()
        if not fname.endswith((".csv", ".xls", ".xlsx")):
            return jsonify({"error": "Unsupported format (use CSV or Excel)"}), 400

        result = upload_and_profile(file.read(), file.filename)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ────────────────────────────────────────────────────────────────────
# BIN  —  start background job, return job_id immediately
# ────────────────────────────────────────────────────────────────────
@app.route("/api/bin", methods=["POST", "OPTIONS"])
def bin_data():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        file       = request.files.get("file")
        config_raw = request.form.get("config", "{}")
        config     = json.loads(config_raw)

        if not file:
            return jsonify({"error": "No file"}), 400
        if not config.get("outcome"):
            return jsonify({"error": "No outcome column specified"}), 400

        job_id = start_binning_job(file.read(), file.filename, config)
        return jsonify({"job_id": job_id})

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ────────────────────────────────────────────────────────────────────
# JOB STATUS  —  poll for progress and results
# ────────────────────────────────────────────────────────────────────
@app.route("/api/job/<job_id>", methods=["GET"])
def job_status(job_id):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ────────────────────────────────────────────────────────────────────
# MERGE  —  manual bin merge in the UI
# ────────────────────────────────────────────────────────────────────
@app.route("/api/merge", methods=["POST", "OPTIONS"])
def merge_bins():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body     = request.get_json()
        bins     = body["bins"]
        indices  = sorted(body["indices"])
        col_type = body["type"]
        te       = body["total_events"]
        tne      = body["total_non_events"]

        merged_bin = {
            "n":      sum(bins[i]["n"]      for i in indices),
            "events": sum(bins[i]["events"] for i in indices),
        }
        if col_type == "numeric":
            merged_bin["min"] = bins[indices[0]]["min"]
            merged_bin["max"] = bins[indices[-1]]["max"]
        else:
            merged_bin["label"] = " | ".join(bins[i]["label"] for i in indices)

        new_bins = [b for i, b in enumerate(bins) if i not in indices]
        new_bins.insert(indices[0], merged_bin)

        enriched = enrich_bins(new_bins, te, tne)
        return jsonify({
            "bins":     enriched,
            "total_iv": round(sum(b["iv"] for b in enriched), 6),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────────
# EXPORT  —  Python transformer or WoE CSV
# ────────────────────────────────────────────────────────────────────
@app.route("/api/export_code", methods=["POST", "OPTIONS"])
def export_code():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body        = request.get_json()
        results     = body["results"]
        outcome_col = body.get("outcome_col", "target")
        code        = generate_python_code(results, outcome_col)
        return jsonify({"code": code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────────
# REBIN  —  re-bin a single column with a different mode
# ────────────────────────────────────────────────────────────────────
@app.route("/api/rebin", methods=["POST", "OPTIONS"])
def rebin():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body     = request.get_json()
        job_id   = body["job_id"]
        col      = body["col"]
        bin_mode = body["bin_mode"]          # "equal_width" | "monotonic"
        max_bins = int(body.get("max_bins", 10))
        min_pct  = float(body.get("min_pct", 5))

        result = rebin_column(job_id, col, bin_mode, max_bins, min_pct)
        return jsonify({"col": col, "result": result})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/export_csv", methods=["POST", "OPTIONS"])
def export_csv():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body    = request.get_json()
        results = body["results"]
        csv_str = generate_woe_csv(results)
        return jsonify({"csv": csv_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export_excel", methods=["POST", "OPTIONS"])
def export_excel():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        import io
        from flask import send_file
        body        = request.get_json()
        results     = body["results"]
        outcome_col = body.get("outcome_col", "target")
        xlsx_bytes  = generate_woe_excel(results, outcome_col)
        return send_file(
            io.BytesIO(xlsx_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="woe_binning.xlsx",
        )
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/export_apps_script", methods=["POST", "OPTIONS"])
def export_apps_script():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body    = request.get_json()
        results = body["results"]
        script  = generate_apps_script(results)
        return jsonify({"script": script})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ────────────────────────────────────────────────────────────────────
# MODEL — fit elastic net scorecard
# ────────────────────────────────────────────────────────────────────
@app.route("/api/model/fit", methods=["POST", "OPTIONS"])
def model_fit():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        body        = request.get_json()
        bin_job_id  = body["bin_job_id"]
        config      = body.get("config", {})
        model_job_id = start_model_job(bin_job_id, config)
        return jsonify({"model_job_id": model_job_id})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/model/status/<model_job_id>", methods=["GET"])
def model_status(model_job_id):
    job = get_model_job(model_job_id)
    if job is None:
        return jsonify({"error": "Model job not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
