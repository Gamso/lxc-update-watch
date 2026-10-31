# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org).

## [Unreleased]

## [0.2.0] - 2026-10-31

### Added
- Options menu (gear icon): language selector and the *Back up before updating applications* choice, which is
  remembered by the browser.
- Live check: when a check runs, the collector reports each step of each container (system packages, application
  version, Docker images) and the page animates every row as results arrive, with a progress bar. The final report
  reloads the page with the sorted result.

### Changed
- The *Refresh PVE scripts* button moved under *Check now*, at the right of the page, and no longer asks for the
  administration password (it only refreshes script files). It stays available when no password is set; the system
  and application update buttons still need it.
- The minimum supported Python version is 3.13 (the version Proxmox VE 9 ships), and the automated tests run on it.

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
