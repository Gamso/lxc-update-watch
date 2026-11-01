# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org).

## [Unreleased]

### Added
- Optional sign-in with OpenID Connect (Authelia, Keycloak, Authentik...), turned on by `OIDC_ISSUER` in the
  web service environment: authorization code flow with PKCE, optional allow-lists of groups or users, a *Sign out*
  button in the Options menu. The collector keeps its token and nothing changes when the variable is not set.
- *Update Docker stacks* button: runs `docker compose pull` then `docker compose up -d` for the Compose stacks of the
  containers that have an image to update (found through the labels Compose puts on its containers), with an optional
  backup first and an option, on by default, to remove the old images afterwards (`docker image prune -f` only, and
  only when every stack succeeded). Containers not started by Compose are listed as to update by hand.
- Several updates in one go: tick any of system, applications and Docker stacks. They run as one job, in that order
  for each container (containers in parallel); a container is backed up once and stops at its first failed step.
- Options: *Back up before updating applications and Docker stacks* and *Remove old Docker images* (on by default).

### Changed
- The page's CSS and JavaScript moved out of `index.html` into `server/static/style.css` and `server/static/app.js`
  (loaded with the version in the URL, so a new release is never served from an old cache). The page hands its data
  to the script through one small `window.LXCW` object.
- No update asks for a confirmation any more: the button starts the job right away (the choices of the Options menu
  still apply).
- The three update buttons (system, applications, Docker stacks) are replaced by a menu with check boxes and a *Run*
  button; an entry with nothing to update is greyed out.

## [0.2.0] - 2026-10-31

### Added
- Options menu (gear icon): language selector and the *Back up before updating applications* choice, which is
  remembered by the browser.
- Console per container: one tab per container with its state (waiting, running, ok, failed), a color per
  container on the `[CT n]` prefixes in the *All* view, and the ASCII-art banners of the official scripts filtered
  out. The console stays open on the same tab when the page reloads at the end of a job.
- Live check: when a check runs, the collector reports each step of each container (system packages, application
  version, Docker images) and the page animates every row as results arrive, with a progress bar. The final report
  reloads the page with the sorted result.

### Changed
- The *Refresh PVE scripts* button moved under *Check now*, at the right of the page.
- The minimum supported Python version is 3.13 (the version Proxmox VE 9 ships), and the automated tests run on it.

### Fixed
- Refreshing the PVE scripts no longer triggers a full check of the containers afterwards; only system and
  application updates are followed by a check.
- The collector's one-minute poll no longer fails when the service drops the connection.

### Removed
- The administration password: the action buttons no longer ask for anything. `LXC_WATCH_ADMIN_PASSWORD` is ignored
  and `deploy.ps1 -Action password` is gone. Protect the page with the network or a reverse proxy instead (see the
  security notes in the README).

## [0.1.0] - 2026-10-31

First public release.

### Added
- Web page listing the LXC containers with system updates (apt, apk), application updates and Docker image updates.
- Collector for the Proxmox host (Python, standard library only) with a daily systemd timer and a one-minute
  polling timer for requests made from the page.
- Automatic detection of applications for containers created by community-scripts helper scripts and add-ons,
  following each script's own `check_for_gh_release` rule (pinned versions and tag prefixes included).
- Manual application entries: GitHub releases, apt packages, version files and commands.
- Docker image comparison against the registry digest (Docker Hub, ghcr.io, quay.io, private registries).
- Action buttons protected by an administration password: parallel system update, application update with the
  official *PVE LXC Apps Update* script and optional `vzdump` backup, refresh of the *PVE Scripts Local* scripts.
- Live console on the page, with colors, streamed from the collector.
- English and French interface, selectable on the page.
- `deploy.ps1`: guided installation, update of changed files, dry run, status and logs from Windows, with a single
  password prompt and no key installed on Proxmox.
- Project versioning (`VERSION`, changelog, versions shown in the page footer, in the report and by `deploy.ps1`)
  and an automated test suite.

[Unreleased]: https://github.com/Gamso/lxc-update-watch/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Gamso/lxc-update-watch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Gamso/lxc-update-watch/releases/tag/v0.1.0
