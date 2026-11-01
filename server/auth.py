"""Optional single sign-on with an OpenID Connect provider (Authelia, Keycloak, Authentik...).

Disabled unless OIDC_ISSUER is set. When enabled, every page and API used by a browser requires a signed-in user;
the endpoints used by the collector keep their shared-token authentication.
"""
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from flask import jsonify, redirect, request, session, url_for

log = logging.getLogger(__name__)

SESSION_HOURS = 12
OPEN_ENDPOINTS = {"healthz", "oidc_login", "oidc_callback", "oidc_logged_out", "static"}


class ConfigError(Exception):
    pass


def split_list(value):
    return tuple(item.strip().lower() for item in (value or "").replace(";", ",").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = "openid profile email groups"
    allowed_groups: tuple = ()
    allowed_users: tuple = ()
    auth_method: str = "client_secret_basic"

    @classmethod
    def from_env(cls, env):
        """Settings from the environment, None when OIDC is not configured; raises ConfigError if incomplete."""
        issuer = env.get("OIDC_ISSUER", "").strip()
        if not issuer:
            return None
        values = {name: env.get(name, "").strip() for name in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI")}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigError("OIDC_ISSUER is set but these variables are missing: " + ", ".join(missing))
        return cls(issuer=issuer.rstrip("/"), client_id=values["OIDC_CLIENT_ID"],
                   client_secret=values["OIDC_CLIENT_SECRET"], redirect_uri=values["OIDC_REDIRECT_URI"],
                   scopes=env.get("OIDC_SCOPES", "").strip() or cls.scopes,
                   allowed_groups=split_list(env.get("OIDC_ALLOWED_GROUPS")),
                   allowed_users=split_list(env.get("OIDC_ALLOWED_USERS")),
                   auth_method=env.get("OIDC_TOKEN_AUTH_METHOD", "").strip() or cls.auth_method)


def user_name(claims):
    return str(claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "")


def is_allowed(settings, claims):
    """With no allow-list every user the provider authenticates is accepted (the provider's own policy applies)."""
    if not settings.allowed_groups and not settings.allowed_users:
        return True
    names = {str(claims.get(key, "")).lower() for key in ("preferred_username", "email", "sub")}
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(names & set(settings.allowed_users)) or bool({str(g).lower() for g in groups} & set(settings.allowed_groups))


def enable(app, settings, collector_endpoints, client=None):
    """Turn the login on. `client` replaces the Authlib client (tests)."""
    if not app.secret_key:
        # Derive the key from the collector token so there is nothing more to configure; sessions survive restarts.
        seed = os.environ.get("LXC_WATCH_SECRET_KEY") or os.environ.get("LXC_WATCH_TOKEN") or ""
        app.secret_key = hashlib.sha256(f"lxc-update-watch-session:{seed}".encode()).hexdigest() if seed else os.urandom(32)
    app.config.update(SESSION_COOKIE_NAME="lxcw_session", SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=settings.redirect_uri.startswith("https://"),
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS))
    if client is None:
        from authlib.integrations.flask_client import OAuth
        client = OAuth(app).register(
            name="oidc", client_id=settings.client_id, client_secret=settings.client_secret,
            server_metadata_url=f"{settings.issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": settings.scopes, "code_challenge_method": "S256",
                           "token_endpoint_auth_method": settings.auth_method})
    app.config["OIDC"] = {"settings": settings, "client": client, "collector": set(collector_endpoints)}


def disable(app):
    app.config["OIDC"] = None


def current_user():
    return session.get("user")


def guard():
    """before_request hook: send anonymous browsers to the login, leave the collector's endpoints alone."""
    from flask import current_app
    oidc = current_app.config.get("OIDC")
    endpoint = request.endpoint
    if not oidc or endpoint is None or endpoint in OPEN_ENDPOINTS or endpoint in oidc["collector"]:
        return None
    if session.get("user"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="login required", code="login_required"), 401
    return redirect(url_for("oidc_login"))


def login(current_app_config):
    oidc = current_app_config["OIDC"]
    return oidc["client"].authorize_redirect(oidc["settings"].redirect_uri)


def callback(current_app_config):
    """Finish the login. Returns (user, None) on success or (None, reason) with reason 'failed' or 'denied'."""
    oidc = current_app_config["OIDC"]
    settings, client = oidc["settings"], oidc["client"]
    try:
        token = client.authorize_access_token()
        claims = dict(token.get("userinfo") or {})
        if (settings.allowed_groups or settings.allowed_users) and "groups" not in claims:
            claims.update(client.userinfo(token=token))  # some providers keep the groups out of the ID token
    except Exception:  # state mismatch, bad signature, provider unreachable... never let a half login through
        log.exception("OIDC login failed")
        return None, "failed"
    name = user_name(claims)
    if not name:
        return None, "failed"
    if not is_allowed(settings, claims):
        log.warning("OIDC login refused for %s: groups received %r, allowed groups %r, allowed users %r",
                    name, claims.get("groups"), settings.allowed_groups, settings.allowed_users)
        return None, "denied"
    session.clear()
    session.permanent = True
    session["user"] = name
    return name, None
