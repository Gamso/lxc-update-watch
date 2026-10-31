<#
.SYNOPSIS
    Deploys, configures and checks lxc-update-watch on Proxmox, from Windows.

.DESCRIPTION
    Without arguments: interactive menu. With -Action: one specific step, no menu.
    The Proxmox password is asked once at startup. It stays in memory for the duration of the script, is never
    written to disk and nothing is installed on Proxmox. (A permanent SSH key is possible but optional: ssh-key.)

    ssh-key    (optional) install a permanent SSH key on the Proxmox host
    collector  deploy the collector and its systemd units on the host
    service    deploy the web page in its LXC (first installation included)
    config     create or check /etc/lxc-update-watch.json (token, address, missing keys)
    password   set the administration password of the action buttons
    dry-run    collect without sending anything and print a summary
    run-now    collect and send the report to the page
    probe-pve  list the scripts PVE Scripts Local would refresh (changes nothing)
    status     services, timers, web page, versions and deployed files
    logs       latest collector logs
    update     redeploy only what differs from your local copy
    full       complete installation, step by step

.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -Action update
#>
[CmdletBinding()]
param(
    [ValidateSet('menu', 'ssh-key', 'collector', 'service', 'config', 'password', 'dry-run', 'run-now',
                 'probe-pve', 'status', 'logs', 'update', 'full')]
    [string]$Action = 'menu',
    [string]$ProxmoxHost,
    [string]$User,
    [int]$ServiceCtid
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$global:OutputEncoding = [Text.UTF8Encoding]::new($false)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SettingsFile = Join-Path $Root 'deploy.settings.json'
$Version = (Get-Content (Join-Path $Root 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1)
$SshOptions = @('-o', 'ServerAliveInterval=30', '-o', 'StrictHostKeyChecking=accept-new')
$RemoteStage = '/tmp/lxcw-deploy'
$AdminPasswordPattern = '^[A-Za-z0-9._@+=:,-]{8,}$'

# Deployed files: local path -> remote path ('host' = Proxmox host, 'ct' = the LXC of the web service)
$Manifest = @(
    @{ Component = 'collector'; Where = 'host'; Local = 'VERSION';                         Remote = '/opt/lxc-update-watch/VERSION' }
    @{ Component = 'collector'; Where = 'host'; Local = 'collector\lxc_collect.py';        Remote = '/opt/lxc-update-watch/collector/lxc_collect.py' }
    @{ Component = 'collector'; Where = 'host'; Local = 'deploy\lxc-collect.service';      Remote = '/etc/systemd/system/lxc-collect.service' }
    @{ Component = 'collector'; Where = 'host'; Local = 'deploy\lxc-collect.timer';        Remote = '/etc/systemd/system/lxc-collect.timer' }
    @{ Component = 'collector'; Where = 'host'; Local = 'deploy\lxc-collect-poll.service'; Remote = '/etc/systemd/system/lxc-collect-poll.service' }
    @{ Component = 'collector'; Where = 'host'; Local = 'deploy\lxc-collect-poll.timer';   Remote = '/etc/systemd/system/lxc-collect-poll.timer' }
    @{ Component = 'service';   Where = 'ct';   Local = 'VERSION';                         Remote = '/opt/lxc-update-watch/VERSION' }
    @{ Component = 'service';   Where = 'ct';   Local = 'server\app.py';                   Remote = '/opt/lxc-update-watch/server/app.py' }
    @{ Component = 'service';   Where = 'ct';   Local = 'server\i18n.py';                  Remote = '/opt/lxc-update-watch/server/i18n.py' }
    @{ Component = 'service';   Where = 'ct';   Local = 'server\templates\index.html';     Remote = '/opt/lxc-update-watch/server/templates/index.html' }
    @{ Component = 'service';   Where = 'ct';   Local = 'server\requirements.txt';         Remote = '/opt/lxc-update-watch/server/requirements.txt' }
    @{ Component = 'service';   Where = 'ct';   Local = 'deploy\lxc-update-watch.service'; Remote = '/opt/lxc-update-watch/deploy/lxc-update-watch.service' }
)
# Uploaded but not compared (config template)
$ExtraUploads = @('collector\config.example.json')

# ---------------------------------------------------------------- output

function Write-Title([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)    { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text)  { Write-Host "  [!]  $Text" -ForegroundColor Yellow }
function Write-Info([string]$Text)  { Write-Host "  $Text" -ForegroundColor DarkGray }

function Confirm-Step([string]$Question, [bool]$Default = $true) {
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    $answer = (Read-Host "  $Question [$hint]").Trim().ToLower()
    if (-not $answer) { return $Default }
    return $answer -in @('y', 'yes')
}

function ConvertTo-PlainText([securestring]$Secret) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ---------------------------------------------------------------- settings

function Initialize-Settings {
    $saved = @{}
    if (Test-Path $SettingsFile) {
        (Get-Content $SettingsFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $saved[$_.Name] = $_.Value }
    }
    $script:Settings = @{
        ProxmoxHost = if ($ProxmoxHost) { $ProxmoxHost } elseif ($saved.ProxmoxHost) { $saved.ProxmoxHost } else { $null }
        User        = if ($User) { $User } elseif ($saved.User) { $saved.User } else { 'root' }
        ServiceCtid = if ($ServiceCtid) { $ServiceCtid } elseif ($saved.ServiceCtid) { [int]$saved.ServiceCtid } else { 0 }
    }
    while (-not $script:Settings.ProxmoxHost) {
        $script:Settings.ProxmoxHost = (Read-Host '  Proxmox host (IP address or name)').Trim()
    }
    while (-not $script:Settings.ServiceCtid) {
        $answer = (Read-Host '  CTID of the LXC that hosts the web page').Trim()
        if ($answer -match '^\d+$') { $script:Settings.ServiceCtid = [int]$answer }
    }
    $script:Settings | ConvertTo-Json | Set-Content -Path $SettingsFile -Encoding UTF8
    $script:Target = "$($script:Settings.User)@$($script:Settings.ProxmoxHost)"
}

function Test-Prerequisites {
    foreach ($tool in 'ssh', 'scp') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "$tool not found. Enable 'OpenSSH Client' in Settings > Apps > Optional features."
        }
    }
}

function Test-KeyLogin {
    $ErrorActionPreference = 'Continue'   # an ssh error message must not become an exception
    & ssh -n -o BatchMode=yes -o ConnectTimeout=6 @SshOptions $script:Target 'true' 2>$null
    return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------- SSH

# Asks for the password once and hands it to ssh/scp through SSH_ASKPASS (environment variables of this process
# only). Does nothing when an SSH key already works.
function Connect-Proxmox {
    $ErrorActionPreference = 'Continue'
    if (Test-KeyLogin) { Write-Info 'Connected with an SSH key, no password needed.'; return }
    $helper = Join-Path ([IO.Path]::GetTempPath()) ('lxcw-askpass-' + [guid]::NewGuid().ToString('N') + '.cmd')
    Set-Content -Path $helper -Value '@powershell -NoProfile -NonInteractive -Command "[Console]::Out.WriteLine($env:LXCW_PW)"' -Encoding ASCII
    $script:AskPassHelper = $helper
    for ($try = 1; $try -le 3; $try++) {
        $secret = Read-Host "  Password of $($script:Target) (asked once, never stored)" -AsSecureString
        $env:LXCW_PW = ConvertTo-PlainText $secret
        $env:SSH_ASKPASS = $helper
        $env:SSH_ASKPASS_REQUIRE = 'force'
        $env:DISPLAY = 'lxcw:0'
        & ssh -n -o ConnectTimeout=8 @SshOptions $script:Target 'true' 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Ok 'Connected. The password will not be asked again during this run.'; return }
        Write-Warn 'Connection refused: wrong password or Proxmox unreachable.'
    }
    Disconnect-Proxmox
    if (-not (Confirm-Step 'Continue anyway, typing the password for every command?' $false)) { throw 'Cannot connect.' }
}

function Disconnect-Proxmox {
    foreach ($name in 'LXCW_PW', 'SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'DISPLAY') { Remove-Item "Env:\$name" -ErrorAction SilentlyContinue }
    if ($script:AskPassHelper) { Remove-Item $script:AskPassHelper -Force -ErrorAction SilentlyContinue; $script:AskPassHelper = $null }
}

# Runs a bash script on the Proxmox host. The script travels base64-encoded (UTF-8): quotes and accents survive
# and standard input stays free to pass a secret without putting it on the command line.
function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Script, [string]$Stdin, [switch]$Quiet, [switch]$AllowFail)
    $ErrorActionPreference = 'Continue'
    $body = "exec 2>&1`n" + ($Script -replace "`r`n", "`n")
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
    $sshArgs = @($SshOptions) + @($script:Target, "bash <(echo $b64 | base64 -d)")
    $out = New-Object System.Collections.Generic.List[string]
    $emit = { param($line) $out.Add([string]$line); if (-not $Quiet) { Write-Host "  $line" } }
    if ($PSBoundParameters.ContainsKey('Stdin')) {
        $Stdin | & ssh @sshArgs | ForEach-Object { & $emit $_ }
    } else {
        & ssh -n @sshArgs | ForEach-Object { & $emit $_ }
    }
    if ($LASTEXITCODE -ne 0 -and -not $AllowFail) { throw "The remote command failed (exit code $LASTEXITCODE)." }
    return , $out.ToArray()
}

function Send-Files {
    $ErrorActionPreference = 'Continue'
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('lxcw-' + [guid]::NewGuid().ToString('N'))
    try {
        $relative = @($Manifest | ForEach-Object { $_.Local } | Select-Object -Unique) + $ExtraUploads
        foreach ($rel in $relative) {
            $source = Join-Path $Root $rel
            if (-not (Test-Path $source)) { throw "Local file not found: $rel" }
            $dest = Join-Path $stage $rel
            New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
            Copy-Item $source $dest
        }
        Invoke-Remote "rm -rf $RemoteStage" -Quiet | Out-Null
        & scp -r -q @SshOptions $stage "$($script:Target):$RemoteStage"
        if ($LASTEXITCODE -ne 0) { throw 'Could not copy the files to Proxmox (scp).' }
        Write-Ok "$($relative.Count) files sent to $($script:Target):$RemoteStage"
    } finally {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    }
}

function Test-LocalFiles {
    foreach ($item in $Manifest) {
        if (-not (Test-Path (Join-Path $Root $item.Local))) { throw "Local file not found: $($item.Local)" }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        foreach ($rel in 'collector\lxc_collect.py', 'server\app.py', 'server\i18n.py') {
            & python -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" (Join-Path $Root $rel)
            if ($LASTEXITCODE -ne 0) { throw "Python syntax error in $rel" }
        }
        Write-Ok 'Python syntax checked locally'
    } else {
        Write-Info 'Python not found locally: syntax check skipped (it is done on Proxmox too).'
    }
    Get-Content (Join-Path $Root 'collector\config.example.json') -Raw | ConvertFrom-Json | Out-Null
}

function Get-CtScript([string]$Template) {
    return $Template.Replace("`r`n", "`n").Replace('__CTID__', [string]$script:Settings.ServiceCtid).Replace('__STAGE__', $RemoteStage)
}

# ---------------------------------------------------------------- remote scripts (bash)

function Get-CollectorScript {
    return Get-CtScript @'
STAGE=__STAGE__
BASE=/opt/lxc-update-watch
D=$BASE/collector
TS=$(date +%Y%m%d%H%M%S)
set -e
install -d "$D"
if [ -f "$D/lxc_collect.py" ]; then
  cp -p "$D/lxc_collect.py" "$D/lxc_collect.py.bak-$TS"
  ls -1t "$D"/lxc_collect.py.bak-* | tail -n +4 | xargs -r rm -f
  echo "Backup: lxc_collect.py.bak-$TS"
fi
install -m 644 "$STAGE/VERSION" "$BASE/VERSION"
install -m 755 "$STAGE/collector/lxc_collect.py" "$D/lxc_collect.py"
for u in lxc-collect.service lxc-collect.timer lxc-collect-poll.service lxc-collect-poll.timer; do
  install -m 644 "$STAGE/deploy/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
python3 - "$D/lxc_collect.py" <<'PY'
import ast, sys
ast.parse(open(sys.argv[1], encoding="utf-8").read())
print("Python syntax OK on the host")
PY
if [ -f /etc/lxc-update-watch.json ] && ! grep -q 'replace-with' /etc/lxc-update-watch.json; then
  systemctl enable --now lxc-collect.timer lxc-collect-poll.timer >/dev/null 2>&1
  echo "Timers enabled (daily collection + requests from the page)"
else
  echo "Config missing or incomplete: timers not enabled (run the 'config' step)"
fi
rm -rf "$D/__pycache__"
echo "Collector version: $(python3 "$D/lxc_collect.py" --version | awk '{print $2}')"
'@
}

function Get-ServiceScript {
    return Get-CtScript @'
CT=__CTID__
STAGE=__STAGE__
TS=$(date +%Y%m%d%H%M%S)
APP=/opt/lxc-update-watch
set -e
pct status $CT | grep -q running || { echo "ERROR: CT $CT is not running."; exit 1; }
pct exec $CT -- mkdir -p $APP
pct exec $CT -- sh -c "cd $APP/server 2>/dev/null && for f in app.py i18n.py templates/index.html; do [ -f \"\$f\" ] && cp -p \"\$f\" \"\$f.bak-$TS\"; done; for f in app.py i18n.py templates/index.html; do ls -1t \"\$f\".bak-* 2>/dev/null | tail -n +4 | xargs -r rm -f; done; true"
chmod -R u=rwX,go=rX "$STAGE"
tar -C "$STAGE" -cf - server deploy VERSION | pct exec $CT -- tar -C $APP -xf -
pct exec $CT -- chmod 755 $APP
pct exec $CT -- chmod -R u=rwX,go=rX $APP/server $APP/deploy $APP/VERSION
echo "Files copied to CT $CT (read permissions set for the service user)"
if ! pct exec $CT -- test -x $APP/venv/bin/gunicorn; then
  echo "First installation: system packages and Python environment..."
  pct exec $CT -- sh -c "command -v apt-get >/dev/null || { echo 'ERROR: the CT must be Debian or Ubuntu'; exit 1; }; apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv openssl >/dev/null"
  pct exec $CT -- python3 -m venv $APP/venv
fi
pct exec $CT -- $APP/venv/bin/pip install -q -r $APP/server/requirements.txt
if ! pct exec $CT -- test -f /etc/lxc-update-watch.env; then
  pct exec $CT -- sh -c 'umask 077; echo "LXC_WATCH_TOKEN=$(openssl rand -hex 24)" > /etc/lxc-update-watch.env'
  echo "Created /etc/lxc-update-watch.env (token generated)"
fi
pct exec $CT -- cp $APP/deploy/lxc-update-watch.service /etc/systemd/system/lxc-update-watch.service
pct exec $CT -- systemctl daemon-reload
pct exec $CT -- systemctl enable lxc-update-watch >/dev/null 2>&1
pct exec $CT -- systemctl restart lxc-update-watch
sleep 2
STATE=$(pct exec $CT -- systemctl is-active lxc-update-watch || true)
echo "Service: $STATE"
[ "$STATE" = "active" ] || { pct exec $CT -- journalctl -u lxc-update-watch -n 15 --no-pager || true; exit 1; }
IP=$(pct exec $CT -- hostname -I | awk '{print $1}')
echo "Web page: http://$IP:8080"
echo "Service version: $(pct exec $CT -- cat $APP/VERSION)"
'@
}

function Get-ConfigScript {
    return Get-CtScript @'
CT=__CTID__
CFG=/etc/lxc-update-watch.json
set -e
pct status $CT | grep -q running || { echo "ERROR: CT $CT is not running."; exit 1; }
IP=$(pct exec $CT -- hostname -I | awk '{print $1}')
TOKEN=$(pct exec $CT -- sh -c "grep '^LXC_WATCH_TOKEN=' /etc/lxc-update-watch.env | cut -d= -f2-")
[ -n "$TOKEN" ] || { echo "ERROR: service token not found (run the 'service' step first)."; exit 1; }
python3 - "$CFG" "http://$IP:8080" "$TOKEN" __STAGE__/collector/config.example.json <<'PY'
import json, os, shutil, sys, time
path, url, token, example = sys.argv[1:5]
created = not os.path.exists(path)
with open(example if created else path, encoding="utf-8") as f:
    cfg = json.load(f)
if created:
    print("Config created from the template (edit the application list with nano if needed).")
else:
    print("Existing config: valid JSON.")
changed = created
if cfg.get("token") != token:
    cfg["token"] = token
    print("Token aligned with the service's token.")
    changed = True
if created or "server_url" not in cfg:
    cfg["server_url"] = url
    changed = True
elif cfg["server_url"] != url:
    print("Note: server_url is %s (the LXC answers on %s). Leave it if that is intended." % (cfg["server_url"], url))
for key, default in (("upgrade_workers", 4),):
    if key not in cfg:
        cfg[key] = default
        print("Key added: %s = %s" % (key, default))
        changed = True
if "pve_scripts_local" not in cfg:
    print("To do: add \"pve_scripts_local\": {\"url\": \"http://CT-IP:3000\"} for the PVE scripts button.")
if changed:
    if not created:
        shutil.copy2(path, "%s.bak-%s" % (path, time.strftime("%Y%m%d%H%M%S")))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(path, 0o600)
    print("File written: %s" % path)
print("Applications declared by hand: %d" % len(cfg.get("apps", {})))
PY
CODE=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$IP:8080/healthz" || true)
if [ "$CODE" = "200" ]; then echo "The service answers from the host (http://$IP:8080)"; else echo "WARNING: the service does not answer from the host (HTTP $CODE)"; fi
if ! grep -q '"backup_storage"' "$CFG"; then
  echo "Backup before application updates: add \"backup_storage\": \"NAME\" to the config. Available backup storages: $(pvesm status --content backup 2>/dev/null | awk 'NR>1{printf "%s ", $1}')"
fi
if pct exec $CT -- grep -q '^LXC_WATCH_ADMIN_PASSWORD=.\+' /etc/lxc-update-watch.env; then
  echo "Administration password: set"
else
  echo "Administration password: NOT set (the action buttons stay hidden)"
fi
if [ -f /opt/lxc-update-watch/collector/lxc_collect.py ]; then
  systemctl enable --now lxc-collect.timer lxc-collect-poll.timer >/dev/null 2>&1 && echo "Timers enabled" || echo "Timers not enabled (run the 'collector' step?)"
fi
'@
}

function Get-PasswordScript {
    return Get-CtScript @'
CT=__CTID__
IFS= read -r PW
PW=${PW%$'\r'}
PW=${PW#$'\xef\xbb\xbf'}
[[ "$PW" =~ ^[A-Za-z0-9._@+=:,-]{8,}$ ]] || { echo "ERROR: invalid password."; exit 1; }
set -e
umask 077
pct pull $CT /etc/lxc-update-watch.env /tmp/lxcw.env
{ grep -v '^LXC_WATCH_ADMIN_PASSWORD=' /tmp/lxcw.env || true; printf 'LXC_WATCH_ADMIN_PASSWORD=%s\n' "$PW"; } > /tmp/lxcw.env.new
pct push $CT /tmp/lxcw.env.new /etc/lxc-update-watch.env --perms 600
rm -f /tmp/lxcw.env /tmp/lxcw.env.new
pct exec $CT -- systemctl restart lxc-update-watch
echo "Administration password saved, service restarted."
'@
}

function Get-SshKeyScript {
    return Get-CtScript @'
IFS= read -r KEY
KEY=${KEY%$'\r'}
KEY=${KEY#$'\xef\xbb\xbf'}
case "$KEY" in ssh-*) ;; *) echo "ERROR: invalid public key."; exit 1;; esac
umask 077
mkdir -p ~/.ssh
touch ~/.ssh/authorized_keys
if grep -qF "$KEY" ~/.ssh/authorized_keys; then echo "Key already present."; else echo "$KEY" >> ~/.ssh/authorized_keys; echo "Key added."; fi
'@
}

function Get-HashScript {
    $hostPaths = ($Manifest | Where-Object { $_.Where -eq 'host' } | ForEach-Object { $_.Remote }) -join ' '
    $ctPaths = ($Manifest | Where-Object { $_.Where -eq 'ct' } | ForEach-Object { $_.Remote }) -join ' '
    $template = @'
CT=__CTID__
for p in __HOST__; do if [ -f "$p" ]; then sha256sum "$p"; else echo "MISSING  $p"; fi; done
if pct status $CT 2>/dev/null | grep -q running; then
  pct exec $CT -- sh -c 'for p in __CTFILES__; do if [ -f "$p" ]; then sha256sum "$p"; else echo "MISSING  $p"; fi; done'
else
  echo "CTDOWN"
fi
'@
    return (Get-CtScript $template).Replace('__HOST__', $hostPaths).Replace('__CTFILES__', $ctPaths)
}

function Get-StatusScript {
    return Get-CtScript @'
CT=__CTID__
echo "== Proxmox host"
echo "Collector version: $(python3 /opt/lxc-update-watch/collector/lxc_collect.py --version 2>/dev/null | awk '{print $2}')"
echo "Timers: collection=$(systemctl is-active lxc-collect.timer) requests=$(systemctl is-active lxc-collect-poll.timer)"
systemctl list-timers lxc-collect.timer lxc-collect-poll.timer --no-pager 2>/dev/null | head -4
echo "Latest collector logs:"
journalctl -u lxc-collect.service -u lxc-collect-poll.service -n 5 --no-pager -o cat 2>/dev/null
echo
echo "== Web service (CT $CT)"
pct status $CT
if pct status $CT | grep -q running; then
  echo "Service: $(pct exec $CT -- systemctl is-active lxc-update-watch)"
  IP=$(pct exec $CT -- hostname -I | awk '{print $1}')
  echo "Page: http://$IP:8080 (HTTP $(curl -s -o /dev/null -m 5 -w '%{http_code}' http://$IP:8080/healthz))"
  echo "Status: $(curl -s -m 5 http://$IP:8080/api/status)"
fi
'@
}

# ---------------------------------------------------------------- steps

function Step-SshKey {
    $ErrorActionPreference = 'Continue'
    Write-Title 'SSH key: no more password prompts'
    if (Test-KeyLogin) { Write-Ok 'Key login already works.'; return }
    $keyPath = Join-Path $env:USERPROFILE '.ssh\id_ed25519'
    if (-not (Test-Path "$keyPath.pub")) {
        New-Item -ItemType Directory -Force -Path (Split-Path $keyPath) | Out-Null
        Write-Info 'Creating an ed25519 key (no passphrase)...'
        & ssh-keygen -t ed25519 -N '""' -f $keyPath -q
        if ($LASTEXITCODE -ne 0) { throw 'ssh-keygen failed.' }
    }
    $publicKey = (Get-Content "$keyPath.pub" -Raw).Trim()
    Write-Info "Sending the public key: the password of $($script:Target) is asked one last time."
    $publicKey | & ssh @SshOptions $script:Target "bash <(echo $([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-SshKeyScript)))) | base64 -d)"
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the key.' }
    if (Test-KeyLogin) { Write-Ok 'Key login works.' } else { Write-Warn 'The key is installed but key login still fails.' }
}

function Step-Collector {
    Write-Title 'Deploying the collector (Proxmox host)'
    Test-LocalFiles
    Send-Files
    Invoke-Remote (Get-CollectorScript) | Out-Null
    Write-Ok "Collector v$Version deployed."
}

function Step-Service {
    Write-Title "Deploying the web page (CT $($script:Settings.ServiceCtid))"
    Test-LocalFiles
    Send-Files
    Invoke-Remote (Get-ServiceScript) | Out-Null
    Write-Ok "Web page v$Version deployed."
}

function Test-RemoteExists([string]$Path) {
    $result = Invoke-Remote "test -e '$Path' && echo yes || echo no" -Quiet
    return ($result -contains 'yes')
}

function Step-Config {
    Write-Title 'Collector configuration'
    if (-not (Test-RemoteExists "$RemoteStage/collector/config.example.json")) { Send-Files }
    $lines = Invoke-Remote (Get-ConfigScript)
    $script:LastConfigOutput = $lines
    Write-Info "To edit the config by hand: ssh -t $($script:Target) nano /etc/lxc-update-watch.json"
}

function Step-Password {
    Write-Title 'Administration password (action buttons)'
    Write-Info 'At least 8 characters: letters, digits and . _ @ + = : , - (no spaces or $ # " '' \).'
    $first = ConvertTo-PlainText (Read-Host '  New password' -AsSecureString)
    $second = ConvertTo-PlainText (Read-Host '  Confirm it' -AsSecureString)
    if ($first -ne $second) { Write-Warn 'The two entries differ: nothing was changed.'; return }
    if ($first -notmatch $AdminPasswordPattern) { Write-Warn 'Password rejected: too short or characters not allowed.'; return }
    Invoke-Remote (Get-PasswordScript) -Stdin $first | Out-Null
    Write-Ok 'Password saved.'
}

function Show-DryRunSummary([string[]]$Lines) {
    $text = $Lines -join "`n"
    $start = $text.IndexOf('{')
    if ($start -lt 0) { Write-Warn 'No JSON in the output.'; return }
    $json = $text.Substring($start)
    $file = Join-Path $Root ("dry-run-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
    Set-Content -Path $file -Value $json -Encoding UTF8
    $report = $json | ConvertFrom-Json

    $rows = foreach ($c in $report.containers) {
        $app = '-'
        if ($c.PSObject.Properties['app']) {
            $a = $c.app
            if ($a.PSObject.Properties['error']) { $app = "$($a.name): $($a.error)" }
            elseif ($a.update) { $app = "$($a.name) $($a.installed) -> $($a.latest)" }
            else { $app = "$($a.name) $($a.installed) (up to date)" }
        }
        $docker = '-'
        if ($c.PSObject.Properties['docker']) {
            $images = @($c.docker | Where-Object { -not $_.PSObject.Properties['local'] })
            $outdated = @($images | Where-Object { $_.PSObject.Properties['update'] -and $_.update }).Count
            $docker = "$outdated/$($images.Count) to update"
        }
        [pscustomobject]@{ CT = $c.ctid; Name = $c.name; State = $c.status; OS = $c.os; 'OS updates' = $c.os_updates; Application = $app; Docker = $docker }
    }
    $rows | Format-Table -AutoSize | Out-Host

    $withOs = @($report.containers | Where-Object { $_.PSObject.Properties['os_updates'] -and $_.os_updates -gt 0 }).Count
    $apps = @($report.containers | Where-Object { $_.PSObject.Properties['app'] -and $_.app.PSObject.Properties['update'] -and $_.app.update }).Count
    $appErrors = @($report.containers | Where-Object { $_.PSObject.Properties['app'] -and $_.app.PSObject.Properties['error'] })
    Write-Info "$($report.containers.Count) containers, $withOs with system updates, $apps with an application update."
    foreach ($e in $appErrors) { Write-Warn "CT $($e.ctid) $($e.name): $($e.app.error) (check version_file / version_cmd in the config)" }
    Write-Info "Full JSON saved: $file"
}

function Step-DryRun {
    Write-Title 'Dry run (nothing is sent)'
    $lines = Invoke-Remote 'python3 /opt/lxc-update-watch/collector/lxc_collect.py --dry-run' -Quiet
    Show-DryRunSummary $lines
}

function Step-RunNow {
    Write-Title 'Collect and send to the page'
    Invoke-Remote 'python3 /opt/lxc-update-watch/collector/lxc_collect.py' | Out-Null
}

function Step-ProbePve {
    Write-Title 'PVE Scripts Local scripts (read only)'
    Invoke-Remote 'python3 /opt/lxc-update-watch/collector/lxc_collect.py --probe-pve-scripts' -AllowFail | Out-Null
}

function Get-Drift {
    $lines = Invoke-Remote (Get-HashScript) -Quiet
    $remote = @{}
    $ctDown = $false
    foreach ($line in $lines) {
        if ($line -eq 'CTDOWN') { $ctDown = $true; continue }
        if ($line -match '^MISSING\s+(.+)$') { $remote["missing:$($Matches[1])"] = $true; continue }
        if ($line -match '^([0-9a-f]{64})\s+(.+)$') { $remote[$Matches[2]] = $Matches[1] }
    }
    foreach ($item in $Manifest) {
        $localHash = (Get-FileHash -Algorithm SHA256 (Join-Path $Root $item.Local)).Hash.ToLower()
        $state = if ($ctDown -and $item.Where -eq 'ct') { 'CT stopped' }
                 elseif (-not $remote.ContainsKey($item.Remote)) { 'missing' }
                 elseif ($remote[$item.Remote] -eq $localHash) { 'up to date' }
                 else { 'different' }
        [pscustomobject]@{ Component = $item.Component; File = $item.Local; State = $state }
    }
}

function Step-Status {
    Write-Title "Status (local version: v$Version)"
    Invoke-Remote (Get-StatusScript) | Out-Null
    Write-Title 'Deployed files (compared with your local copy)'
    $drift = Get-Drift
    $drift | Format-Table -AutoSize | Out-Host
    return $drift
}

function Step-Logs {
    Write-Title 'Collector logs'
    Invoke-Remote 'journalctl -u lxc-collect.service -u lxc-collect-poll.service -n 40 --no-pager -o short-iso' | Out-Null
}

function Step-Update {
    Write-Title "Updating what changed (local version: v$Version)"
    $drift = @(Get-Drift)
    $drift | Format-Table -AutoSize | Out-Host
    $stale = @($drift | Where-Object { $_.State -ne 'up to date' })
    if (-not $stale) { Write-Ok 'Everything is up to date.'; return }
    $components = @($stale | ForEach-Object { $_.Component } | Select-Object -Unique)
    if (-not (Confirm-Step "Redeploy: $($components -join ', ')?")) { return }
    if ($components -contains 'collector') { Step-Collector }
    if ($components -contains 'service') { Step-Service }
}

function Step-Full {
    Write-Title 'Complete installation, step by step'
    Write-Info 'Each step asks for confirmation; you can skip any of them.'
    $steps = @(
        @{ Name = 'Web page (first installation in the LXC)'; Run = { Step-Service } },
        @{ Name = 'Collector on the Proxmox host'; Run = { Step-Collector } },
        @{ Name = 'Configuration (token, service address)'; Run = { Step-Config } },
        @{ Name = 'Administration password of the action buttons'; Run = { Step-Password }; When = { $script:LastConfigOutput -match 'NOT set' } },
        @{ Name = 'Dry run (nothing is sent)'; Run = { Step-DryRun } },
        @{ Name = 'First real collection'; Run = { Step-RunNow } }
    )
    $index = 0
    foreach ($step in $steps) {
        $index++
        if ($step.When -and -not (& $step.When)) { Write-Info "Step $index/$($steps.Count) not needed: $($step.Name)"; continue }
        Write-Host ''
        Write-Host "Step $index/$($steps.Count): $($step.Name)" -ForegroundColor White
        if (-not (Confirm-Step 'Run this step?')) { Write-Info 'Step skipped.'; continue }
        & $step.Run
    }
    Write-Title 'Done'
    Write-Info 'Check the result with the "status" step, then open the web page.'
}

# ---------------------------------------------------------------- menu

function Show-Menu {
    $entries = [ordered]@{
        '1'  = @('Complete installation, step by step', { Step-Full })
        '2'  = @('Permanent SSH key (optional)', { Step-SshKey })
        '3'  = @('Deploy the collector (host)', { Step-Collector })
        '4'  = @('Deploy the web page (LXC)', { Step-Service })
        '5'  = @('Configuration (token, address)', { Step-Config })
        '6'  = @('Administration password', { Step-Password })
        '7'  = @('Dry run', { Step-DryRun })
        '8'  = @('Collect and send now', { Step-RunNow })
        '9'  = @('Test PVE Scripts Local scripts', { Step-ProbePve })
        '10' = @('Status and deployed files', { Step-Status | Out-Null })
        '11' = @('Collector logs', { Step-Logs })
        '12' = @('Update what changed', { Step-Update })
        'q'  = @('Quit', $null)
    }
    while ($true) {
        Write-Host ''
        Write-Host "lxc-update-watch v$Version -> $($script:Target), web page in CT $($script:Settings.ServiceCtid)" -ForegroundColor Cyan
        foreach ($key in $entries.Keys) { Write-Host ('  {0,-3} {1}' -f $key, $entries[$key][0]) }
        $choice = (Read-Host 'Choice').Trim().ToLower()
        if ($choice -eq 'q') { return }
        if (-not $entries.Contains($choice)) { Write-Warn 'Unknown choice.'; continue }
        try { & $entries[$choice][1] } catch { Write-Host "  [ERROR] $($_.Exception.Message)" -ForegroundColor Red }
    }
}

function Main {
    Test-Prerequisites
    Initialize-Settings
    Connect-Proxmox
    switch ($Action) {
        'menu'      { Show-Menu }
        'ssh-key'   { Step-SshKey }
        'collector' { Step-Collector }
        'service'   { Step-Service }
        'config'    { Step-Config }
        'password'  { Step-Password }
        'dry-run'   { Step-DryRun }
        'run-now'   { Step-RunNow }
        'probe-pve' { Step-ProbePve }
        'status'    { Step-Status | Out-Null }
        'logs'      { Step-Logs }
        'update'    { Step-Update }
        'full'      { Step-Full }
    }
}

# Does nothing when the file is dot-sourced (tests): . .\deploy.ps1
if ($MyInvocation.InvocationName -ne '.') {
    $failed = $false
    try { Main } catch { Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red; $failed = $true } finally { Disconnect-Proxmox }
    if ($failed) { exit 1 }
}
