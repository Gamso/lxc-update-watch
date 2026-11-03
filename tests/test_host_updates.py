"""Updates of the Proxmox host itself: what the collector reads and how the page shows it."""
import lxc_collect as c

APT_OUTPUT = """\
NOTE: This is only a simulation!
Inst libnss3 [2:3.110-1+deb13u4] (2:3.110-1+deb13u5 Debian-Security:13/stable-security [amd64])
Inst vim-tiny [2:9.1.1230-2] (2:9.1.1230-2+deb13u1 Debian:13.1/stable [amd64])
Inst pve-manager [9.2.24] (9.2.25 Proxmox:trixie/stable [all])
Inst proxmox-kernel-6.14.11-5-pve-signed (6.14.11-5 Proxmox:trixie/stable [amd64])
Conf libnss3 (2:3.110-1+deb13u5 Debian-Security:13/stable-security [amd64])
"""


def reported(apt_output=APT_OUTPUT):
    packages = c.parse_host_upgrades(apt_output)
    return {"count": len(packages), "packages": packages, "kernel": any(c.KERNEL_RE.match(p["name"]) for p in packages)}


# ---------------------------------------------------------------- collector

def test_host_upgrades_are_read_with_their_origin():
    packages = c.parse_host_upgrades(APT_OUTPUT)
    assert [(p["origin"], p["name"]) for p in packages] == [
        ("Debian", "libnss3"), ("Debian", "vim-tiny"), ("Proxmox", "proxmox-kernel-6.14.11-5-pve-signed"),
        ("Proxmox", "pve-manager")]
    nss = packages[0]
    assert nss["current"] == "2:3.110-1+deb13u4" and nss["new"] == "2:3.110-1+deb13u5"
    assert [p["current"] for p in packages if p["name"].startswith("proxmox-kernel")] == [""]     # a new package


def test_host_updates_reports_count_and_kernel(monkeypatch):
    seen = []
    monkeypatch.setattr(c, "run", lambda cmd, timeout=120: seen.append(cmd) or (0, APT_OUTPUT))
    result = c.host_updates()
    assert result["count"] == 4 and result["kernel"] is True
    assert seen[0][-3:] == ["apt-get", "-s", "dist-upgrade"] and "LC_ALL=C" in seen[0]   # a simulation: nothing is installed
    monkeypatch.setattr(c, "run", lambda cmd, timeout=120: (0, "Reading package lists...\n0 upgraded, 0 newly installed"))
    assert c.host_updates() == {"count": 0, "packages": [], "kernel": False}


def test_host_updates_error_is_a_code_not_a_crash(monkeypatch):
    monkeypatch.setattr(c, "run", lambda cmd, timeout=120: (100, ""))
    assert c.host_updates() == {"error": "apt_failed"}


# ---------------------------------------------------------------- page

def page(web, token_headers, report, host, headers=None):
    if host is not None:
        report["host_updates"] = host
    assert web.post("/api/report", json=report, headers=token_headers).status_code == 200
    return web.get("/", headers=headers or {}).get_data(as_text=True)


def test_host_row_lists_the_packages_by_origin(web, token_headers, sample_report):
    html = page(web, token_headers, sample_report, reported())
    assert "Proxmox host" in html and "4 packages" in html and "Proxmox 2" in html and "Debian 2" in html
    assert html.index("Origin: Proxmox (2)") < html.index("Origin: Debian (2)")          # Proxmox first, as in the panel
    assert "pve-manager  9.2.24 → 9.2.25" in html and "libnss3  2:3.110-1+deb13u4 → 2:3.110-1+deb13u5" in html
    assert "needs a reboot" in html
    assert html.index("Proxmox host") < html.index("To update</h2>")


def test_host_row_when_up_to_date_or_unknown(web, token_headers, sample_report):
    html = page(web, token_headers, sample_report, {"count": 0, "packages": [], "kernel": False})
    assert "Host up to date" in html and "needs a reboot" not in html
    html = page(web, token_headers, sample_report, {"error": "apt_failed"})
    assert "Host updates unknown" in html and "apt-get failed on the host" in html


def test_host_row_is_translated_and_absent_from_old_reports(web, token_headers, sample_report):
    html = page(web, token_headers, sample_report, reported(), {"Accept-Language": "fr"})
    assert "Hôte Proxmox" in html and "Origine : Proxmox (2)" in html
    sample_report.pop("host_updates")
    assert "Proxmox host" not in page(web, token_headers, sample_report, None)


def test_many_host_packages_are_cut_with_a_count(web, token_headers, sample_report):
    packages = [{"name": f"lib{i:02d}", "current": "1", "new": "2", "origin": "Debian"} for i in range(20)]
    html = page(web, token_headers, sample_report, {"count": 20, "packages": packages, "kernel": False})
    assert "lib14" in html and "lib15" not in html and "… and 5 more" in html


# ---------------------------------------------------------------- upgrading the host

class Shipper:
    def __init__(self):
        self.text = ""

    def write(self, text):
        self.text += text


def upgrade(monkeypatch, apt_output=APT_OUTPUT, config=None, code=0):
    """Run upgrade_host with a fake apt-get: returns (results, shipper text, commands run)."""
    commands = []
    monkeypatch.setattr(c, "run", lambda cmd, timeout=120: (0, apt_output))

    def fake_stream(cmd, prefix, shipper, timeout, env=None):
        commands.append((cmd, prefix, env))
        shipper.write(prefix + "Setting up pve-manager\n")
        return code, "Setting up pve-manager", False

    monkeypatch.setattr(c, "stream_process", fake_stream)
    shipper = Shipper()
    return c.upgrade_host({}, config or {}, shipper), shipper.text, commands


NO_KERNEL = APT_OUTPUT.replace("Inst proxmox-kernel-6.14.11-5-pve-signed (6.14.11-5 Proxmox:trixie/stable [amd64])\n", "")


def test_the_host_is_upgraded_without_any_question(monkeypatch):
    results, text, commands = upgrade(monkeypatch, NO_KERNEL)
    assert results == [{"label": "Proxmox host", "ok": True, "message": "3 package(s) upgraded"}]
    cmd, prefix, env = commands[0]
    assert cmd[0] == "apt-get" and cmd[-1] == "dist-upgrade" and "-y" in cmd and "Dpkg::Options::=--force-confold" in cmd
    assert env["DEBIAN_FRONTEND"] == "noninteractive" and prefix == "[PVE] "
    assert "Upgrading 3 package(s)" in text


def test_a_new_kernel_is_never_installed_from_the_page(monkeypatch):
    results, text, commands = upgrade(monkeypatch)                      # the output includes a kernel package
    assert results[0]["ok"] is False and "kernel" in results[0]["message"] and commands == []
    assert "reboot" in text


def test_the_host_upgrade_can_be_switched_off_in_the_config(monkeypatch):
    results, _, commands = upgrade(monkeypatch, NO_KERNEL, config={"allow_host_upgrade": False})
    assert results[0]["ok"] is False and "allow_host_upgrade" in results[0]["message"] and commands == []


def test_nothing_to_install_or_a_failing_apt(monkeypatch):
    results, _, commands = upgrade(monkeypatch, "0 upgraded, 0 newly installed")
    assert results[0]["ok"] is True and "up to date" in results[0]["message"] and commands == []
    results, _, _ = upgrade(monkeypatch, NO_KERNEL, code=100)
    assert results[0]["ok"] is False and "Setting up pve-manager" in results[0]["message"]
    results, _, _ = upgrade(monkeypatch, NO_KERNEL, code=124)
    assert "timed out" in results[0]["message"]
    monkeypatch.setattr(c, "run", lambda cmd, timeout=120: (100, ""))
    assert "could not read" in c.upgrade_host({}, {}, Shipper())[0]["message"]


def test_the_report_tells_whether_the_upgrade_is_allowed(monkeypatch):
    monkeypatch.setattr(c, "run", lambda cmd, timeout=120: (0, APT_OUTPUT))
    assert {**c.host_updates(), "allowed": True}["allowed"] is True


def test_run_job_dispatches_the_host_upgrade(monkeypatch):
    calls = []

    class ClosableShipper(Shipper):
        def close(self):
            pass

    sent = []
    monkeypatch.setattr(c, "server_call", lambda config, method, path, body=None, timeout=15: sent.append((path, body)) or {})
    monkeypatch.setattr(c, "LogShipper", lambda config, job_id: ClosableShipper())
    monkeypatch.setattr(c, "upgrade_host", lambda job, config, shipper: calls.append(job["action"]) or [])
    monkeypatch.setattr(c, "host_updates", lambda: {"count": 0, "packages": [], "kernel": False})
    c.run_job({}, {"id": "j", "action": "upgrade_host", "ctids": []})
    assert calls == ["upgrade_host"]
    assert ("/api/report/host", {"count": 0, "packages": [], "kernel": False, "allowed": True}) in sent
    assert [path for path, _ in sent][-1] == "/api/jobs/j/result"       # the new list arrives before the job ends


def test_an_unreachable_service_does_not_break_the_end_of_the_job(monkeypatch):
    def unreachable(*args, **kwargs):
        raise OSError("down")

    monkeypatch.setattr(c, "server_call", unreachable)
    monkeypatch.setattr(c, "host_updates", lambda: {"count": 0, "packages": [], "kernel": False})
    c.send_host_updates({})


# ---------------------------------------------------------------- the page and the service

def host_state(web, token_headers, report, **host):
    report["host_updates"] = {"count": 3, "packages": c.parse_host_upgrades(NO_KERNEL), "kernel": False, **host}
    assert web.post("/api/report", json=report, headers=token_headers).status_code == 200


def test_the_page_offers_the_upgrade_when_no_kernel_is_involved(web, token_headers, sample_report):
    host_state(web, token_headers, sample_report)
    html = web.get("/").get_data(as_text=True)
    assert 'id="act-host"' in html and "Upgrade the host (3)" in html
    host_state(web, token_headers, sample_report, kernel=True)
    html = web.get("/").get_data(as_text=True)
    assert 'id="act-host"' not in html and "needs a reboot" in html
    host_state(web, token_headers, sample_report, allowed=False)
    html = web.get("/").get_data(as_text=True)
    assert 'id="act-host"' not in html and "node, Updates, Upgrade" in html


def test_the_host_upgrade_starts_a_job_without_containers(web, token_headers, sample_report):
    host_state(web, token_headers, sample_report)
    response = web.post("/api/actions", json={"action": "upgrade_host"})
    job = response.get_json()
    assert response.status_code == 202 and job["action"] == "upgrade_host" and job["ctids"] == [] and job["backup"] is False
    pending = web.get("/api/jobs/pending", headers=token_headers).get_json()
    assert pending["job"]["id"] == job["id"] and pending["refresh"] is False     # the containers are not checked again
    page = web.get("/").get_data(as_text=True)
    assert "Proxmox host upgrade" in page and 'id="console"' in page and 'id="console-tabs"' not in page


def test_the_host_upgrade_is_refused_when_it_should_not_run(web, token_headers, sample_report):
    host_state(web, token_headers, sample_report, kernel=True)
    refused = web.post("/api/actions", json={"action": "upgrade_host"})
    assert refused.status_code == 400 and refused.get_json()["code"] == "host_kernel"
    host_state(web, token_headers, sample_report, allowed=False)
    assert web.post("/api/actions", json={"action": "upgrade_host"}).get_json()["code"] == "host_upgrade_disabled"
    host_state(web, token_headers, sample_report, count=0, packages=[])
    assert web.post("/api/actions", json={"action": "upgrade_host"}).get_json()["code"] == "no_host_updates"
    sample_report.pop("host_updates")
    web.post("/api/report", json=sample_report, headers=token_headers)
    assert web.post("/api/actions", json={"action": "upgrade_host"}).get_json()["code"] == "no_host_updates"
    assert web.get("/api/status").get_json()["job"] is None


def test_the_host_upgrade_is_never_chained_with_the_containers(web, token_headers, sample_report):
    host_state(web, token_headers, sample_report)
    for actions in (["upgrade_host", "upgrade_os"], ["upgrade_os", "upgrade_host"], ["upgrade_host", "upgrade_host"]):
        assert web.post("/api/actions", json={"actions": actions}).status_code == 400, actions
    host_state(web, token_headers, sample_report)
    assert web.post("/api/actions", json={"action": "upgrade_host"}).status_code == 202
    assert web.post("/api/actions", json={"action": "upgrade_host"}).status_code == 409          # one job at a time


def test_refusal_messages_are_translated(web, token_headers, sample_report):
    host_state(web, token_headers, sample_report, kernel=True)
    page = web.get("/", headers={"Accept-Language": "fr"}).get_data(as_text=True)
    assert "redémarre" in page and "api_err" not in page.split("<script")[0]


def test_only_the_host_part_of_the_report_is_replaced(web, token_headers, sample_report):
    host_state(web, token_headers, sample_report)
    before = web.get("/api/state").get_json()
    new = {"count": 0, "packages": [], "kernel": False, "allowed": True}
    assert web.post("/api/report/host", json=new).status_code == 401                 # collector only
    assert web.post("/api/report/host", json=new, headers=token_headers).status_code == 200
    after = web.get("/api/state").get_json()
    assert after["host_updates"] == new
    assert {k: v for k, v in after.items() if k != "host_updates"} == {k: v for k, v in before.items() if k != "host_updates"}
    assert "Host up to date" in web.get("/").get_data(as_text=True)
    assert web.post("/api/report/host", json=[1], headers=token_headers).status_code == 400
    assert web.get("/api/status").get_json()["refreshing"] is False


def test_the_host_list_cannot_be_sent_before_any_report(web, token_headers):
    response = web.post("/api/report/host", json={"count": 0}, headers=token_headers)
    assert response.status_code == 400 and response.get_json()["code"] == "no_report"
