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


# ---------------------------------------------------------------- console per container

def test_finish_marker_is_recognizable_and_colored():
    shipper = Shipper()
    c.finish(shipper, 101, True)
    c.finish(shipper, 122, False, "boom")
    assert "[CT 101] \x1b[32;1m== finished: ok ==\x1b[0m\n" in shipper.text
    assert "[CT 122] boom\n" in shipper.text and "[CT 122] \x1b[31;1m== finished: failed ==\x1b[0m\n" in shipper.text


def test_banner_art_is_dropped_but_text_is_kept():
    art = r"print(r'  / /___/   / /___  _  __'); print(r' /_____/_/|_\____/  \____/ .___/'); print('Selected containers: 101')"
    shipper = Shipper()
    c.stream_process(python_command(art), "[CT 101] ", shipper, 20)
    assert shipper.text == "[CT 101] Selected containers: 101\x1b[0m\n"


def test_system_update_ends_each_container_with_a_marker(monkeypatch):
    monkeypatch.setattr(c, "stream_process", lambda *a, **k: (0, "done", False))
    shipper = Shipper()
    assert c.upgrade_one(101, "app", shipper)["ok"] is True
    assert "== finished: ok ==" in shipper.text
    monkeypatch.setattr(c, "stream_process", lambda *a, **k: (124, "", False))
    shipper = Shipper()
    assert c.upgrade_one(101, "app", shipper)["ok"] is False
    assert "timed out after 30 minutes" in shipper.text and "== finished: failed ==" in shipper.text


def test_skipped_containers_are_closed_in_the_console(monkeypatch):
    monkeypatch.setattr(c, "list_containers", lambda: [("7", "running", "other")])
    shipper = Shipper()
    results = c.upgrade_containers([5], {"upgrade_workers": 1}, shipper)
    assert results[0]["ok"] is False
    assert "[CT 5] container stopped or not found" in shipper.text and "[CT 5] \x1b[31;1m== finished: failed" in shipper.text


def test_application_update_closes_every_container(monkeypatch):
    monkeypatch.setattr(c, "cached_fetch", lambda *a, **k: "echo script")
    monkeypatch.setattr(c, "list_containers", lambda: [("100", "running", "a"), ("102", "running", "b")])
    monkeypatch.setattr(c, "pct_exec", lambda ctid, script, timeout=120: (0 if int(ctid) == 100 else 1, ""))
    monkeypatch.setattr(c, "stream_process", lambda *a, **k: (0, "updated", False))
    shipper = Shipper()
    results = c.update_apps({"ctids": [100, 102], "backup": False}, {"app_update_workers": 1}, shipper)
    assert [r["ok"] for r in results] == [True, False]
    assert "[CT 100] \x1b[32;1m== finished: ok" in shipper.text          # updated
    assert "[CT 102] stopped, or no update command" in shipper.text and "[CT 102] \x1b[31;1m== finished: failed" in shipper.text


# ---------------------------------------------------------------- Docker stacks

INVENTORY = "\n".join([
    "web|/opt/web|/opt/web/compose.yml|/web-app-1|nginx:latest",
    "web|/opt/web|/opt/web/compose.yml|/web-db-1|postgres:16",
    "|||/manual|redis:7",
    "mail|/srv/mail|/srv/mail/a.yml,/srv/mail/b.yml|/mail-1|ghcr.io/o/mail:latest",
])


def test_inventory_splits_compose_stacks_from_manual_containers():
    stacks, unmanaged = c.parse_docker_inventory(INVENTORY)
    assert [s["project"] for s in stacks] == ["web", "mail"]
    assert stacks[0]["containers"] == ["web-app-1", "web-db-1"]
    assert stacks[1]["files"] == "/srv/mail/a.yml,/srv/mail/b.yml"
    assert unmanaged == ["manual (redis:7)"]
    assert c.parse_docker_inventory("NODOCKER") is None
    assert c.parse_docker_inventory("") == ([], [])


def test_compose_script_pulls_then_recreates_and_quotes_paths():
    stack = {"project": "my app", "workdir": "/opt/my app", "files": "/opt/my app/a.yml,/opt/my app/b.yml"}
    script = c.compose_script(stack)
    assert script.index(" pull ") < script.index(" up -d")
    assert "-p 'my app'" in script and "--project-directory '/opt/my app'" in script
    assert "-f '/opt/my app/a.yml' -f '/opt/my app/b.yml'" in script
    assert "compose file not found" in script and "docker-compose" in script  # a missing file is reported, v1 is a fallback


def docker_env(monkeypatch, inventory=INVENTORY, fail=()):
    """Fake the container: its Docker inventory, and every command run through stream_process."""
    commands = []
    monkeypatch.setattr(c, "pct_exec", lambda ctid, script, timeout=120: (0, inventory))

    def fake_stream(cmd, prefix, shipper, timeout, env=None):
        commands.append(cmd)
        shipper.write(prefix + "output\n")
        code = 1 if any(word in " ".join(cmd) for word in fail) else 0
        return code, ("Total reclaimed space: 1.2GB" if "prune" in cmd else "ok"), False

    monkeypatch.setattr(c, "stream_process", fake_stream)
    return commands


def run_docker(**job):
    shipper = Shipper()
    result = c.update_docker_one(101, "app", {"ctids": [101], **job}, {"backup_storage": "backup"}, shipper)
    return result, shipper.text


def test_docker_update_updates_every_stack_then_removes_old_images(monkeypatch):
    commands = docker_env(monkeypatch)
    result, text = run_docker(cleanup=True)
    assert result["ok"] is True and "2 stack(s) updated, Total reclaimed space: 1.2GB" in result["message"]
    assert "1 container(s) not managed by Compose" in result["message"]
    assert len(commands) == 3 and commands[2][-3:] == ["image", "prune", "-f"]    # the cleanup comes last
    assert "update them by hand: manual (redis:7)" in text and "== stack web (web-app-1, web-db-1) ==" in text
    assert "== finished: ok ==" in text


def test_docker_update_keeps_old_images_when_cleanup_is_off(monkeypatch):
    commands = docker_env(monkeypatch)
    result, _ = run_docker(cleanup=False)
    assert result["ok"] is True and len(commands) == 2 and not any("prune" in cmd for cmd in commands)


def test_a_failed_stack_keeps_the_old_images(monkeypatch):
    commands = docker_env(monkeypatch, fail=("/srv/mail",))
    result, text = run_docker(cleanup=True)
    assert result["ok"] is False and result["message"] == "failed: mail"
    assert not any("prune" in cmd for cmd in commands)                            # nothing is deleted after a failure
    assert "stack mail failed" in text and "== finished: failed ==" in text and "old images kept" in text


def test_backup_runs_first_and_a_failed_backup_stops_everything(monkeypatch):
    commands = docker_env(monkeypatch)
    result, _ = run_docker(backup=True)
    assert commands[0][:2] == ["vzdump", "101"] and "backup" in commands[0] and result["ok"] is True
    commands = docker_env(monkeypatch, fail=("vzdump",))
    result, text = run_docker(backup=True, cleanup=True)
    assert result["ok"] is False and "backup failed" in result["message"]
    assert len(commands) == 1 and "== finished: failed ==" in text                # no stack was touched


def test_docker_update_reports_what_it_cannot_do(monkeypatch):
    docker_env(monkeypatch, inventory="NODOCKER")
    assert "not installed" in run_docker()[0]["message"]
    docker_env(monkeypatch, inventory="|||/manual|redis:7")
    result, text = run_docker()
    assert result["ok"] is False and "no running Docker Compose stack" in result["message"] and "redis:7" in text


def test_docker_update_of_several_containers(monkeypatch):
    docker_env(monkeypatch)
    monkeypatch.setattr(c, "list_containers", lambda: [("101", "running", "a"), ("102", "stopped", "b")])
    shipper = Shipper()
    results = c.update_docker({"ctids": [101, 102], "cleanup": False}, {"docker_update_workers": 1}, shipper)
    assert [(r["label"], r["ok"]) for r in results] == [("CT 101 a", True), ("CT 102", False)]
    assert "[CT 102] container stopped or not found" in shipper.text
    nostorage = c.update_docker({"ctids": [101], "backup": True}, {}, Shipper())
    assert nostorage[0]["label"] == "Backup" and nostorage[0]["ok"] is False
