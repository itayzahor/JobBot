"""Review page for the Gmail agent's eval labels: one row per email, with Claude's draft label
(type, company, role, matched job) pre-filled for the user to confirm or correct.

Reads qa/data/gmail_emails.json (from qa/gmail_dataset.py) and reads/writes
qa/data/gmail_labels.json. Every change is saved immediately. Runs on port 5001 so it never
collides with the dashboard on 5000.

    venv\\Scripts\\python.exe qa\\label_emails.py   ->   http://localhost:5001
"""

import json
import os
import sqlite3
import sys

from flask import Flask, jsonify, render_template_string, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from jobbot.paths import DB_PATH  # noqa: E402

EMAILS_PATH = os.path.join(ROOT, "qa", "data", "gmail_emails.json")
LABELS_PATH = os.path.join(ROOT, "qa", "data", "gmail_labels.json")

TYPES = [
    "confirmation", "rejection", "interview_invite", "home_assignment", "recruiter_outreach",
    # About a specific application but no status change (e.g. "still reviewing", "send your transcript")
    # - at most a note on the job
    "other_update",
    "unrelated",
]
# How the email relates to the jobs DB: one job, several equally likely jobs, a job applied to
# outside JobBot, or not about a specific application at all
MATCH_KINDS = ["job", "ambiguous", "not_in_db", "none"]

app = Flask(__name__)


def _load_labels() -> dict:
    if not os.path.exists(LABELS_PATH):
        return {}
    with open(LABELS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _jobs() -> list[dict]:
    """Every job an email could plausibly be about: anything applied/in process, plus any job a
    label already points at (e.g. a confirmation for a job still marked 'new')."""
    labels = _load_labels()
    referenced = {jid for lab in labels.values() for jid in lab.get("job_ids", [])}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, company, title, status FROM jobs").fetchall()
    conn.close()
    jobs = [dict(r) for r in rows if r["status"] in ("applied", "process") or r["id"] in referenced]
    return sorted(jobs, key=lambda j: (j["company"].lower(), j["title"]))


@app.route("/")
def index():
    with open(EMAILS_PATH, encoding="utf-8") as f:
        emails = json.load(f)
    labels = _load_labels()
    only = request.args.get("show", "all")
    if only == "unverified":
        emails = [e for e in emails if not labels.get(e["id"], {}).get("verified")]
    elif only == "applications":
        emails = [e for e in emails if labels.get(e["id"], {}).get("type", "unrelated") != "unrelated"]
    verified = sum(1 for lab in labels.values() if lab.get("verified"))
    return render_template_string(
        PAGE, emails=emails, labels=labels, jobs=_jobs(), types=TYPES, kinds=MATCH_KINDS,
        verified=verified, total=len(labels), show=only,
    )


@app.route("/label/<email_id>", methods=["POST"])
def save_label(email_id):
    labels = _load_labels()
    labels[email_id] = {**labels.get(email_id, {}), **request.get_json()}
    with open(LABELS_PATH, "w", encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False, indent=1)
    return jsonify(ok=True)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Gmail labels</title>
<style>
 body{font-family:system-ui,sans-serif;margin:16px;background:#f6f7f9;color:#222}
 .row{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin-bottom:8px}
 .row.ok{border-left:5px solid #2e9e5b} .row.todo{border-left:5px solid #d9a400}
 .meta{font-size:12px;color:#666} .subj{font-weight:600;margin:2px 0 6px}
 .fields{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px}
 select,input{font-size:13px;padding:3px} input.txt{width:150px} select.job{max-width:420px}
 details pre{white-space:pre-wrap;font-size:12px;max-height:300px;overflow:auto;background:#fafafa;padding:8px}
 .cand{font-size:12px;color:#8a5a00} nav a{margin-right:12px} .saved{color:#2e9e5b;font-size:12px}
</style></head><body>
<h2>Gmail eval labels - {{verified}} / {{total}} verified</h2>
<nav>Show: <a href="?show=all">all</a><a href="?show=applications">application-related</a><a href="?show=unverified">unverified</a> (now: {{show}})</nav><br>
{% for e in emails %}{% set lab = labels.get(e.id, {}) %}
<div class="row {{ 'ok' if lab.verified else 'todo' }}" data-id="{{e.id}}">
 <div class="meta">{{e.received[:10]}} · {{e.sender}}</div>
 <div class="subj">{{e.subject}}</div>
 <div class="fields">
  <select name="type">{% for t in types %}<option {{'selected' if lab.type==t}}>{{t}}</option>{% endfor %}</select>
  <input class="txt" name="company" placeholder="company" value="{{lab.company or ''}}">
  <input class="txt" name="role" placeholder="role" value="{{lab.role or ''}}">
  <select name="match">{% for k in kinds %}<option {{'selected' if lab.match==k}}>{{k}}</option>{% endfor %}</select>
  <select class="job" name="job"><option value="">- job -</option>
   {% for j in jobs %}<option value="{{j.id}}" {{'selected' if lab.job_ids and lab.job_ids[0]==j.id}}>{{j.company}} - {{j.title}} [{{j.status}}]</option>{% endfor %}</select>
  <label><input type="checkbox" name="verified" {{'checked' if lab.verified}}> correct</label>
  <span class="saved"></span>
 </div>
 {% if lab.match=='ambiguous' and lab.job_ids %}<div class="cand">candidates: {% for jid in lab.job_ids %}{% for j in jobs if j.id==jid %}{{j.title}}; {% endfor %}{% endfor %}</div>{% endif %}
 {% if lab.note %}<div class="cand">draft note: {{lab.note}}</div>{% endif %}
 <details><summary>body</summary><pre>{{e.body}}</pre></details>
</div>{% endfor %}
<script>
document.querySelectorAll('.row').forEach(row => {
  row.querySelectorAll('select,input').forEach(el => el.addEventListener('change', () => {
    const v = n => row.querySelector('[name='+n+']');
    const job = v('job').value, match = v('match').value;
    const body = {type: v('type').value, company: v('company').value, role: v('role').value,
                  match: match, verified: v('verified').checked};
    // Picking a single job replaces the candidate list; ambiguous keeps the drafted candidates
    if (match === 'job') body.job_ids = job ? [job] : [];
    if (match === 'not_in_db' || match === 'none') body.job_ids = [];
    fetch('/label/' + row.dataset.id, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
      .then(() => { row.className = 'row ' + (body.verified ? 'ok' : 'todo'); row.querySelector('.saved').textContent = 'saved'; });
  }));
});
</script></body></html>"""


if __name__ == "__main__":
    app.run(port=5001, debug=False)
