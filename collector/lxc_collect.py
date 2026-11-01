#!/usr/bin/env python3
"""Collect system and application updates of the LXC containers and send them to the web service.

Run as root on the Proxmox host (it uses `pct`). Python 3.13+, standard library only.

  lxc_collect.py              collect, then send the report to the service (server_url of the config)
  lxc_collect.py --dry-run    collect and print the JSON without sending anything
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = os.environ.get("LXC_WATCH_CONFIG", "/etc/lxc-update-watch.json")
CACHE_DIR = os.environ.get("LXC_WATCH_CACHE", "/var/cache/lxc-update-watch")
SCRIPTS_REPO = "community-scripts/ProxmoxVE"
UPDATE_APPS_PATH = "tools/pve/update-apps.sh"
USER_AGENT = "lxc-update-watch"

VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)+[\w.+~-]*)")
APT_INST_RE = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \((\S+)")
APT_PKG_RE = re.compile(r"(\S+) (?:(\S+) )?→ (\S+)")
PRERELEASE_RE = re.compile(r"alpha|beta|rc\d|preview|nightly", re.IGNORECASE)
GH_CALL_RE = re.compile(r'check_for_gh_release\s+((?:"[^"]*"\s*){2,5})')
CHECK_RELEASE_RE = re.compile(r'check_for_gh_release\s+"[^"]*"\s+"([^"/]+/[^"]+)"(?:\s+"([^"]*)")?')
RELEASE_VAR_RE = re.compile(r'^\s*RELEASE="([^"$]+)"', re.MULTILINE)
PVE_SCRIPT_URL = "https://raw.githubusercontent.com/community-scripts/{repo}/main/ct/{name}.sh"


def read_version():
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


VERSION = read_version()


# ---------------------------------------------------------------- Proxmox helpers

def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    return p.returncode, p.stdout.strip()


def pct_exec(ctid, script, timeout=120):
    return run(["pct", "exec", str(ctid), "--", "sh", "-c", script], timeout)


def list_containers():
    _, out = run(["pct", "list"])
    containers = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            # The Lock column can be empty: the status is the 2nd field and the name the last one.
            containers.append((parts[0], parts[1], parts[-1]))
    return containers


# ---------------------------------------------------------------- operating system updates

def os_updates(ctid):
    _, osid = pct_exec(ctid, ". /etc/os-release 2>/dev/null; echo \"$ID\"")
    osid = osid or "unknown"
    if osid in ("debian", "ubuntu"):
        _, out = pct_exec(
            ctid,
            "apt-get update -qq >/dev/null 2>&1; apt-get -s upgrade 2>/dev/null | grep '^Inst'",
            timeout=300,
        )
        packages = []
        for line in out.splitlines():
            m = APT_INST_RE.match(line)
            if m:
                name, old, new = m.groups()
                packages.append(f"{name} {old} → {new}" if old else f"{name} → {new}")
        return osid, len(packages), packages
    if osid == "alpine":
        _, out = pct_exec(ctid, "apk update >/dev/null 2>&1; apk list -u 2>/dev/null", timeout=300)
        packages = [line.split()[0] for line in out.splitlines() if line.strip()]
        return osid, len(packages), packages
    return osid, None, []


# ---------------------------------------------------------------- versions

def parse_version(text, pattern=None):
    m = re.search(pattern, text) if pattern else VERSION_RE.search(text)
    if not m:
        return None
    return (m.group(1) if m.groups() else m.group(0)).lstrip("v")


def version_key(version):
    try:
        return tuple(int(x) for x in re.split(r"[.-]", version.split("+")[0]) if x.isdigit())
    except ValueError:
        return ()


def strip_v(version):
    return version[1:] if re.match(r"v\d", version) else version


def latest_github_release(repo, token, cache, prefix=None):
    """Tag of the latest release; with `prefix`, the newest one whose version starts with that prefix."""
    key = f"{repo}@{prefix}" if prefix else repo
    if key in cache:
        return cache[key]
    url = f"https://api.github.com/repos/{repo}/releases" + ("?per_page=100" if prefix else "/latest")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if prefix:
            candidates = [
                r["tag_name"] for r in data
                if not r["draft"] and not r["prerelease"] and not PRERELEASE_RE.search(r["tag_name"])
                and (parse_version(r["tag_name"]) or "").startswith(prefix)
            ]
            cache[key] = max(candidates, key=lambda t: version_key(parse_version(t)), default=None)
        else:
            cache[key] = data["tag_name"]
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        cache[key] = None
    return cache[key]


def cached_fetch(url, ttl, token=None):
    """GET with a disk cache (spares GitHub); returns None when unavailable and nothing is cached."""
    path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest())
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path, encoding="utf-8") as f:
                return f.read()
    except OSError:
        pass
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        try:
            with open(path, encoding="utf-8") as f:  # on failure, a stale copy beats nothing
                return f.read()
        except OSError:
            return None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    return text


# ---------------------------------------------------------------- applications declared in the config

def pve_script_release(name, repo_name, cache):
    """Read a helper script: the GitHub repository it tracks and its pinned version (None = latest release)."""
    key = ("pve", repo_name, name)
    if key not in cache:
        cache[key] = None
        try:
            req = urllib.request.Request(PVE_SCRIPT_URL.format(repo=repo_name, name=name),
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", "replace")
            m = CHECK_RELEASE_RE.search(text)
            if m:
                repo, arg = m.groups()
                pinned = None
                if arg and re.match(r"v?\d", arg):
                    pinned = arg
                elif arg and "RELEASE" in arg:
                    var = RELEASE_VAR_RE.search(text)
                    pinned = var.group(1) if var else None
                cache[key] = {"repo": repo, "pinned": pinned}
        except (urllib.error.URLError, ValueError, TimeoutError):
            pass
    return cache[key]


def app_status(ctid, app_cfg, github_token, cache):
    """Compare the installed version with the GitHub release (or the version a helper script pins)."""
    name = app_cfg.get("name", f"CT {ctid}")
    if "version_file" in app_cfg:
        _, raw = pct_exec(ctid, f"cat {app_cfg['version_file']}")
    elif "version_cmd" in app_cfg:
        _, raw = pct_exec(ctid, app_cfg["version_cmd"])
    else:
        return {"name": name, "error": "missing_version_source"}
    installed = parse_version(raw, app_cfg.get("version_regex"))
    if not installed:
        return {"name": name, "error": "installed_version_not_found"}
    # Priority: the version the helper script will install, then the latest GitHub release.
    script = pve_script_release(app_cfg["pve_script"], app_cfg.get("pve_repo", "ProxmoxVE"), cache) \
        if "pve_script" in app_cfg else None
    gh_repo = app_cfg.get("repo") or (script or {}).get("repo")
    if script and script["pinned"]:
        tag, source = script["pinned"], "helper-script"
    else:
        tag = latest_github_release(gh_repo, github_token, cache, app_cfg.get("version_prefix")) if gh_repo else None
        source = "GitHub"
    latest = parse_version(tag, app_cfg.get("version_regex")) if tag else None
    if not latest:
        return {"name": name, "installed": installed, "error": "latest_version_unavailable"}
    a, b = version_key(installed), version_key(latest)
    update = b > a if a and b else installed != latest
    result = {"name": name, "installed": installed, "latest": latest, "update": update, "source": source}
    if app_cfg.get("update_cmd"):
        result["update_cmd"] = app_cfg["update_cmd"]
    return result


def apt_app_status(ctid, app_cfg, packages):
    """Application installed with apt: an update exists when its package is in the list of pending upgrades."""
    name, pkg = app_cfg.get("name", f"CT {ctid}"), app_cfg["apt_package"]
    for p in packages:
        m = APT_PKG_RE.match(p)
        if m and m.group(1) == pkg:
            result = {"name": name, "installed": m.group(2) or "?", "latest": m.group(3), "update": True}
            break
    else:
        _, installed = pct_exec(ctid, f"dpkg-query -W -f='${{Version}}' {pkg} 2>/dev/null")
        if not installed:
            return {"name": name, "error": "package_not_installed"}
        result = {"name": name, "installed": installed, "latest": installed, "update": False}
    if app_cfg.get("update_cmd"):
        result["update_cmd"] = app_cfg["update_cmd"]
    return result


# ---------------------------------------------------------------- Docker images

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
DOCKER_LIST_SCRIPT = (
    "command -v docker >/dev/null 2>&1 || exit 0; "
    "docker ps --format '{{.Image}}' 2>/dev/null | sort -u | while read -r img; do "
    "printf '%s\\t' \"$img\"; docker image inspect -f '{{json .RepoDigests}}' \"$img\" 2>/dev/null || echo '[]'; "
    "done"
)


def parse_image(image):
    """'ghcr.io/o/app:1.2' -> (registry, repository, tag, name without tag); None if pinned by digest."""
    if "@" in image:
        return None
    name, tag = image, "latest"
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = image.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    return registry, repo, tag, name


def registry_digest(registry, repo, tag, auth, cache):
    """Current digest of `tag` in the registry (HEAD on the manifest), with an anonymous or configured token."""
    key = (registry, repo, tag)
    if key in cache:
        return cache[key]
    host = "registry-1.docker.io" if registry == "docker.io" else registry
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": MANIFEST_ACCEPT, "User-Agent": USER_AGENT}
    digest = None
    try:
        for _ in range(2):
            req = urllib.request.Request(url, headers=headers, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    digest = resp.headers.get("Docker-Content-Digest")
                break
            except urllib.error.HTTPError as e:
                challenge = e.headers.get("WWW-Authenticate", "")
                if e.code != 401 or not challenge.startswith("Bearer ") or "Authorization" in headers:
                    break
                params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                token_req = urllib.request.Request(
                    f"{params['realm']}?service={params.get('service', '')}&scope=repository:{repo}:pull",
                    headers={"User-Agent": USER_AGENT},
                )
                creds = auth.get(registry)
                if creds:
                    basic = base64.b64encode(f"{creds['username']}:{creds['token']}".encode()).decode()
                    token_req.add_header("Authorization", f"Basic {basic}")
                with urllib.request.urlopen(token_req, timeout=15) as resp:
                    body = json.load(resp)
                headers["Authorization"] = f"Bearer {body.get('token') or body['access_token']}"
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        digest = None
    cache[key] = digest
    return digest


def docker_status(ctid, auth, cache):
    """For every image running in the LXC, compare its local digest with the registry's."""
    _, out = pct_exec(ctid, DOCKER_LIST_SCRIPT, timeout=60)
    images = []
    for line in out.splitlines():
        image, _, digests_json = line.partition("\t")
        parsed = parse_image(image)
        entry = {"image": image}
        try:
            digests = json.loads(digests_json or "[]")
        except ValueError:
            digests = []
        if not parsed:
            entry["error"] = "pinned_by_digest"
        elif not digests:
            entry["local"] = True  # built locally: no registry to compare with
        else:
            registry, repo, tag, name = parsed
            local = next((d.split("@", 1)[1] for d in digests if d.split("@", 1)[0] == name), None)
            local = local or (digests[0].split("@", 1)[1] if len(digests) == 1 else None)
            remote = registry_digest(registry, repo, tag, auth, cache)
            if not local:
                entry["error"] = "local_digest_not_found"
            elif not remote:
                entry["error"] = "registry_unreachable"
            else:
                entry["update"] = local != remote
        images.append(entry)
    return images


# ---------------------------------------------------------------- automatic detection from the helper scripts

def normalize_name(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def script_index(config, cache):
    """Container scripts (ct/) and add-ons (tools/addon/) of community-scripts, indexed by normalized name."""
    if "script_index" not in cache:
        index = {}
        text = cached_fetch(f"https://api.github.com/repos/{SCRIPTS_REPO}/git/trees/main?recursive=1", 86400,
                            config.get("github_token"))
        try:
            for entry in json.loads(text or "{}").get("tree", []):
                m = re.match(r"(ct|tools/addon)/([^/]+)\.sh$", entry["path"])
                if m:
                    kind = "ct" if m.group(1) == "ct" else "addon"
                    index.setdefault(normalize_name(m.group(2)), []).append((kind, m.group(2), entry["path"]))
        except (ValueError, KeyError, TypeError):
            index = {}
        for entries in index.values():
            entries.sort(key=lambda e: e[0] != "ct")  # a container script comes before an add-on
        cache["script_index"] = index
    return cache["script_index"]


def script_components(text):
    """Components tracked by a script: its check_for_gh_release calls (name, repository, pinned version, tag prefix)."""
    release_var = RELEASE_VAR_RE.search(text)
    components = []
    for match in GH_CALL_RE.finditer(text):
        args = re.findall(r'"([^"]*)"', match.group(1))
        name, repo = args[0], args[1]
        pin = args[2] if len(args) > 2 else ""
        prefix = args[4] if len(args) > 4 else ""
        if re.fullmatch(r"\$\{?RELEASE\}?", pin):
            pin = release_var.group(1) if release_var else "?"
        if "$" in name or "$" in repo or "$" in pin or "$" in prefix or pin == "?":
            continue  # computed at run time: cannot be known from here
        components.append({"name": name, "repo": repo, "pin": pin, "prefix": prefix})
    return components


def release_target(comp, token):
    """Version the script would offer: the pinned one, else the latest stable release (with an optional tag prefix)."""
    if comp["pin"]:
        return comp["pin"]
    api = f"https://api.github.com/repos/{comp['repo']}/releases"
    if comp["prefix"]:
        text = cached_fetch(api + "?per_page=100", 900, token)
        try:
            tags = [r["tag_name"] for r in json.loads(text or "[]") if not r["draft"] and not r["prerelease"]
                    and r["tag_name"].startswith(comp["prefix"])]
        except (ValueError, KeyError, TypeError):
            return None
        return tags[0] if tags else None
    text = cached_fetch(api + "/latest", 900, token)
    try:
        return json.loads(text or "{}").get("tag_name")
    except ValueError:
        return None


def auto_app_status(ctid, name, config, cache):
    """Same rule as the helper scripts: version read from ~/.<app>, compared with the release the script would install."""
    token = config.get("github_token")
    for kind, slug, path in script_index(config, cache).get(normalize_name(name), []):
        text = cached_fetch(f"https://raw.githubusercontent.com/{SCRIPTS_REPO}/main/{path}", 86400)
        components = script_components(text or "")
        if not components:
            continue
        files = {c["name"]: "/root/." + c["name"].lower().replace(" ", "") for c in components}
        probe = " ".join(files.values())
        _, out = pct_exec(ctid, f"for f in {probe}; do [ -f \"$f\" ] && printf '%s\\t%s\\n' \"$f\" \"$(head -c 120 \"$f\")\"; done; true")
        installed = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        found = []
        for comp in components:
            if files[comp["name"]] not in installed:
                continue
            current = strip_v(installed[files[comp["name"]]].strip())
            tag = release_target(comp, token)
            target = strip_v(tag) if tag else None
            found.append({"name": comp["name"], "installed": current, "latest": target,
                          "update": bool(target) and current != target,
                          "error": None if target else "target_version_unavailable"})
        if not found:
            continue  # the host name looks like this script but nothing from it is installed here
        main = next((c for c in found if c["update"]), found[0])
        app = {"name": main["name"], "source": "helper-script (auto)",
               "components": [{k: v for k, v in c.items() if k != "error"} for c in found]}
        if main["error"]:
            app["error"] = main["error"]
        else:
            app.update(installed=main["installed"], latest=main["latest"], update=main["update"])
        app["update_cmd"] = ("update" if kind == "ct" else
                             f'bash -c "$(curl -fsSL https://raw.githubusercontent.com/{SCRIPTS_REPO}/main/tools/addon/{slug}.sh)"')
        return app
    return None


class ProgressReporter:
    """Tells the web page what the collection is doing, step by step, so that it can animate each container.

    Best effort: events are sent in the background, once every half second, and the collection never waits for them."""

    MAX_PENDING = 2000

    def __init__(self, config, run_id):
        self.config, self.run_id = config, run_id
        self.lock = threading.Lock()
        self.pending = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def event(self, **fields):
        with self.lock:
            self.pending.append(fields)

    def _flush(self):
        with self.lock:
            events, self.pending = self.pending, []
        if not events:
            return
        try:
            server_call(self.config, "POST", "/api/progress", {"run": self.run_id, "events": events}, timeout=5)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError, KeyError):
            with self.lock:
                self.pending = (events + self.pending)[-self.MAX_PENDING:]  # retried on the next pass

    def _loop(self):
        while not self.stop.wait(0.5):
            self._flush()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        self._flush()


def report_step(progress, ctid, step, state, data=None):
    if progress:
        progress.event(type="step", ctid=int(ctid), step=step, state=state, data=data or {})


def collect_one(entry, config, cache, progress=None):
    ctid, status, name = entry
    ct = {"ctid": int(ctid), "name": name, "status": status}
    if status != "running":
        return ct
    report_step(progress, ctid, "system", "running")
    ct["os"], ct["os_updates"], ct["packages"] = os_updates(ctid)
    rc, _ = pct_exec(ctid, "test -f /usr/bin/update")
    ct["helper_script"] = rc == 0
    report_step(progress, ctid, "system", "done",
                {k: ct[k] for k in ("os", "os_updates", "packages", "helper_script")})

    report_step(progress, ctid, "app", "running")
    app_cfg = config.get("apps", {}).get(ctid)
    if app_cfg and "apt_package" in app_cfg:
        ct["app"] = apt_app_status(ctid, app_cfg, ct["packages"])
    elif app_cfg:
        ct["app"] = app_status(ctid, app_cfg, config.get("github_token"), cache)
    elif config.get("auto_apps", True):
        auto = auto_app_status(ctid, name, config, cache)
        if auto:
            ct["app"] = auto
    report_step(progress, ctid, "app", "done", {"app": ct["app"]} if "app" in ct else None)

    report_step(progress, ctid, "docker", "running")
    docker = docker_status(ctid, config.get("registry_auth", {}), cache)
    if docker:
        ct["docker"] = docker
    report_step(progress, ctid, "docker", "done", {"docker": docker} if docker else None)
    return ct


# ---------------------------------------------------------------- jobs requested from the web page

UPGRADE_SCRIPT = (
    '. /etc/os-release 2>/dev/null; '
    'case "$ID" in '
    'debian|ubuntu) export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && '
    'apt-get -y -o Dpkg::Options::=--force-confold upgrade;; '
    'alpine) apk update && apk upgrade;; '
    '*) echo "unsupported OS"; exit 2;; esac'
)

CSI_RE = re.compile(r"\x1b\[[0-9;?]*([@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]|\x1b(?!\[)[()]?[A-Za-z0-9]?")


READABLE_RE = re.compile(r"[^\W_]")  # a letter or a digit: lines without one are banner art and frames


def clean_line(raw):
    """Keep colors (SGR) but drop cursor moves, erasures and spinners: a \\r rewrites the current line."""
    text = CSI_RE.sub(lambda m: m.group(0) if m.group(1) == "m" else "", raw.rstrip("\n"))
    segments = [s for s in text.split("\r") if s.strip()]
    return CONTROL_RE.sub("", segments[-1]) if segments else ""


def finish(shipper, ctid, ok, note=""):
    """Last lines of a container in the console. The page recognizes the marker and shows the container as done."""
    if note:
        shipper.write(f"[CT {ctid}] {note}\n")
    shipper.write(f"[CT {ctid}] \x1b[{'32' if ok else '31'};1m== finished: {'ok' if ok else 'failed'} ==\x1b[0m\n")


class LogShipper:
    """Sends the output of the commands to the web page in batches (once a second) without ever blocking the job."""

    def __init__(self, config, job_id):
        self.config, self.job_id = config, job_id
        self.lock = threading.Lock()
        self.pending = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def write(self, text):
        with self.lock:
            self.pending.append(text)

    def _flush(self):
        with self.lock:
            text, self.pending = "".join(self.pending), []
        if not text:
            return
        try:
            server_call(self.config, "POST", f"/api/jobs/{self.job_id}/log", {"text": text}, timeout=10)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            with self.lock:
                self.pending.insert(0, text)  # retried on the next pass

    def _loop(self):
        while not self.stop.wait(1.0):
            self._flush()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        self._flush()


def stream_process(cmd, prefix, shipper, timeout, env=None):
    """Run a command and stream its output line by line; returns (exit code, last line, error seen)."""
    # Output is read as bytes: text mode would turn the \r of spinners into as many lines.
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    timed_out = []

    def on_timeout():
        timed_out.append(True)
        proc.kill()

    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    last, saw_error = "", False
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = clean_line(raw.decode("utf-8", "replace"))
            if not READABLE_RE.search(CSI_RE.sub("", line)):
                continue  # nothing readable: the ASCII-art banner of the official scripts, frames, blank lines
            shipper.write(f"{prefix}{line}\x1b[0m\n")
            last = CSI_RE.sub("", line).strip()
            saw_error = saw_error or "[ERROR]" in line
        code = proc.wait()
    finally:
        timer.cancel()
    return (124 if timed_out else code), last, saw_error


def upgrade_step(ctid, shipper):
    """Upgrade the system packages of one LXC (plain upgrade, never dist-upgrade); returns (ok, message, note)."""
    code, last, _ = stream_process(["pct", "exec", str(ctid), "--", "sh", "-c", UPGRADE_SCRIPT],
                                   f"[CT {ctid}] ", shipper, 1800)
    if code == 124:
        return False, "timed out after 30 minutes", "timed out after 30 minutes"
    return code == 0, last[:200], ""


def upgrade_one(ctid, name, shipper):
    ok, message, note = upgrade_step(ctid, shipper)
    finish(shipper, ctid, ok, note)
    return {"label": f"CT {ctid} {name}", "ok": ok, "message": message}


def upgrade_containers(ctids, config, shipper):
    running = {int(c[0]): c[2] for c in list_containers() if c[1] == "running"}
    results, todo, skipped = [], [], []
    for ctid in ctids:
        if ctid in running:
            todo.append((ctid, running[ctid]))
        else:
            results.append({"label": f"CT {ctid}", "ok": False, "message": "container stopped or not found"})
            skipped.append(ctid)
    workers = int(config.get("upgrade_workers", 4))
    shipper.write(f"System update of {len(todo)} container(s), {workers} in parallel\n")
    for ctid in skipped:
        finish(shipper, ctid, False, "container stopped or not found")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results += list(pool.map(lambda t: upgrade_one(*t, shipper), todo))
    return sorted(results, key=lambda r: r["label"])


NO_UPDATE_COMMAND = "stopped, or no update command (container not created by a helper script)"


def fetch_update_apps_script():
    return cached_fetch(f"https://raw.githubusercontent.com/{SCRIPTS_REPO}/main/{UPDATE_APPS_PATH}", 3600)


def apps_step(ctid, script, backup_storage, shipper):
    """Run the official update script for one LXC; returns (ok, message, note).

    The script makes the backup itself when `backup_storage` is given."""
    env = {**os.environ, "TERM": "xterm", "var_container": str(ctid), "var_unattended": "yes",
           "var_skip_confirm": "yes", "var_continue_on_error": "yes", "var_auto_reboot": "no",
           "var_backup": "yes" if backup_storage else "no"}
    if backup_storage:
        env["var_backup_storage"] = backup_storage
    code, last, saw_error = stream_process(["bash", "-c", script], f"[CT {ctid}] ", shipper, 3600, env)
    if code == 124:
        return False, "timed out after one hour", "timed out after one hour"
    ok = code == 0 and not saw_error
    return ok, (last if ok else (last or f"exit code {code}"))[:200], ""


def update_apps(job, config, shipper):
    """Run the official "PVE LXC Apps Update" script without interaction, one instance per container."""
    script = fetch_update_apps_script()
    if not script:
        return [{"label": "update-apps.sh", "ok": False, "message": "official script unavailable (network issue?)"}]
    storage = config.get("backup_storage")
    if job.get("backup") and not storage:
        return [{"label": "Backup", "ok": False, "message": "backup_storage is not set in the collector config"}]
    running = {int(c[0]): c[2] for c in list_containers() if c[1] == "running"}
    results, todo, skipped = [], [], []
    for ctid in job["ctids"]:
        has_update = ctid in running and pct_exec(ctid, "test -f /usr/bin/update")[0] == 0
        if has_update:
            todo.append((ctid, running[ctid]))
        else:
            results.append({"label": f"CT {ctid}", "ok": False, "message": NO_UPDATE_COMMAND})
            skipped.append(ctid)
    workers = int(config.get("app_update_workers", 2))
    backup_note = f", backing up to {storage} first" if job.get("backup") else ", no backup"
    shipper.write(f"Application update of {len(todo)} container(s) with the official script "
                  f"({workers} in parallel{backup_note})\n")
    for ctid in skipped:
        finish(shipper, ctid, False, NO_UPDATE_COMMAND)

    def one(item):
        ctid, name = item
        ok, message, note = apps_step(ctid, script, storage if job.get("backup") else None, shipper)
        finish(shipper, ctid, ok, note)
        return {"label": f"CT {ctid} {name}", "ok": ok, "message": message}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results += list(pool.map(one, todo))
    return sorted(results, key=lambda r: r["label"])


# ---------------------------------------------------------------- Docker stacks

# One line per running container: <compose project>|<working dir>|<compose files>|<name>|<image>. Docker Compose
# records the first three as labels on every container it creates, which is how a stack is found again.
DOCKER_INVENTORY_SCRIPT = (
    "command -v docker >/dev/null 2>&1 || { echo NODOCKER; exit 0; }; "
    "for id in $(docker ps -q); do docker inspect --format "
    "'{{index .Config.Labels \"com.docker.compose.project\"}}|"
    "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}|"
    "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}|"
    "{{.Name}}|{{.Config.Image}}' \"$id\"; done"
)


def parse_docker_inventory(text):
    """Split the running containers of an LXC into Compose stacks and containers started another way.

    Returns (stacks, unmanaged), or None when Docker is not installed."""
    if text.strip() == "NODOCKER":
        return None
    stacks, unmanaged = {}, []
    for line in text.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        project, workdir, files, name, image = parts
        name = name.lstrip("/")
        if project and files:
            stacks.setdefault((project, workdir, files), []).append(name)
        else:
            unmanaged.append(f"{name} ({image})")
    return ([{"project": key[0], "workdir": key[1], "files": key[2], "containers": names}
             for key, names in stacks.items()], unmanaged)


def compose_script(stack):
    """Shell script run inside the LXC: check the files, download the new images, recreate what changed."""
    files = [f for f in stack["files"].split(",") if f]
    options = f"--project-directory {shlex.quote(stack['workdir'])} -p {shlex.quote(stack['project'])} " + \
              " ".join(f"-f {shlex.quote(f)}" for f in files)
    checks = "".join(f"[ -f {shlex.quote(f)} ] || {{ echo 'compose file not found: '{shlex.quote(f)}; exit 3; }}\n"
                     for f in files)
    return ('if docker compose version >/dev/null 2>&1; then C="docker compose"; else C=docker-compose; fi\n'
            f"{checks}$C {options} pull && $C {options} up -d\n")


def backup_container(ctid, storage, shipper):
    """vzdump of the whole container before touching it (the same command as the official update script)."""
    shipper.write(f"[CT {ctid}] Backing up to {storage} before updating...\n")
    code, _, _ = stream_process(["vzdump", str(ctid), "--compress", "zstd", "--storage", storage,
                                 "--notes-template", "{{guestname}} - lxc-update-watch backup"],
                                f"[CT {ctid}] ", shipper, 7200)
    return code == 0


def docker_step(ctid, job, shipper):
    """docker compose pull, then up -d, for every Compose stack of one LXC, then remove the old images.

    Returns (ok, message, note)."""
    prefix = f"[CT {ctid}] "
    inventory = parse_docker_inventory(pct_exec(ctid, DOCKER_INVENTORY_SCRIPT, timeout=60)[1])
    if inventory is None:
        return False, "Docker is not installed in this container", "Docker is not installed"
    stacks, unmanaged = inventory
    if unmanaged:
        shipper.write(f"{prefix}Not managed by Docker Compose, update them by hand: {', '.join(unmanaged)}\n")
    if not stacks:
        return False, "no running Docker Compose stack found", "no running Docker Compose stack found"

    failed = []
    for stack in stacks:
        shipper.write(f"{prefix}== stack {stack['project']} ({', '.join(stack['containers'])}) ==\n")
        code, _, _ = stream_process(["pct", "exec", str(ctid), "--", "sh", "-c", compose_script(stack)],
                                    prefix, shipper, 3600)
        if code != 0:
            failed.append(stack["project"])
            shipper.write(f"{prefix}stack {stack['project']} failed (exit code {code})\n")
    if failed:
        return False, f"failed: {', '.join(failed)}"[:200], "old images kept" if job.get("cleanup") else ""

    summary = f"{len(stacks)} stack(s) updated"
    if job.get("cleanup"):  # only dangling images: the ones the pull left without a name and nothing uses
        code, last, _ = stream_process(["pct", "exec", str(ctid), "--", "docker", "image", "prune", "-f"],
                                       prefix, shipper, 900)
        summary += f", {last}" if code == 0 and last else ", image cleanup failed" if code else ""
    if unmanaged:
        summary += f"; {len(unmanaged)} container(s) not managed by Compose"
    return True, summary[:200], ""


def update_docker_one(ctid, name, job, config, shipper):
    """Back up if asked, then update the Docker stacks of one LXC."""
    if job.get("backup") and not backup_container(ctid, config.get("backup_storage"), shipper):
        ok, message, note = False, "backup failed: nothing was updated", "backup failed, nothing was updated"
    else:
        ok, message, note = docker_step(ctid, job, shipper)
    finish(shipper, ctid, ok, note)
    return {"label": f"CT {ctid} {name}", "ok": ok, "message": message}


def update_docker(job, config, shipper):
    """Update the Docker Compose stacks of the containers of a job, a few containers at a time."""
    if job.get("backup") and not config.get("backup_storage"):
        return [{"label": "Backup", "ok": False, "message": "backup_storage is not set in the collector config"}]
    running = {int(c[0]): c[2] for c in list_containers() if c[1] == "running"}
    results, todo = [], []
    for ctid in job["ctids"]:
        if ctid in running:
            todo.append((ctid, running[ctid]))
        else:
            results.append({"label": f"CT {ctid}", "ok": False, "message": "container stopped or not found"})
    workers = int(config.get("docker_update_workers", 2))
    notes = [f"{workers} in parallel", "backup first" if job.get("backup") else "no backup",
             "old images removed afterwards" if job.get("cleanup") else "old images kept"]
    shipper.write(f"Docker stack update of {len(todo)} container(s) ({', '.join(notes)})\n")
    for ctid in [r for r in job["ctids"] if r not in running]:
        finish(shipper, ctid, False, "container stopped or not found")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results += list(pool.map(lambda item: update_docker_one(*item, job, config, shipper), todo))
    return sorted(results, key=lambda r: r["label"])


# ---------------------------------------------------------------- several updates in one job

CHAIN_ORDER = ("upgrade_os", "update_apps", "update_docker")  # base system first, then the application, then its images
CHAIN_NAMES = {"upgrade_os": "system", "update_apps": "applications", "update_docker": "Docker stacks"}
CHAIN_WORKERS = {"upgrade_os": ("upgrade_workers", 4), "update_apps": ("app_update_workers", 2),
                 "update_docker": ("docker_update_workers", 2)}


def update_chain(job, config, shipper):
    """Run several updates in one job: containers in parallel, the steps of each container one after the other.

    job["actions"] lists the steps and job["targets"] the containers of each. A container is backed up once, before
    its first step, and stops at its first failed step; the other containers carry on."""
    actions = [a for a in CHAIN_ORDER if a in job.get("actions", [])]
    targets = {a: {int(c) for c in job.get("targets", {}).get(a, [])} for a in actions}
    storage = config.get("backup_storage")
    backup = bool(job.get("backup")) and any(a in ("update_apps", "update_docker") for a in actions)
    if backup and not storage:
        return [{"label": "Backup", "ok": False, "message": "backup_storage is not set in the collector config"}]
    script = fetch_update_apps_script() if "update_apps" in actions else None
    running = {int(c[0]): c[2] for c in list_containers() if c[1] == "running"}
    results, todo = [], []
    for ctid in sorted(set().union(*targets.values())):
        if ctid in running:
            todo.append((ctid, running[ctid]))
        else:
            results.append({"label": f"CT {ctid}", "ok": False, "message": "container stopped or not found"})
            finish(shipper, ctid, False, "container stopped or not found")
    workers = max(1, min(int(config.get(CHAIN_WORKERS[a][0], CHAIN_WORKERS[a][1])) for a in actions))
    shipper.write(f"Update of {len(todo)} container(s): {' then '.join(CHAIN_NAMES[a] for a in actions)} "
                  f"({workers} in parallel, {'backup first' if backup else 'no backup'})\n")

    def run_step(action, ctid):
        if action == "upgrade_os":
            return upgrade_step(ctid, shipper)
        if action == "update_apps":
            if not script:
                return False, "official script unavailable (network issue?)", ""
            if pct_exec(ctid, "test -f /usr/bin/update")[0] != 0:
                return False, NO_UPDATE_COMMAND, NO_UPDATE_COMMAND
            return apps_step(ctid, script, None, shipper)  # the backup, if any, was made before the first step
        return docker_step(ctid, job, shipper)

    def one(item):
        ctid, name = item
        label, prefix, done = f"CT {ctid} {name}", f"[CT {ctid}] ", []
        if backup and not backup_container(ctid, storage, shipper):
            finish(shipper, ctid, False, "backup failed, nothing was updated")
            return {"label": label, "ok": False, "message": "backup failed: nothing was updated"}
        mine = [a for a in actions if ctid in targets[a]]
        for index, action in enumerate(mine):
            shipper.write(f"{prefix}== {CHAIN_NAMES[action]} ==\n")
            ok, message, note = run_step(action, ctid)
            done.append(f"{CHAIN_NAMES[action]}: {message or ('ok' if ok else 'failed')}")
            if not ok:
                later = [CHAIN_NAMES[a] for a in mine[index + 1:]]
                if later:
                    done.append(f"not run: {', '.join(later)}")
                    shipper.write(f"{prefix}Stopped after the {CHAIN_NAMES[action]} failure, not run: {', '.join(later)}\n")
                finish(shipper, ctid, False, note)
                return {"label": label, "ok": False, "message": "; ".join(done)[:200]}
        finish(shipper, ctid, True)
        return {"label": label, "ok": True, "message": "; ".join(done)[:200]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results += list(pool.map(one, todo))
    return sorted(results, key=lambda r: r["label"])


def normalize_id(text):
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"\.(sh|bash|py|js|ts)$", "", (text or "").lower())).strip("-")


def trpc(base, procedure, payload=None, timeout=60):
    """Call a tRPC procedure of PVE Scripts Local (SuperJSON transformer): GET without input, POST with {"json": input}."""
    url = f"{base.rstrip('/')}/api/trpc/{procedure}"
    if payload is None:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    else:
        req = urllib.request.Request(url, data=json.dumps({"json": payload}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["result"]["data"]["json"]


def downloaded_pve_slugs(base):
    """Same selection as "Update all downloaded scripts": cards whose script is present locally."""
    local = trpc(base, "scripts.getAllDownloadedScripts")["scripts"]
    cards = trpc(base, "scripts.getScriptCardsWithCategories")["cards"]
    slugs = []
    for card in cards:
        card_slug = (card.get("slug") or "").lower()
        basenames = {normalize_id(b) for b in card.get("install_basenames") or []}
        if any((s.get("slug") or "").lower() == card_slug or normalize_id(s.get("name")) in basenames
               for s in local if s.get("name")):
            slugs.append(card["slug"])
    return slugs


def update_pve_scripts(config):
    base = (config.get("pve_scripts_local") or {}).get("url")
    if not base:
        return [{"label": "PVE Scripts Local", "ok": False, "message": "pve_scripts_local.url is not set in the config"}]
    try:
        slugs = downloaded_pve_slugs(base)
        if not slugs:
            return [{"label": "PVE Scripts Local", "ok": False, "message": "no downloaded script found"}]
        data = trpc(base, "scripts.loadMultipleScripts", {"slugs": slugs}, timeout=1800)
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as e:
        return [{"label": "PVE Scripts Local", "ok": False, "message": f"API unreachable or unexpected: {e}"}]
    results = [{"label": "PVE Scripts Local", "ok": bool(data.get("success")) and not data.get("failed"),
                "message": data.get("message") or data.get("error") or ""}]
    results += [{"label": f["slug"], "ok": False, "message": f["error"][:200]} for f in (data.get("failed") or [])[:10]]
    return results


def server_call(config, method, path, body=None, timeout=15):
    req = urllib.request.Request(
        config["server_url"].rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None),
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config['token']}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def run_job(config, job):
    server_call(config, "POST", f"/api/jobs/{job['id']}/start")
    shipper = LogShipper(config, job["id"])
    try:
        if job["action"] == "upgrade_os":
            results = upgrade_containers(job["ctids"], config, shipper)
        elif job["action"] == "update_apps":
            results = update_apps(job, config, shipper)
        elif job["action"] == "update_docker":
            results = update_docker(job, config, shipper)
        elif job["action"] == "update_chain":
            results = update_chain(job, config, shipper)
        elif job["action"] == "update_pve_scripts":
            shipper.write("Refreshing the scripts downloaded in PVE Scripts Local…\n")
            results = update_pve_scripts(config)
        else:
            results = [{"label": job["action"], "ok": False, "message": "unknown action"}]
    except Exception as e:  # the job must always finish on the service side
        results = [{"label": job["action"], "ok": False, "message": str(e)[:200]}]
    failed = sum(1 for r in results if not r["ok"])
    shipper.write(f"\n== Done: {len(results) - failed} succeeded, {failed} failed ==\n")
    shipper.close()
    server_call(config, "POST", f"/api/jobs/{job['id']}/result", {"results": results}, timeout=30)


# ---------------------------------------------------------------- entry point

def handle_requests(config):
    """Run the job requested from the web page, if any. Returns True when a full check must follow.

    The service asks for that check itself (a refresh request) when the job changes the containers, i.e. after a
    system or application update; refreshing the PVE scripts leaves them untouched, so no check follows it."""
    try:
        pending = server_call(config, "GET", "/api/jobs/pending", timeout=10)
    except (OSError, ValueError):  # unreachable service, dropped connection, reply that is not JSON
        return False
    if pending.get("job"):
        run_job(config, pending["job"])
    return bool(pending.get("refresh"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--dry-run", action="store_true", help="print the JSON report without sending it")
    parser.add_argument("--if-requested", action="store_true",
                        help="only act if the web page requested a check or an action")
    parser.add_argument("--probe-pve-scripts", action="store_true",
                        help="list the scripts that \"Update all downloaded scripts\" would refresh, changing nothing")
    args = parser.parse_args()

    if run(["which", "pct"])[0] != 0:
        sys.exit("pct not found: run this on the Proxmox host.")
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
        if not args.dry_run:
            sys.exit(f"Config file not found: {CONFIG_PATH}")

    if args.probe_pve_scripts:
        base = (config.get("pve_scripts_local") or {}).get("url")
        if not base:
            sys.exit("pve_scripts_local.url is not set in the config.")
        slugs = downloaded_pve_slugs(base)
        print(f"{len(slugs)} script(s) would be refreshed:", ", ".join(sorted(slugs)))
        return

    if args.if_requested and not handle_requests(config):
        return

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = {}
    to_check = list_containers()
    progress = None if args.dry_run else ProgressReporter(config, started_at)
    if progress:
        progress.event(type="start", containers=[{"ctid": int(c[0]), "name": c[2], "status": c[1]} for c in to_check])
    with ThreadPoolExecutor(max_workers=4) as pool:
        containers = list(pool.map(lambda e: collect_one(e, config, cache, progress), to_check))
    if progress:
        progress.close()  # every event must be delivered before the report ends the live view
    containers.sort(key=lambda c: c["ctid"])
    report = {
        "host": socket.gethostname(),
        "collector_version": VERSION,
        "started_at": started_at,
        "backup_storage": config.get("backup_storage") or None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "containers": containers,
    }

    if args.dry_run:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    req = urllib.request.Request(
        config["server_url"].rstrip("/") + "/api/report",
        data=json.dumps(report).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config['token']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Report sent ({resp.status}): {len(containers)} container(s).")
    except urllib.error.URLError as e:
        sys.exit(f"Could not send the report: {e}")


if __name__ == "__main__":
    main()
