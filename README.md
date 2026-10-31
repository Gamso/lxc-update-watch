# lxc-update-watch

A small web page that shows which of your Proxmox LXC containers have **system updates** (apt / apk), **application
updates** (community-scripts helper scripts, add-ons, GitHub releases, apt packages) and **Docker image updates**,
and can apply them from the page, with a live console and an optional backup first.

- Detects updates for containers created by the [community-scripts](https://github.com/community-scripts/ProxmoxVE)
  helper scripts with **no configuration**: it reads the scripts themselves and applies the same rule they do.
- Docker images (Docker Hub, ghcr.io, quay.io, private registries) are compared with the registry digest.
- Action buttons: system update (parallel), application update with the official
  *PVE LXC Apps Update* script (optional `vzdump` backup first), and refresh of the scripts in *PVE Scripts Local*.
- Bilingual interface (English / French), chosen with the EN / FR switch or the browser language.
- Live check: **Check now** animates each container row while its system packages, application version and Docker
  images are being checked, with a progress bar.
- Pull-based design: the Proxmox host never accepts incoming connections.

## How it works

```
 Proxmox host                                   LXC "web service"
 ┌──────────────────────────┐   report (HTTP)   ┌──────────────────────────┐
 │ collector (Python, root) │ ────────────────► │ Flask page + job queue   │
 │  - pct exec in each CT   │ ◄──────────────── │  - shows the report      │
 │  - systemd timers        │  polls every min  │  - stores action requests│
 └──────────────────────────┘                   └──────────────────────────┘
```

- `collector/lxc_collect.py` runs **on the Proxmox host** (it needs `pct exec`), once a day and whenever the page asks
  for a check or an action. Standard library only.
- `server/` is a Flask service running in **its own LXC**. It never talks to the host: the collector polls it every
  minute for requests, runs them and streams the output back.
- `deploy.ps1` installs and updates both from a Windows machine (see below).

## Requirements

- Proxmox VE 9 or any host with **Python 3.13 or newer** (Proxmox VE 9 is based on Debian 13, which ships 3.13).
  The collector uses the standard library only.
- An LXC with Python 3.13 or newer for the web service (a Debian 13 container works out of the box; Flask and
  gunicorn are installed in a virtual environment).
- Python 3.13 is the only version the automated tests run against.

## Install

### With `deploy.ps1` (Windows, recommended)

Requires the OpenSSH client (`ssh`, `scp`) and PowerShell 5.1+. Create an empty Debian/Ubuntu LXC for the web
service, then:

```powershell
.\deploy.ps1                      # interactive menu
.\deploy.ps1 -Action full         # guided installation, step by step
.\deploy.ps1 -Action update       # redeploy only what differs from your local copy
.\deploy.ps1 -Action dry-run      # collect without sending anything, readable summary
.\deploy.ps1 -Action status       # services, timers, page, versions, deployed files
```

If PowerShell refuses to run the script: `powershell -ExecutionPolicy Bypass -File .\deploy.ps1`.

- The Proxmox password is asked **once** per run, kept in memory only and handed to `ssh`/`scp` through `SSH_ASKPASS`.
  Nothing is written to disk or installed on Proxmox (a permanent SSH key is possible but optional: `-Action ssh-key`).
- On the first run the script asks for the Proxmox host (IP or name) and the CTID of the web service's LXC, then
  remembers them in `deploy.settings.json` (no secret in it, git-ignored). They can also be given with
  `-ProxmoxHost`, `-User` (default `root`) and `-ServiceCtid`, which override the saved values.
- Replaced files are backed up on Proxmox (`.bak-YYYYMMDDHHMMSS`, three kept). Your
  `/etc/lxc-update-watch.json` is never overwritten: only the token (aligned with the service's) and missing keys
  are added, with a backup.
- Other actions: `collector`, `service`, `config`, `run-now`, `probe-pve`, `logs`.

### By hand

Web service, inside the LXC:

```bash
apt install -y python3-venv openssl
mkdir -p /opt/lxc-update-watch && cd /opt/lxc-update-watch
# copy VERSION, server/ and deploy/ here
python3 -m venv venv && venv/bin/pip install -r server/requirements.txt
echo "LXC_WATCH_TOKEN=$(openssl rand -hex 24)" > /etc/lxc-update-watch.env
cp deploy/lxc-update-watch.service /etc/systemd/system/
systemctl enable --now lxc-update-watch          # page on http://<lxc-ip>:8080
```

Collector, on the Proxmox host:

```bash
mkdir -p /opt/lxc-update-watch/collector
# copy VERSION to /opt/lxc-update-watch/ and lxc_collect.py to the collector/ folder
cp collector/config.example.json /etc/lxc-update-watch.json   # then edit it (server_url, token)
cp deploy/lxc-collect.service deploy/lxc-collect.timer deploy/lxc-collect-poll.service deploy/lxc-collect-poll.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now lxc-collect.timer lxc-collect-poll.timer
python3 /opt/lxc-update-watch/collector/lxc_collect.py --dry-run     # test without sending
```

## Configuration

The collector reads `/etc/lxc-update-watch.json` (override with `LXC_WATCH_CONFIG`).

| Key | Meaning |
| --- | --- |
| `server_url`, `token` | Address of the web service and its `LXC_WATCH_TOKEN` (required). |
| `github_token` | Optional. Raises the GitHub API rate limit (60 requests/hour without it). |
| `auto_apps` | `true` by default. `false` disables the automatic helper-script detection. |
| `apps` | Manual entries, keyed by CTID. They take precedence over the automatic detection (see below). |
| `backup_storage` | Proxmox storage for the *Back up before updating applications* option (`pvesm status --content backup` lists them). Without it the option is unavailable. |
| `pve_scripts_local` | `{"url": "http://<ct-ip>:3000"}`, needed for the *Refresh PVE scripts* button. |
| `upgrade_workers` | Containers upgraded in parallel by the system update button (default 4). |
| `app_update_workers` | Containers updated in parallel by the application update button (default 2). |
| `registry_auth` | Credentials for private registries: `{"ghcr.io": {"username": "...", "token": "..."}}`. |

Environment of the web service (`/etc/lxc-update-watch.env`):

| Variable | Meaning |
| --- | --- |
| `LXC_WATCH_TOKEN` | Shared secret used by the collector (required). |
| `LXC_WATCH_DATA` | State directory (`/var/lib/lxc-update-watch` with the provided unit). |

### Automatic detection of applications

For every running container the collector:

1. finds the helper script from the container's host name (`filebrowserquantum` matches `filebrowser-quantum`),
   among `ct/` scripts and `tools/addon/` add-ons;
2. reads its `check_for_gh_release` calls: tracked component, GitHub repository, pinned version, tag prefix;
3. checks that the version file `/root/.<component>` exists in the container (otherwise the container is ignored);
4. applies the script's own rule: an update exists when the installed version differs from the pinned version or,
   if nothing is pinned, from the latest stable release.

So the page shows what the script itself would offer, and follows changes (a newly pinned version, a new component).
Scripts and the index are cached for 24 hours in `/var/cache/lxc-update-watch`.

### Manual entries (`apps`)

Only needed for what the automatic detection does not cover.

```json
"apps": {
  "103": {"name": "cloudflared", "apt_package": "cloudflared"},
  "106": {"name": "Technitium DNS", "repo": "TechnitiumSoftware/DnsServer",
          "version_file": "/root/.technitium", "update_cmd": "update"}
}
```

| Key | Meaning |
| --- | --- |
| `name` | Displayed name. |
| `apt_package` | Application installed with apt: an update exists when this package is in the pending upgrades. |
| `repo` | GitHub repository whose latest release is compared. |
| `version_file` or `version_cmd` | File, or command run inside the container, giving the installed version. |
| `version_regex` | Optional; group 1 is the version when the format is unusual. |
| `version_prefix` | Optional; only compare with releases whose version starts with this prefix (`"1."` stays on the 1.x branch). |
| `pve_script` / `pve_repo` | Optional; read the version a helper script pins instead of using the latest release. |
| `update_cmd` | Optional; command shown on the page to update the application. |

## Action buttons

A click records a request on the service; the host's timer picks it up within a minute, runs it and streams the
output to the page's console. There is no password: anyone who can open the page can start them (see the security
notes below).

- **Update system** runs `apt-get upgrade` (never `dist-upgrade`) or `apk upgrade` in containers with pending
  packages, in parallel. Services may restart, Docker included.
- **Update applications** runs the official `tools/pve/update-apps.sh` unattended, one instance per container, for
  containers created by a helper script whose application has an update. With `backup_storage` set, the
  *Back up before updating applications* option of the Options menu (gear icon, remembered by the browser) runs a
  `vzdump` before each update. Containers that need a reboot are listed in the console.
  Add-ons have no `update` command: the page shows the command to run inside the container.
- **Refresh PVE scripts** (under *Check now*) reproduces *Update all downloaded scripts* of PVE Scripts Local through its internal API
  (undocumented, so it may break with its updates; `deploy.ps1 -Action probe-pve` checks it without changing anything).

Security notes: **the page has no login**. Anyone who can open it can read the state and the console output, and
can start the update buttons, which run `apt-get upgrade` and the community update scripts as root on the host
through the collector. Only the fixed actions of the page can be started, never an arbitrary command, and requests
must be JSON so a cross-site form cannot trigger one. Keep the page on your local network, or put an authenticating
reverse proxy (Authelia, Cloudflare Access, a VPN...) in front of it before exposing it to the Internet.

## Languages

The interface is available in English and French. The language is taken from the `lang` cookie (set by the language
selector of the Options menu, the gear icon at the top right of the page), then from the browser's `Accept-Language`, then defaults to English.
Translations live in `server/i18n.py`; adding a language means adding one dictionary to `STRINGS`, its code to
`SUPPORTED` and its name to `LANGUAGE_NAMES` (a test checks that every key exists in all languages). Technical
output (console, job results, collector messages) is always in English.

## Versioning

The project follows [Semantic Versioning](https://semver.org). The single source of truth is the `VERSION` file;
the collector and the web service both read it, report their version (page footer, `--version`, `/api/status`) and
the page warns when the collector and the service run different versions. `deploy.ps1 -Action status` compares the
deployed files with your local copy.

Release process:

1. Move the notes of `## [Unreleased]` in `CHANGELOG.md` under a new `## [X.Y.Z] - date` heading, and update `VERSION`.
2. Commit (`Release X.Y.Z`), then tag and push: `git tag vX.Y.Z && git push origin main vX.Y.Z`.
3. The `release` workflow checks that the tag matches `VERSION` and publishes a GitHub release with the changelog notes.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no network and no Proxmox: the collector's `pct` calls and the GitHub requests are replaced by fakes.
