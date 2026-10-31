import sys

import pytest

import lxc_collect as c

SAMPLE_SCRIPT = '''
RELEASE="v8.1.0"
if check_for_gh_release "OpenCloud" "opencloud-eu/opencloud" "${RELEASE}" "tested one by one"; then
  :
fi
if check_for_gh_release "my app" "owner/repo"; then :; fi
if check_for_gh_release "prefixed" "owner/prefixed" "" "" "web-v2"; then :; fi
if check_for_gh_release "${APP}" "owner/dynamic"; then :; fi
'''


# ---------------------------------------------------------------- parsing helpers

def test_parse_image():
    assert c.parse_image("nginx") == ("docker.io", "library/nginx", "latest", "nginx")
    assert c.parse_image("user/app:2") == ("docker.io", "user/app", "2", "user/app")
    assert c.parse_image("ghcr.io/o/app:1.2")[:3] == ("ghcr.io", "o/app", "1.2")
    assert c.parse_image("localhost:5000/x")[:2] == ("localhost:5000", "x")
    assert c.parse_image("nginx@sha256:abc") is None


def test_version_helpers():
    assert c.parse_version("release-5.2.4_v2.0.15", r"release-(\d+(?:\.\d+)+)") == "5.2.4"
    assert c.parse_version("v1.37.3") == "1.37.3"
    assert c.parse_version("no version here") is None
    assert c.version_key("1.10.0") > c.version_key("1.9.9")
    assert c.strip_v("v1.2") == "1.2" and c.strip_v("verbose") == "verbose"


def test_clean_line_keeps_colors_and_collapses_spinners():
    assert c.clean_line("\x1b[33m[INFO]\x1b[0m hello\n") == "\x1b[33m[INFO]\x1b[0m hello"
    assert c.clean_line("spin 1\rspin 2\rspin 3\n") == "spin 3"
    assert c.clean_line("\x1b[?25lhidden cursor\x1b[2K\n") == "hidden cursor"
    assert c.clean_line("   \n") == ""


def test_script_components():
    components = {comp["name"]: comp for comp in c.script_components(SAMPLE_SCRIPT)}
    assert components["OpenCloud"]["pin"] == "v8.1.0"
    assert components["my app"]["pin"] == "" and components["my app"]["prefix"] == ""
    assert components["prefixed"]["prefix"] == "web-v2"
    assert "${APP}" not in components and len(components) == 3  # a name computed at run time is skipped


# ---------------------------------------------------------------- automatic detection

@pytest.fixture
def helper_script_env(monkeypatch):
    texts = {"ct/opencloud.sh": SAMPLE_SCRIPT, "tools/addon/filebrowser-quantum.sh": 'check_for_gh_release "filebrowser-quantum" "o/fb"'}
    tree = '{"tree": [{"path": "ct/opencloud.sh"}, {"path": "tools/addon/filebrowser-quantum.sh"}, {"path": "README.md"}]}'

    def fake_fetch(url, ttl, token=None):
        if "git/trees" in url:
            return tree
        return next((text for path, text in texts.items() if url.endswith(path)), None)

    monkeypatch.setattr(c, "cached_fetch", fake_fetch)
    monkeypatch.setattr(c, "release_target", lambda comp, token: comp["pin"] or {"o/fb": "v2.0.2-stable"}.get(comp["repo"]))


def fake_container(files):
    def pct_exec(ctid, script, timeout=120):
        wanted = script.split("for f in ", 1)[1].split(";", 1)[0].split()
        return 0, "\n".join(f"{path}\t{files[path]}" for path in wanted if path in files)
    return pct_exec


def test_auto_detection_uses_the_pinned_version(helper_script_env, monkeypatch):
    monkeypatch.setattr(c, "pct_exec", fake_container({"/root/.opencloud": "7.4.0"}))
    app = c.auto_app_status("119", "opencloud", {}, {})
    assert (app["name"], app["installed"], app["latest"], app["update"]) == ("OpenCloud", "7.4.0", "8.1.0", True)
    monkeypatch.setattr(c, "pct_exec", fake_container({"/root/.opencloud": "8.1.0"}))
    assert c.auto_app_status("119", "opencloud", {}, {})["update"] is False


def test_auto_detection_matches_addons_by_normalized_name(helper_script_env, monkeypatch):
    monkeypatch.setattr(c, "pct_exec", fake_container({"/root/.filebrowser-quantum": "1.5.6-stable"}))
    app = c.auto_app_status("105", "filebrowserquantum", {}, {})
    assert (app["installed"], app["latest"], app["update"]) == ("1.5.6-stable", "2.0.2-stable", True)
    assert "tools/addon/filebrowser-quantum.sh" in app["update_cmd"]


def test_auto_detection_ignores_unrelated_containers(helper_script_env, monkeypatch):
    monkeypatch.setattr(c, "pct_exec", fake_container({}))
    assert c.auto_app_status("120", "opencloud", {}, {}) is None   # script matches, nothing installed here
    assert c.auto_app_status("121", "something-else", {}, {}) is None


def test_apt_application(monkeypatch):
    packages = ["tzdata 2026e → 2026f", "cloudflared 2026.10.4 → 2026.10.7"]
    app = c.apt_app_status("103", {"name": "cloudflared", "apt_package": "cloudflared"}, packages)
    assert (app["installed"], app["latest"], app["update"]) == ("2026.10.4", "2026.10.7", True)
    monkeypatch.setattr(c, "pct_exec", lambda *a, **k: (0, ""))
    assert c.apt_app_status("103", {"apt_package": "cloudflared"}, [])["error"] == "package_not_installed"


def test_manual_application_errors_are_codes(monkeypatch):
    monkeypatch.setattr(c, "pct_exec", lambda *a, **k: (0, "garbage"))
    assert c.app_status("1", {"version_file": "/x"}, "", {})["error"] == "installed_version_not_found"
    assert c.app_status("1", {}, "", {})["error"] == "missing_version_source"


# ---------------------------------------------------------------- streaming

class Shipper:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


def python_command(code):
    return [sys.executable, "-c", code]


def test_stream_process_prefixes_lines_and_detects_errors():
    shipper = Shipper()
    code, last, saw_error = c.stream_process(
        python_command("print('one'); print('spin 1\\rspin 2'); print('[ERROR] boom')"), "[CT 7] ", shipper, 20)
    assert code == 0 and saw_error is True and last == "[ERROR] boom"
    assert "[CT 7] one" in shipper.text and "[CT 7] spin 2" in shipper.text and "spin 1" not in shipper.text


def test_stream_process_reports_the_exit_code_and_timeouts():
    assert c.stream_process(python_command("import sys; sys.exit(3)"), "", Shipper(), 20)[0] == 3
    assert c.stream_process(python_command("import time; time.sleep(30)"), "", Shipper(), 0.5)[0] == 124


def test_collector_version_comes_from_the_version_file():
    assert c.VERSION == open(c.Path(__file__).parent.parent / "VERSION", encoding="utf-8").read().strip()


# ---------------------------------------------------------------- progress reporting

class Recorder:
    def __init__(self):
        self.events = []

    def event(self, **fields):
        self.events.append(fields)


def test_collect_one_reports_every_step_in_order(monkeypatch):
    monkeypatch.setattr(c, "os_updates", lambda ctid: ("debian", 2, ["a", "b"]))
    monkeypatch.setattr(c, "pct_exec", lambda *a, **k: (0, ""))
    monkeypatch.setattr(c, "auto_app_status", lambda *a: {"name": "App", "installed": "1", "latest": "1", "update": False})
    monkeypatch.setattr(c, "docker_status", lambda *a: [{"image": "x", "update": False}])
    recorder = Recorder()
    ct = c.collect_one(("100", "running", "app"), {}, {}, recorder)
    assert [(e["step"], e["state"]) for e in recorder.events] == [
        ("system", "running"), ("system", "done"), ("app", "running"), ("app", "done"), ("docker", "running"), ("docker", "done")]
    system_done = recorder.events[1]["data"]
    assert system_done["os_updates"] == 2 and system_done["helper_script"] is True
    assert recorder.events[3]["data"]["app"]["name"] == "App" and recorder.events[5]["data"]["docker"][0]["image"] == "x"
    assert ct["os_updates"] == 2 and ct["app"]["name"] == "App"


def test_collect_one_reports_nothing_for_a_stopped_container():
    recorder = Recorder()
    assert c.collect_one(("103", "stopped", "idle"), {}, {}, recorder) == {"ctid": 103, "name": "idle", "status": "stopped"}
    assert recorder.events == []


def test_progress_reporter_sends_batches_and_retries(monkeypatch):
    sent, failures = [], [True]

    def fake_call(config, method, path, body=None, timeout=15):
        if failures:
            failures.clear()
            raise OSError("server down")
        sent.append((path, body))

    monkeypatch.setattr(c, "server_call", fake_call)
    reporter = c.ProgressReporter({}, "run-1")
    reporter.event(type="start", containers=[])
    reporter.close()          # first flush fails, the final one delivers
    reporter._flush()
    assert sent and sent[0][0] == "/api/progress" and sent[0][1]["run"] == "run-1"
    assert sent[0][1]["events"][0]["type"] == "start"


# ---------------------------------------------------------------- requests made from the page

def pending_state(monkeypatch, pending):
    ran = []
    monkeypatch.setattr(c, "server_call", lambda *a, **k: pending)
    monkeypatch.setattr(c, "run_job", lambda config, job: ran.append(job["action"]))
    return ran


def test_nothing_requested_means_nothing_to_do(monkeypatch):
    ran = pending_state(monkeypatch, {"refresh": False, "job": None})
    assert c.handle_requests({}) is False and ran == []


def test_a_check_request_runs_a_collection(monkeypatch):
    ran = pending_state(monkeypatch, {"refresh": True, "job": None})
    assert c.handle_requests({}) is True and ran == []


def test_refreshing_the_pve_scripts_does_not_trigger_a_check(monkeypatch):
    ran = pending_state(monkeypatch, {"refresh": False, "job": {"action": "update_pve_scripts"}})
    assert c.handle_requests({}) is False and ran == ["update_pve_scripts"]


def test_updates_are_followed_by_a_check(monkeypatch):
    # the service asks for the check itself when it records a system or application update
    ran = pending_state(monkeypatch, {"refresh": True, "job": {"action": "update_apps"}})
    assert c.handle_requests({}) is True and ran == ["update_apps"]


def test_an_unreachable_service_is_not_an_error(monkeypatch):
    def down(*args, **kwargs):
        raise OSError("down")
    monkeypatch.setattr(c, "server_call", down)
    assert c.handle_requests({}) is False
