"""User-interface translations (English and French) and language selection."""

SUPPORTED = ("en", "fr")
# Name of each language in that language, shown in the page's language selector.
LANGUAGE_NAMES = {"en": "English", "fr": "Français"}
DEFAULT = "en"
COOKIE = "lang"

STRINGS = {
    "en": {
        "page_title": "LXC updates",
        "no_report": "No report received yet. Run the collector on the Proxmox host.",
        "last_check": "{host} · last check: {when}",
        "check_now": "Check now",
        "checking": "Checking…",
        "check_starts": "The check starts within a minute and can take a few minutes.",
        "request_failed": "Request failed, please try again.",
        "request_refused": "Request refused.",
        "stale": "The last report is more than {hours} h old: the collector may have stopped.",
        "btn_upgrade_os": "Update system ({n})",
        "btn_update_apps": "Update applications ({n})",
        "btn_pve_scripts": "Refresh PVE scripts",
        "opt_backup": "Back up first ({storage})",
        "backup_unavailable": "Backup unavailable: <code>backup_storage</code> is not set in the collector config",
        "admin_password": "Administration password",
        "cancel": "Cancel",
        "confirm": "Confirm",
        "job_upgrade_os": "System update",
        "job_update_apps": "Application update",
        "job_update_apps_backup": "Application update (with backup)",
        "job_update_pve_scripts": "PVE scripts refresh",
        "job_pending": "waiting for the collector (up to a minute)",
        "job_running": "running…",
        "job_done_ok": "finished",
        "job_done_errors": "finished with errors",
        "console": "Live output",
        "m_pending": "To update",
        "m_packages": "System packages",
        "m_apps": "Applications",
        "m_untracked": "Untracked",
        "sec_pending": "To update",
        "sec_others": "Up to date, untracked or stopped",
        "b_stopped": "Stopped",
        "b_unknown": "Unknown",
        "b_system_ok": "System up to date",
        "b_packages_one": "{n} package",
        "b_packages_other": "{n} packages",
        "b_app_untracked": "App not tracked",
        "b_app_error": "{name}: {error}",
        "b_docker_local": "Local Docker images",
        "b_docker_outdated_one": "{n}/{total} Docker image to update",
        "b_docker_outdated_other": "{n}/{total} Docker images to update",
        "b_docker_unchecked_one": "{n}/{total} Docker image not checked",
        "b_docker_unchecked_other": "{n}/{total} Docker images not checked",
        "b_docker_ok_one": "{n} Docker image up to date",
        "b_docker_ok_other": "{n} Docker images up to date",
        "d_more": "… and {n} more",
        "d_docker_newer": "Docker images whose tag has a newer version:",
        "d_docker_update": "Update: <code>docker compose pull &amp;&amp; docker compose up -d</code> inside the LXC",
        "d_unchecked": "Not checked:",
        "d_target": "{name}: target version {latest} (source: {source})",
        "d_components": "Components:",
        "d_app_update": "Application update:",
        "version_mismatch": "The collector (v{collector}) and the web service (v{server}) differ: redeploy with deploy.ps1.",
        "date_fmt": "%Y-%m-%d %H:%M",
        "js_confirm_os": "Update the system packages of these containers, in parallel?\n\n{names}\n\n"
                         "Services may restart (Docker included).",
        "js_confirm_apps": "Update the applications of these containers with their community script?\n\n{names}\n\n"
                           "{backup}\nAffected services will restart.",
        "js_confirm_pve": "Refresh all scripts downloaded in PVE Scripts Local?\nThis can take several minutes.",
        "js_backup_yes": "A backup (vzdump) will be made before each update.",
        "js_backup_no": "No backup will be made.",
        "err.missing_version_source": "version_file or version_cmd missing",
        "err.installed_version_not_found": "installed version not found",
        "err.latest_version_unavailable": "latest version unavailable",
        "err.package_not_installed": "package not installed",
        "err.target_version_unavailable": "target version unavailable",
        "err.pinned_by_digest": "image pinned by digest",
        "err.local_digest_not_found": "local digest not found",
        "err.registry_unreachable": "registry unreachable or private",
        "api_err.bad_password": "Password refused.",
        "api_err.password_not_set": "Actions are disabled: no administration password is set on the service.",
        "api_err.unknown_action": "Unknown action.",
        "api_err.no_report": "No report received yet.",
        "api_err.job_running": "A job is already running.",
        "api_err.no_containers": "No container to update.",
        "api_err.backup_not_configured": "backup_storage is not set in the collector config.",
    },
    "fr": {
        "page_title": "Mises à jour LXC",
        "no_report": "Aucun rapport reçu pour le moment. Lance le collecteur sur l'hôte Proxmox.",
        "last_check": "{host} · dernière vérification : {when}",
        "check_now": "Vérifier maintenant",
        "checking": "Vérification en cours…",
        "check_starts": "La vérification démarre dans la minute et peut durer quelques minutes.",
        "request_failed": "Demande impossible, réessaie.",
        "request_refused": "Demande refusée.",
        "stale": "Le dernier rapport date de plus de {hours} h : le collecteur ne tourne peut-être plus.",
        "btn_upgrade_os": "Mettre à jour le système ({n})",
        "btn_update_apps": "Mettre à jour les applications ({n})",
        "btn_pve_scripts": "Actualiser les scripts PVE",
        "opt_backup": "Sauvegarder avant ({storage})",
        "backup_unavailable": "Sauvegarde indisponible : <code>backup_storage</code> absent de la config du collecteur",
        "admin_password": "Mot de passe d'administration",
        "cancel": "Annuler",
        "confirm": "Valider",
        "job_upgrade_os": "Mise à jour système",
        "job_update_apps": "Mise à jour des applications",
        "job_update_apps_backup": "Mise à jour des applications (avec sauvegarde)",
        "job_update_pve_scripts": "Actualisation des scripts PVE",
        "job_pending": "en attente du collecteur (une minute au plus)",
        "job_running": "en cours…",
        "job_done_ok": "terminée",
        "job_done_errors": "terminée avec des erreurs",
        "console": "Sortie en direct",
        "m_pending": "À mettre à jour",
        "m_packages": "Paquets système",
        "m_apps": "Applications",
        "m_untracked": "Non suivies",
        "sec_pending": "À mettre à jour",
        "sec_others": "À jour, non suivi ou arrêté",
        "b_stopped": "Arrêté",
        "b_unknown": "Inconnu",
        "b_system_ok": "Système à jour",
        "b_packages_one": "{n} paquet",
        "b_packages_other": "{n} paquets",
        "b_app_untracked": "App non suivie",
        "b_app_error": "{name} : {error}",
        "b_docker_local": "Images Docker locales",
        "b_docker_outdated_one": "{n}/{total} image Docker à mettre à jour",
        "b_docker_outdated_other": "{n}/{total} images Docker à mettre à jour",
        "b_docker_unchecked_one": "{n}/{total} image Docker non vérifiée",
        "b_docker_unchecked_other": "{n}/{total} images Docker non vérifiées",
        "b_docker_ok_one": "{n} image Docker à jour",
        "b_docker_ok_other": "{n} images Docker à jour",
        "d_more": "… et {n} autres",
        "d_docker_newer": "Images Docker avec une version plus récente de leur tag :",
        "d_docker_update": "Mise à jour : <code>docker compose pull &amp;&amp; docker compose up -d</code> dans le LXC",
        "d_unchecked": "Non vérifiées :",
        "d_target": "{name} : version cible {latest} (source : {source})",
        "d_components": "Composants :",
        "d_app_update": "Mise à jour de l'application :",
        "version_mismatch": "Le collecteur (v{collector}) et le service web (v{server}) diffèrent : "
                            "redéploie avec deploy.ps1.",
        "date_fmt": "%d/%m/%Y à %H:%M",
        "js_confirm_os": "Mettre à jour les paquets système de ces conteneurs, en parallèle ?\n\n{names}\n\n"
                         "Les services concernés peuvent redémarrer (Docker inclus).",
        "js_confirm_apps": "Mettre à jour les applications de ces conteneurs avec leur script communautaire ?\n\n"
                           "{names}\n\n{backup}\nLes services concernés seront redémarrés.",
        "js_confirm_pve": "Actualiser tous les scripts téléchargés dans PVE Scripts Local ?\n"
                          "Cela peut durer plusieurs minutes.",
        "js_backup_yes": "Une sauvegarde (vzdump) sera faite avant chaque mise à jour.",
        "js_backup_no": "Aucune sauvegarde ne sera faite.",
        "err.missing_version_source": "version_file ou version_cmd manquant",
        "err.installed_version_not_found": "version installée introuvable",
        "err.latest_version_unavailable": "dernière version indisponible",
        "err.package_not_installed": "paquet non installé",
        "err.target_version_unavailable": "version cible indisponible",
        "err.pinned_by_digest": "image épinglée par digest",
        "err.local_digest_not_found": "digest local introuvable",
        "err.registry_unreachable": "registre injoignable ou privé",
        "api_err.bad_password": "Mot de passe refusé.",
        "api_err.password_not_set": "Actions désactivées : aucun mot de passe d'administration défini sur le service.",
        "api_err.unknown_action": "Action inconnue.",
        "api_err.no_report": "Aucun rapport reçu pour le moment.",
        "api_err.job_running": "Un travail est déjà en cours.",
        "api_err.no_containers": "Aucun conteneur à mettre à jour.",
        "api_err.backup_not_configured": "backup_storage n'est pas défini dans la config du collecteur.",
    },
}

# Strings handed to the browser for the page's JavaScript.
JS_KEYS = ("checking", "check_starts", "request_failed", "request_refused", "js_confirm_os", "js_confirm_apps",
           "js_confirm_pve", "js_backup_yes", "js_backup_no")


def pick_language(cookie_value, accept_languages):
    """The language cookie wins, then the browser's Accept-Language header, then English."""
    if cookie_value in SUPPORTED:
        return cookie_value
    return accept_languages.best_match(SUPPORTED, DEFAULT) or DEFAULT


class Translator:
    """Callable that translates a key for one language and falls back to English, then to the key itself."""

    def __init__(self, lang):
        self.lang = lang if lang in SUPPORTED else DEFAULT

    def __call__(self, key, **values):
        text = STRINGS[self.lang].get(key) or STRINGS[DEFAULT].get(key, key)
        return text.format(**values) if values else text

    def plural(self, key, n, **values):
        return self(f"{key}_{'one' if n == 1 else 'other'}", n=n, **values)

    def error(self, code):
        """Translate a machine error code sent by the collector; unknown values are shown as they are."""
        return self(f"err.{code}") if f"err.{code}" in STRINGS[DEFAULT] else code

    def browser_strings(self):
        strings = {key: self(key) for key in JS_KEYS}
        strings["api_err"] = {key.split(".", 1)[1]: self(key) for key in STRINGS[DEFAULT] if key.startswith("api_err.")}
        return strings
