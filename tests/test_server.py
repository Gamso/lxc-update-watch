import json


def post_report(web, token_headers, report):
    response = web.post("/api/report", json=report, headers=token_headers)
    assert response.status_code == 200, response.get_json()


# ---------------------------------------------------------------- authentication and reports

def test_report_requires_the_token(web, sample_report):
    assert web.post("/api/report", json=sample_report).status_code == 401
    assert web.post("/api/report", json=sample_report, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_invalid_reports_are_rejected(web, token_headers):
    assert web.post("/api/report", json={"x": 1}, headers=token_headers).status_code == 400
    bad_date = {"checked_at": "not a date", "containers": []}
    response = web.post("/api/report", json=bad_date, headers=token_headers)
    assert response.status_code == 400 and response.get_json()["code"] == "invalid_date"


def test_page_without_report(web):
    assert "No report received yet" in web.get("/", headers={"Accept-Language": "en"}).get_data(as_text=True)


def test_report_is_stored_and_served(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    assert web.get("/api/state").get_json()["host"] == "pve"
    assert web.get("/healthz").get_data(as_text=True) == "ok"


# ---------------------------------------------------------------- page and languages

def test_page_defaults_to_english_and_follows_the_browser(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    english = web.get("/").get_data(as_text=True)
    assert '<html lang="en">' in english and "Check now" in english and "System packages" in english
    french = web.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9"}).get_data(as_text=True)
    assert '<html lang="fr">' in french and "Vérifier maintenant" in french and "Paquets système" in french


def test_language_selector_lists_every_language(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    english = web.get("/").get_data(as_text=True)
    assert '<option value="en" selected>English</option>' in english and '<option value="fr">Français</option>' in english
    french = web.get("/", headers={"Accept-Language": "fr"}).get_data(as_text=True)
    assert '<option value="fr" selected>Français</option>' in french
    assert "<select" in web.get("/").get_data(as_text=True)  # also shown before the first report


def test_language_switch_sets_a_cookie(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    response = web.get("/?lang=fr")
    assert response.status_code == 302 and "lang=fr" in response.headers["Set-Cookie"]
    assert "Vérifier maintenant" in web.get("/").get_data(as_text=True)  # the test client keeps the cookie
    web.get("/?lang=en")
    assert "Check now" in web.get("/").get_data(as_text=True)
    assert web.get("/?lang=xx").status_code == 200  # unsupported language: ignored, page shown


def test_collector_error_codes_are_shown_translated(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    english = web.get("/").get_data(as_text=True)
    assert "Paperless: latest version unavailable" in english
    assert "registry unreachable or private" in english
    web.get("/?lang=fr")
    french = web.get("/").get_data(as_text=True)
    assert "Paperless : dernière version indisponible" in french
    assert "registre injoignable ou privé" in french


def test_local_docker_images_are_not_counted(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    page = web.get("/").get_data(as_text=True)
    assert "1/3 Docker image to update" in page and "mybuild:dev" not in page


def test_versions_are_shown_and_a_mismatch_is_flagged(web, token_headers, sample_report):
    sample_report["collector_version"] = "0.0.1"
    post_report(web, token_headers, sample_report)
    page = web.get("/").get_data(as_text=True)
    assert "collector v0.0.1" in page and "redeploy with deploy.ps1" in page


def test_pending_rows_come_first_with_details(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    page = web.get("/").get_data(as_text=True)
    assert page.index("vaultwarden") < page.index("alpine-box") < page.index("idle")
    assert "Components: vaultwarden 1.32.5 → 1.33.0, vaultwarden_webvault 2026.7.0" in page


# ---------------------------------------------------------------- refresh requests

def test_refresh_request_lifecycle(web, token_headers, sample_report):
    assert web.post("/api/refresh").status_code == 202
    assert web.get("/api/jobs/pending", headers=token_headers).get_json()["refresh"] is True
    assert web.get("/api/refresh/pending").status_code == 401
    post_report(web, token_headers, {**sample_report, "started_at": "2999-01-01T00:00:00+00:00"})
    assert web.get("/api/jobs/pending", headers=token_headers).get_json()["refresh"] is False


def test_a_request_made_during_a_collection_stays_pending(web, token_headers, sample_report):
    web.post("/api/refresh")
    post_report(web, token_headers, {**sample_report, "started_at": "2000-01-01T00:00:00+00:00"})
    assert web.get("/api/jobs/pending", headers=token_headers).get_json()["refresh"] is True


# ---------------------------------------------------------------- jobs

def test_actions_need_the_admin_password(web, token_headers, sample_report):
    post_report(web, token_headers, sample_report)
    assert web.post("/api/actions", json={"action": "upgrade_os"}).status_code == 401
    response = web.post("/api/actions", json={"action": "upgrade_os"}, headers={"X-Admin-Password": "wrong"})
    assert response.status_code == 401 and response.get_json()["code"] == "bad_password"


def test_actions_are_disabled_without_a_password(web, token_headers, sample_report, monkeypatch):
    import app as web_app
    post_report(web, token_headers, sample_report)
    monkeypatch.setattr(web_app, "ADMIN_PASSWORD", "")
    response = web.post("/api/actions", json={"action": "upgrade_os"}, headers={"X-Admin-Password": "x"})
    assert response.status_code == 503 and response.get_json()["code"] == "password_not_set"
    assert 'id="act-os"' not in web.get("/").get_data(as_text=True)


def test_invalid_actions(web, token_headers, admin_headers, sample_report):
    assert web.post("/api/actions", json={"action": "upgrade_os"}, headers=admin_headers).get_json()["code"] == "no_report"
    post_report(web, token_headers, sample_report)
    assert web.post("/api/actions", json={"action": "rm -rf"}, headers=admin_headers).get_json()["code"] == "unknown_action"


def test_upgrade_and_app_update_target_the_right_containers(web, token_headers, admin_headers, sample_report):
    post_report(web, token_headers, sample_report)
    upgrade = web.post("/api/actions", json={"action": "upgrade_os"}, headers=admin_headers)
    assert upgrade.status_code == 202 and upgrade.get_json()["ctids"] == [100, 102]
    pending = web.get("/api/jobs/pending", headers=token_headers).get_json()["job"]
    web.post(f"/api/jobs/{pending['id']}/result", json={"results": []}, headers=token_headers)
    apps = web.post("/api/actions", json={"action": "update_apps"}, headers=admin_headers)
    assert apps.get_json()["ctids"] == [100]  # 101 has no helper-script command, 102 has no application update


def test_backup_needs_a_configured_storage(web, token_headers, admin_headers, sample_report):
    sample_report["backup_storage"] = None
    post_report(web, token_headers, sample_report)
    response = web.post("/api/actions", json={"action": "update_apps", "backup": True}, headers=admin_headers)
    assert response.status_code == 400 and response.get_json()["code"] == "backup_not_configured"
    assert "backup_storage</code> is not set" in web.get("/").get_data(as_text=True)


def test_only_one_job_at_a_time(web, token_headers, admin_headers, sample_report):
    post_report(web, token_headers, sample_report)
    assert web.post("/api/actions", json={"action": "upgrade_os"}, headers=admin_headers).status_code == 202
    again = web.post("/api/actions", json={"action": "update_pve_scripts"}, headers=admin_headers)
    assert again.status_code == 409 and again.get_json()["code"] == "job_running"


def test_full_job_cycle_with_live_log(web, token_headers, admin_headers, sample_report):
    post_report(web, token_headers, sample_report)
    job = web.post("/api/actions", json={"action": "update_apps", "backup": True}, headers=admin_headers).get_json()
    assert job["backup"] is True
    pending = web.get("/api/jobs/pending", headers=token_headers).get_json()["job"]
    assert pending["id"] == job["id"]
    assert web.post(f"/api/jobs/{job['id']}/start", headers=token_headers).status_code == 200
    assert web.get("/api/jobs/pending", headers=token_headers).get_json()["job"] is None  # already started

    log_url = f"/api/jobs/{job['id']}/log"
    web.post(log_url, json={"text": "[CT 100] \x1b[32m[OK]\x1b[0m start\n"}, headers=token_headers)
    first = web.get(log_url).get_json()
    assert "[OK]" in first["text"] and first["status"] == "running"
    web.post(log_url, json={"text": "second chunk\n"}, headers=token_headers)
    assert web.get(f"{log_url}?offset={first['offset']}").get_json()["text"] == "second chunk\n"

    results = [{"label": "CT 100 vaultwarden", "ok": True, "message": "done"},
               {"label": "CT 102", "ok": False, "message": "boom"}]
    web.post(f"/api/jobs/{job['id']}/result", json={"results": results}, headers=token_headers)
    page = web.get("/").get_data(as_text=True)
    assert "Application update (with backup)" in page and "finished with errors" in page and "boom" in page
    assert web.get("/api/status").get_json()["job"] == "done"
    assert web.post("/api/actions", json={"action": "update_pve_scripts"}, headers=admin_headers).status_code == 202


def test_log_is_capped(web, token_headers, admin_headers, sample_report, monkeypatch):
    import app as web_app
    monkeypatch.setattr(web_app, "LOG_MAX_BYTES", 100)
    post_report(web, token_headers, sample_report)
    job = web.post("/api/actions", json={"action": "upgrade_os"}, headers=admin_headers).get_json()
    for _ in range(5):
        web.post(f"/api/jobs/{job['id']}/log", json={"text": "x" * 60}, headers=token_headers)
    text = web.get(f"/api/jobs/{job['id']}/log").get_json()["text"]
    assert "output truncated" in text and len(text) < 300


def test_unknown_job_is_a_404(web, token_headers):
    assert web.post("/api/jobs/nope/start", headers=token_headers).status_code == 404
    assert web.get("/api/jobs/nope/log").status_code == 404


def test_status_exposes_the_version(web):
    status = json.loads(web.get("/api/status").get_data(as_text=True))
    assert status["version"]
