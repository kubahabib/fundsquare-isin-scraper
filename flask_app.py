"""
fundsquare.net ISIN scraper - plain HTTPS app (no WebSocket).

Run locally:  python flask_app.py
PythonAnywhere / Cloud Run: gunicorn -w 1 -t 120 flask_app:app
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
from flask import Flask, Response, jsonify, render_template_string, request

from scraper import RESULT_COLUMNS, parse_url_list, run_scraper

app = Flask(__name__)

# In-memory jobs (one worker). Entries expire after 1 hour.
JOBS: dict[str, dict] = {}
JOB_TTL = timedelta(hours=1)
JOBS_LOCK = threading.Lock()

PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Data Authority Paris ISIN Scraper Utility</title>
  <style>
    :root {
      --bg: #101614; --card: #1A221F; --text: #E8EEEB; --muted: #9AABA3;
      --border: #2C3A34; --input: #121A18; --accent: #2E8B57; --accent-deep: #1F6B42;
      --success-bg: #1A3328; --success-border: #2E8B57;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; background: var(--bg); color: var(--text);
      font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    }
    main { max-width: 1100px; margin: 0 auto; padding: 2rem 1.2rem 3rem; }
    h1 { color: #D8F3E4; font-size: 1.75rem; margin: 0 0 .3rem; }
    .sub { color: var(--muted); margin: 0 0 1.4rem; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--card); border: 1px solid var(--border); border-radius: 16px;
      padding: 1.1rem 1.2rem; box-shadow: 0 8px 24px rgba(0,0,0,.35);
    }
    .card h2 { font-size: 1.05rem; margin: 0 0 .8rem; }
    label { display: block; font-weight: 600; margin: .4rem 0 .35rem; }
    .hint { color: var(--muted); font-size: .92rem; margin: .4rem 0 0; }
    .radios { display: flex; gap: 1.2rem; flex-wrap: wrap; margin: .3rem 0 .5rem; }
    textarea {
      width: 100%; min-height: 118px; background: var(--input); color: var(--text);
      border: 1px solid var(--border); border-radius: 12px; padding: .7rem .8rem;
      font: inherit;
    }
    button, .btn {
      display: inline-block; width: 100%; margin-top: .8rem; padding: .75rem 1rem;
      background: var(--accent); color: #fff; border: 0; border-radius: 10px;
      font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
      text-align: center; text-decoration: none; cursor: pointer;
    }
    button:hover, .btn:hover { background: var(--accent-deep); }
    button:disabled { opacity: .6; cursor: wait; }
    .status { margin: 1rem 0 0; color: var(--muted); min-height: 1.2em; }
    .success {
      background: var(--success-bg); border: 1px solid var(--success-border);
      color: #D8F3E4; border-radius: 10px; padding: .7rem .95rem; font-weight: 600;
      margin: 0 0 .8rem;
    }
    .error { color: #f0a8a8; }
    table { width: 100%; border-collapse: collapse; font-size: .92rem; }
    th, td { text-align: left; padding: .45rem .55rem; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); }
    a { color: var(--accent); }
    details.how {
      margin-top: 1rem; background: var(--card); border: 1px solid var(--border);
      border-radius: 16px; padding: .85rem 1.2rem; color: var(--muted);
    }
    details.how summary { cursor: pointer; color: var(--text); font-weight: 600; }
    details.how ul { margin: .7rem 0 0; padding-left: 1.2rem; }
    details.how li { margin: .35rem 0; }
  </style>
</head>
<body>
<main>
  <h1>Data Authority Paris ISIN Scraper Utility</h1>
  <p class="sub">Streamline the extraction of ISIN identifiers from fundsquare.net fund structures.</p>

  <form id="form">
    <div class="grid">
      <section class="card">
        <h2>⚙ Scraping Configuration</h2>
        <label title="The choice applies to every link you paste on the right.">Identify data to extract?</label>
        <div class="radios">
          <label><input type="radio" name="mode" value="fund" checked> Whole fund structure</label>
          <label><input type="radio" name="mode" value="sub"> Specific sub-funds</label>
        </div>
        <p class="hint" id="hint"></p>
      </section>
      <section class="card">
        <h2>🔗 Fund Tree URLs</h2>
        <textarea id="urls" name="urls" placeholder="https://www.fundsquare.net/fund-tree?idInstr=114412"></textarea>
        <button type="submit" id="go">SCRAPE ISINs</button>
        <p class="status" id="status"></p>
      </section>
    </div>
  </form>

  <section class="card" id="results-card" style="margin-top:1rem; display:none;">
    <p class="hint" style="margin:0 0 .2rem; font-weight:600; color: var(--text);">📊 Results</p>
    <h2 style="color:#D8F3E4; font-size:1.35rem;">Results Summary</h2>
    <div id="summary"></div>
    <label><input type="checkbox" id="details"> Show fund, sub-fund and share class columns</label>
    <p><a class="btn" id="csv" href="#">Download CSV</a></p>
    <div class="wrap"><table id="table"></table></div>
    <div id="problems"></div>
  </section>

  <details class="how">
    <summary>How it works</summary>
    <ul>
      <li>The fund tree on fundsquare.net is rendered server-side: every click on a sub-fund is a plain HTTP request that returns the whole page with that node expanded.</li>
      <li>The application keeps one session per link (the server ties its state to cookies), opens each sub-fund with roughly one request per second and parses every response.</li>
      <li>ISINs are extracted from the share-class rows with a regular expression (2 letters + 9 alphanumerics + 1 check digit) and de-duplicated per fund.</li>
      <li><strong>Whole fund structure</strong> collects every sub-fund (a folderId in the link is ignored). <strong>Specific sub-funds</strong> collects only the sub-fund of each pasted link (the link must contain folderId=..., which appears in the address bar after you click the sub-fund).</li>
      <li>Broken links are skipped and reported in the Links with problems table.</li>
    </ul>
  </details>
</main>
<script>
const hintFund = "Paste the fund-tree link of the fund (for example ...fund-tree?idInstr=114412). The app opens every sub-fund and collects all ISINs. Any folderId in the link is ignored.";
const hintSub = "On fundsquare.net click the sub-fund, then copy the link from the address bar (it contains folderId=...). One link = one sub-fund; you can paste several.";
const placeholderFund = "https://www.fundsquare.net/fund-tree?idInstr=114412";
const placeholderSub = "https://www.fundsquare.net/fund-tree?_eventId=expand&folderId=34166&idInstr=114412#34166\nhttps://www.fundsquare.net/fund-tree?_eventId=expand&folderId=169257&idInstr=114412#169257";
const hint = document.getElementById("hint");
const urlsInput = document.getElementById("urls");
const form = document.getElementById("form");
const statusEl = document.getElementById("status");
const go = document.getElementById("go");
let jobId = null, lastPayload = null, timer = null;

function currentHint() {
  const sub = document.querySelector("input[name=mode]:checked").value === "sub";
  hint.textContent = sub ? hintSub : hintFund;
  urlsInput.placeholder = sub ? placeholderSub : placeholderFund;
}
document.querySelectorAll("input[name=mode]").forEach(el => el.addEventListener("change", currentHint));
currentHint();

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  go.disabled = true;
  statusEl.textContent = "Starting...";
  document.getElementById("results-card").style.display = "none";
  const body = {
    mode: document.querySelector("input[name=mode]:checked").value,
    urls: document.getElementById("urls").value,
  };
  const res = await fetch("/jobs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) {
    statusEl.innerHTML = '<span class="error">' + (data.error || "Unable to start extraction.") + "</span>";
    go.disabled = false;
    return;
  }
  jobId = data.id;
  poll();
});

async function poll() {
  const res = await fetch("/jobs/" + jobId);
  const data = await res.json();
  statusEl.textContent = data.message || data.status;
  if (data.status === "running") {
    timer = setTimeout(poll, 1500);
    return;
  }
  go.disabled = false;
  if (data.status === "error") {
    statusEl.innerHTML = '<span class="error">' + (data.message || "Failed") + "</span>";
    return;
  }
  lastPayload = data;
  render();
}

document.getElementById("details").addEventListener("change", render);

function render() {
  if (!lastPayload) return;
  const details = document.getElementById("details").checked;
  const cols = details
    ? ["ISIN", "Source URL", "Fund", "Sub-fund", "Share class"]
    : ["ISIN", "Source URL"];
  const rows = lastPayload.results || [];
  const problems = lastPayload.problems || [];
  document.getElementById("results-card").style.display = "block";
  document.getElementById("summary").innerHTML =
    rows.length === 0
      ? '<p class="error">No ISINs found. Check the links below.</p>'
      : '<div class="success">Found ' + lastPayload.unique_isins + " unique ISINs from "
        + lastPayload.ok_links + " of " + lastPayload.n_links + " links.</div>";
  document.getElementById("csv").href = "/jobs/" + jobId + "/csv?details=" + (details ? "1" : "0");
  const table = document.getElementById("table");
  table.innerHTML = "<thead><tr>" + cols.map(c => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>"
    + rows.map(r => "<tr>" + cols.map(c => "<td>" + escapeHtml(r[c] || "") + "</td>").join("") + "</tr>").join("")
    + "</tbody>";
  if (problems.length) {
    document.getElementById("problems").innerHTML = "<h3>Links with problems</h3><div class='wrap'><table><thead><tr><th>URL</th><th>Status</th><th>Details</th></tr></thead><tbody>"
      + problems.map(p => "<tr><td>" + escapeHtml(p.URL) + "</td><td>" + escapeHtml(p.Status) + "</td><td>" + escapeHtml(p.Details) + "</td></tr>").join("")
      + "</tbody></table></div>";
  } else {
    document.getElementById("problems").innerHTML = "";
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
</script>
</body>
</html>
"""


def _purge_jobs() -> None:
    cutoff = datetime.now(timezone.utc) - JOB_TTL
    with JOBS_LOCK:
        expired = [jid for jid, job in JOBS.items() if job["created"] < cutoff]
        for jid in expired:
            JOBS.pop(jid, None)


def _run_job(job_id: str, urls: list[str], single_sub_fund: bool) -> None:
    def progress(fraction: float, message: str) -> None:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["message"] = message
                JOBS[job_id]["fraction"] = float(fraction)

    try:
        results, problems = run_scraper(urls, single_sub_fund=single_sub_fund, progress=progress)
        n_failed = problems.loc[problems["Status"] == "failed", "URL"].nunique() if not problems.empty else 0
        with JOBS_LOCK:
            JOBS[job_id].update(
                {
                    "status": "done",
                    "message": "Done",
                    "fraction": 1.0,
                    "results": results,
                    "problems": problems,
                    "n_links": len(urls),
                    "ok_links": len(urls) - int(n_failed),
                    "unique_isins": int(results["ISIN"].nunique()) if not results.empty else 0,
                }
            )
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the UI
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id].update({"status": "error", "message": str(exc)})


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.post("/jobs")
def start_job():
    _purge_jobs()
    payload = request.get_json(silent=True) or {}
    urls = parse_url_list(str(payload.get("urls") or ""))
    if not urls:
        return jsonify({"error": "Paste at least one link first."}), 400
    single = payload.get("mode") == "sub"
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "message": "Starting...",
            "fraction": 0.0,
            "created": datetime.now(timezone.utc),
            "results": pd.DataFrame(columns=RESULT_COLUMNS),
            "problems": pd.DataFrame(),
        }
    threading.Thread(target=_run_job, args=(job_id, urls, single), daemon=True).start()
    return jsonify({"id": job_id})


@app.get("/jobs/<job_id>")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        body = {
            "status": job["status"],
            "message": job["message"],
            "fraction": job.get("fraction", 0),
        }
        if job["status"] == "done":
            body.update(
                {
                    "results": job["results"].to_dict(orient="records"),
                    "problems": job["problems"].to_dict(orient="records"),
                    "n_links": job["n_links"],
                    "ok_links": job["ok_links"],
                    "unique_isins": job["unique_isins"],
                }
            )
    return jsonify(body)


@app.get("/jobs/<job_id>/csv")
def job_csv(job_id: str):
    details = request.args.get("details") == "1"
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job["status"] != "done":
            return jsonify({"error": "CSV not ready"}), 404
        frame = job["results"]
    columns = RESULT_COLUMNS if details else ["ISIN", "Source URL"]
    data = frame[columns].to_csv(index=False).encode("utf-8-sig")
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=fundsquare_isins.csv"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
