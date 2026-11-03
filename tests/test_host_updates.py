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
