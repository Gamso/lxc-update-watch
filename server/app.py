"""Web service: receives the collector's reports and shows which LXC containers can be updated."""
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from i18n import COOKIE, LANGUAGE_NAMES, SUPPORTED, Translator, pick_language

STATE_FILE = Path(os.environ.get("LXC_WATCH_DATA", "data")) / "state.json"
REFRESH_FILE = STATE_FILE.with_name("refresh.json")
JOB_FILE = STATE_FILE.with_name("job.json")
LOG_FILE = STATE_FILE.with_name("job.log")
PROGRESS_FILE = STATE_FILE.with_name("progress.json")
TOKEN = os.environ.get("LXC_WATCH_TOKEN", "")
JOB_ACTIONS = {"upgrade_os", "update_pve_scripts", "update_apps"}
LOG_MAX_BYTES = 1_000_000
PROGRESS_MAX_AGE_S = 600       # a collection that sent nothing for this long is considered dead
CHECK_STEPS = ("system", "app", "docker")
JOB_MAX_AGE_H = 3
STALE_HOURS = 36
UPGRADABLE_OS = ("debian", "ubuntu", "alpine")
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600


def read_version():
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


VERSION = read_version()

app = Flask(__name__)


# ---------------------------------------------------------------- storage

def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_state():
    return read_json(STATE_FILE)


def load_job():
    return read_json(JOB_FILE)


def refresh_requested_at():
    """Time of the pending refresh request, or None."""
    try:
        return datetime.fromisoformat(read_json(REFRESH_FILE)["requested_at"])
    except (TypeError, ValueError, KeyError):
        return None


def request_refresh():
    if refresh_requested_at() is None:
        write_json(REFRESH_FILE, {"requested_at": now_iso()})


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- authentication and errors

def api_error(status, code, message):
    """Abort with a JSON body: `message` is for humans and logs, `code` lets the page show a translated text."""
    abort(make_response(jsonify(error=message, code=code), status))


def same_secret(given, expected):
    return hmac.compare_digest(given.encode(), expected.encode())


def require_token():
    if not TOKEN:
        api_error(503, "token_not_set", "LXC_WATCH_TOKEN is not set")
    if not same_secret(request.headers.get("Authorization", ""), f"Bearer {TOKEN}"):
        api_error(401, "unauthorized", "invalid token")


@app.errorhandler(HTTPException)
def json_errors(error):
    if error.response is not None:
        return error.response
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), error.code
    return error


# ---------------------------------------------------------------- jobs

def job_is_active(job):
    """A pending or running job blocks new requests, unless it is older than JOB_MAX_AGE_H (a stuck job)."""
    if not job or job["status"] not in ("pending", "running"):
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(job["requested_at"])
    return age.total_seconds() < JOB_MAX_AGE_H * 3600


def app_updatable_containers(state):
    """Containers whose application has an update AND that have the `update` command of a helper script."""
    return [ct for ct in state["containers"]
            if ct["status"] == "running" and ct.get("helper_script") and (ct.get("app") or {}).get("update")]


def upgradable_containers(state):
    return [ct for ct in state["containers"]
            if ct["status"] == "running" and ct.get("os") in UPGRADABLE_OS and (ct.get("os_updates") or 0) > 0]


def job_for_collector(job_id):
    job = load_job()
    if not job or job["id"] != job_id:
        api_error(404, "unknown_job", "unknown job")
    return job


# ---------------------------------------------------------------- page model

def system_badge(ct, tr):
    if ct["status"] != "running":
        return tr("b_stopped"), "n"
    n = ct.get("os_updates")
    if n is None:
        return tr("b_unknown"), "n"
    if n == 0:
        return tr("b_system_ok"), "s"
    return tr.plural("b_packages", n), "w"


def pending_flags(ct):
    """(system packages pending, application update pending, Docker images with an update) of a container."""
    images = [i for i in ct.get("docker") or [] if not i.get("local")]
    return (ct.get("os_updates") or 0) > 0, bool((ct.get("app") or {}).get("update")), [i for i in images if i.get("update")]


def app_badges(ct, tr, final=True):
    """Badges of the application column: the tracked application and/or the Docker images.

    With final=False (a check still running) a container without anything to show yet gets no badge,
    instead of the "not tracked" placeholder."""
    if ct["status"] != "running":
        return []
    badges = []
    app_info = ct.get("app")
    if app_info:
        if "error" in app_info:
            badges.append((tr("b_app_error", name=app_info["name"], error=tr.error(app_info["error"])), "n"))
        elif app_info["update"]:
            badges.append((f"{app_info['name']} {app_info['installed']} → {app_info['latest']}", "w"))
        else:
            badges.append((f"{app_info['name']} {app_info['installed']}", "s"))
    all_images = ct.get("docker") or []
    images = [i for i in all_images if not i.get("local")]
    if all_images and not images:
        badges.append((tr("b_docker_local"), "n"))
    if images:
        outdated = [i for i in images if i.get("update")]
        unchecked = [i for i in images if "error" in i]
        if outdated:
            badges.append((tr.plural("b_docker_outdated", len(outdated), total=len(images)), "w"))
        elif unchecked:
            badges.append((tr.plural("b_docker_unchecked", len(unchecked), total=len(images)), "n"))
        else:
            badges.append((tr.plural("b_docker_ok", len(images)), "s"))
    return badges or ([(tr("b_app_untracked"), "n")] if final else [])


def build_view(state, tr):
    rows = []
    for ct in state["containers"]:
        app_info = ct.get("app") or {}
        images = [i for i in ct.get("docker") or [] if not i.get("local")]
        sys_text, sys_kind = system_badge(ct, tr)
        sys_pending, app_pending, docker_pending = pending_flags(ct)
        rows.append({
            **ct,
            "packages": ct.get("packages") or [],
            "sys_text": sys_text, "sys_kind": sys_kind,
            "app_badges": app_badges(ct, tr),
            "pending": sys_pending or app_pending or bool(docker_pending),
            "sys_pending": sys_pending, "app_pending": app_pending,
            "docker_pending": docker_pending,
            "docker_unchecked": [{"image": i["image"], "error": tr.error(i["error"])} for i in images if "error" in i],
            "update_cmd": app_info.get("update_cmd") or ("update" if ct.get("helper_script") else None),
        })
    pending = [r for r in rows if r["pending"]]
    others = [r for r in rows if not r["pending"]]
    running = [r for r in rows if r["status"] == "running"]
    checked = datetime.fromisoformat(state["checked_at"])
    age_h = (datetime.now(timezone.utc) - checked).total_seconds() / 3600
    job = load_job()
    collector_version = state.get("collector_version")
    return {
        "host": state.get("host", ""),
        "checked_raw": state["checked_at"],
        "checked_at": checked.astimezone().strftime(tr("date_fmt")),
        "stale": age_h > STALE_HOURS,
        "stale_hours": STALE_HOURS,
        "refreshing": refresh_requested_at() is not None,
        "upgradable": [{"ctid": ct["ctid"], "name": ct["name"]} for ct in upgradable_containers(state)],
        "app_updatable": [{"ctid": ct["ctid"], "name": ct["name"]} for ct in app_updatable_containers(state)],
        "backup_storage": state.get("backup_storage"),
        "job": job,
        "job_active": job_is_active(job),
        "pending": pending,
        "others": others,
        "total": len(running),
        "n_pending": len(pending),
        "n_packages": sum(r.get("os_updates") or 0 for r in rows),
        "n_apps": sum(r["app_pending"] + len(r["docker_pending"]) for r in rows),
        "n_untracked": sum(1 for r in running if not r.get("app") and not r.get("docker")),
        "collector_version": collector_version,
        "version_mismatch": bool(collector_version) and collector_version != VERSION,
    }


def translator():
    return Translator(pick_language(request.cookies.get(COOKIE), request.accept_languages))


# ---------------------------------------------------------------- pages

@app.get("/")
def index():
    chosen = request.args.get("lang")
    if chosen in SUPPORTED:  # language switcher: remember the choice, then show the clean URL
        response = redirect(url_for("index"))
        response.set_cookie(COOKIE, chosen, max_age=LANG_COOKIE_MAX_AGE, samesite="Lax")
        return response
    tr = translator()
    state = load_state()
    return render_template("index.html", view=build_view(state, tr) if state else None, t=tr, lang=tr.lang,
                           languages=[(code, LANGUAGE_NAMES[code]) for code in SUPPORTED],
                           i18n=tr.browser_strings(), version=VERSION)


@app.get("/healthz")
def healthz():
    return "ok"


# ---------------------------------------------------------------- API: reports and refresh

@app.get("/api/state")
def api_state():
    state = load_state()
    if not state:
        api_error(404, "no_report", "no report received yet")
    return jsonify(state)


@app.get("/api/status")
def api_status():
    state = load_state()
    job = load_job()
    return jsonify(checked_at=state["checked_at"] if state else None,
                   refreshing=refresh_requested_at() is not None,
                   job=job["status"] if job else None, job_id=job["id"] if job else None, version=VERSION)


@app.post("/api/report")
def api_report():
    require_token()
    report = request.get_json(silent=True)
    if not isinstance(report, dict) or not isinstance(report.get("containers"), list) or "checked_at" not in report:
        api_error(400, "invalid_report", "invalid report")
    try:
        datetime.fromisoformat(report["checked_at"])
        started = datetime.fromisoformat(report.get("started_at", report["checked_at"]))
    except ValueError:
        api_error(400, "invalid_date", "invalid date")
    write_json(STATE_FILE, report)
    PROGRESS_FILE.unlink(missing_ok=True)  # the collection is complete: the live view is over
    # A request made while the collector was running is not covered by this report and stays pending.
    requested = refresh_requested_at()
    if requested and requested <= started:
        REFRESH_FILE.unlink(missing_ok=True)
    return jsonify(ok=True, containers=len(report["containers"]))


def load_progress():
    """Progress of the collection in progress, or None (no collection, or it stopped sending events)."""
    progress = read_json(PROGRESS_FILE)
    if not progress:
        return None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(progress["updated_at"])
    return progress if age.total_seconds() < PROGRESS_MAX_AGE_S else None


def apply_progress_events(progress, events):
    """Merge the collector's events: "start" lists the containers, "step" reports a step of one container."""
    for event in events:
        try:
            if event.get("type") == "start":
                progress["containers"] = {}
                for ct in event.get("containers", []):
                    running = ct.get("status") == "running"
                    progress["containers"][str(ct["ctid"])] = {
                        "ctid": ct["ctid"], "name": ct.get("name", ""), "status": ct.get("status", ""),
                        "steps": {step: "queued" if running else "done" for step in CHECK_STEPS}, "data": {}}
            elif event.get("type") == "step":
                entry = progress["containers"].get(str(event.get("ctid")))
                if entry and event.get("step") in CHECK_STEPS and event.get("state") in ("running", "done"):
                    entry["steps"][event["step"]] = event["state"]
                    if isinstance(event.get("data"), dict):
                        entry["data"].update(event["data"])
        except (AttributeError, KeyError, TypeError):
            continue  # a malformed event must not break the others


@app.post("/api/progress")
def api_progress_post():
    require_token()
    body = request.get_json(silent=True) or {}
    run, events = str(body.get("run", "")), body.get("events")
    if not run or not isinstance(events, list):
        api_error(400, "invalid_progress", "invalid progress")
    state = load_state()
    if state and state.get("started_at") == run:
        return jsonify(ok=True, ignored=True)  # this collection is already committed: late events are dropped
    progress = read_json(PROGRESS_FILE)
    if not progress or progress.get("run") != run:
        progress = {"run": run, "containers": {}}
    apply_progress_events(progress, events)
    progress["updated_at"] = now_iso()
    write_json(PROGRESS_FILE, progress)
    return jsonify(ok=True)


@app.get("/api/progress")
def api_progress_get():
    """Live view of a collection: step of every container, with the badges already rendered and translated."""
    tr = translator()
    state = load_state()
    result = {"checked_at": state["checked_at"] if state else None}
    progress = load_progress()
    if not progress:
        return jsonify(active=False, **result)
    containers, finished_count = {}, 0
    for key, entry in progress["containers"].items():
        steps, ct = entry["steps"], {"status": entry["status"], **entry["data"]}
        finished = all(step == "done" for step in steps.values())
        finished_count += finished
        item = {"steps": steps, "status": entry["status"]}
        if entry["status"] == "running":
            if steps["system"] == "done":
                text, kind = system_badge(ct, tr)
                item["sys"] = {"text": text, "kind": kind}
            if steps["app"] == "done":
                item["apps"] = [{"text": text, "kind": kind}
                                for text, kind in app_badges(ct, tr, final=steps["docker"] == "done")]
            if finished:
                sys_pending, app_pending, docker_pending = pending_flags(ct)
                item["pending"] = bool(sys_pending or app_pending or docker_pending)
        containers[key] = item
    return jsonify(active=True, run=progress["run"], done=finished_count, total=len(containers),
                   containers=containers, **result)


@app.post("/api/refresh")
def api_refresh():
    request_refresh()
    return jsonify(refreshing=True), 202


@app.get("/api/refresh/pending")
def api_refresh_pending():
    require_token()
    return jsonify(pending=refresh_requested_at() is not None)


# ---------------------------------------------------------------- API: jobs

@app.post("/api/actions")
def api_actions():
    # No password: access control belongs to the network or a reverse proxy. The body must be JSON, which a
    # cross-site form cannot send (and a cross-site fetch would need a CORS preflight this service never answers).
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in JOB_ACTIONS:
        api_error(400, "unknown_action", "unknown action")
    state = load_state()
    if not state:
        api_error(400, "no_report", "no report received yet")
    if job_is_active(load_job()):
        api_error(409, "job_running", "a job is already running")
    ctids = []
    if action == "upgrade_os":
        ctids = [ct["ctid"] for ct in upgradable_containers(state)]
    elif action == "update_apps":
        ctids = [ct["ctid"] for ct in app_updatable_containers(state)]
    if action in ("upgrade_os", "update_apps") and not ctids:
        api_error(400, "no_containers", "no container to update")
    backup = bool(body.get("backup")) and action == "update_apps"
    if backup and not state.get("backup_storage"):
        api_error(400, "backup_not_configured", "backup_storage is not set in the collector config")
    job = {"id": uuid.uuid4().hex, "action": action, "ctids": ctids, "status": "pending", "backup": backup,
           "requested_at": now_iso(), "results": []}
    LOG_FILE.unlink(missing_ok=True)
    write_json(JOB_FILE, job)
    if action in ("upgrade_os", "update_apps"):
        request_refresh()  # the list must be checked again once the updates are installed
    return jsonify(job), 202


@app.get("/api/jobs/pending")
def api_jobs_pending():
    require_token()
    job = load_job()
    return jsonify(refresh=refresh_requested_at() is not None,
                   job=job if job and job["status"] == "pending" and job_is_active(job) else None)


@app.post("/api/jobs/<job_id>/start")
def api_job_start(job_id):
    require_token()
    job = job_for_collector(job_id)
    job.update(status="running", started_at=now_iso())
    write_json(JOB_FILE, job)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/result")
def api_job_result(job_id):
    require_token()
    job = job_for_collector(job_id)
    body = request.get_json(silent=True) or {}
    results = [{"label": str(r.get("label", ""))[:80], "ok": bool(r.get("ok")), "message": str(r.get("message", ""))[:300]}
               for r in body.get("results", []) if isinstance(r, dict)][:100]
    job.update(status="done", results=results, ok=all(r["ok"] for r in results), finished_at=now_iso())
    write_json(JOB_FILE, job)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/log")
def api_job_log_append(job_id):
    require_token()
    job_for_collector(job_id)
    text = str((request.get_json(silent=True) or {}).get("text", ""))
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    if size < LOG_MAX_BYTES:
        with LOG_FILE.open("a", encoding="utf-8", newline="") as f:
            f.write(text)
            if size + len(text.encode()) >= LOG_MAX_BYTES:
                f.write("\n[output truncated: size limit reached]\n")
    return jsonify(ok=True)


@app.get("/api/jobs/<job_id>/log")
def api_job_log_read(job_id):
    job = job_for_collector(job_id)
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    offset = max(request.args.get("offset", 0, type=int), 0)
    return jsonify(text=text[offset:], offset=len(text), status=job["status"])
