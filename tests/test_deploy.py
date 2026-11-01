from pathlib import Path

ROOT = Path(__file__).parent.parent


def listed(script):
    return {line.split("Local = '")[1].split("'")[0].replace("\\", "/")
            for line in script.splitlines() if "Local = '" in line}


def test_deploy_script_ships_every_file_of_the_service_and_the_collector():
    manifest = listed((ROOT / "deploy.ps1").read_text(encoding="utf-8"))
    shipped = {p.relative_to(ROOT).as_posix() for folder in ("server", "collector")
               for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts
               and p.suffix != ".bak" and p.name != "config.example.json"}
    assert shipped - manifest == set(), "files missing from the $Manifest of deploy.ps1"
    assert all((ROOT / path).exists() for path in manifest), manifest
