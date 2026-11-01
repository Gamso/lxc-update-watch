"""Optional OIDC login: access rules, and the whole flow against a small in-process identity provider."""
import base64
import hashlib
import threading
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from joserfc import jwt
from joserfc.jwk import RSAKey
from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

import auth
from conftest import TOKEN

CLIENT_ID, CLIENT_SECRET = "lxc-update-watch", "s3cret"


def settings(**overrides):
    values = dict(issuer="http://idp.invalid", client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                  redirect_uri="http://localhost/auth/callback")
    values.update(overrides)
    return auth.Settings(**values)


DEFAULT_CLIENT = object()


class FakeClient:
    """Stands in for the Authlib client when the provider itself is not under test."""

    def __init__(self, claims=None, fail=False):
        self.claims, self.fail = claims or {}, fail

    def authorize_redirect(self, redirect_uri):
        return redirect("http://idp.invalid/authorize")

    def authorize_access_token(self):
        if self.fail:
            raise ValueError("bad state")
        return {"userinfo": dict(self.claims)}

    def userinfo(self, token):
        return {"groups": ["admins"]}


@pytest.fixture
def secured(web):
    """The web client with OIDC enabled; `login(settings, client)` can be called to change the setup."""
    import app as web_app

    def login(config=None, client=DEFAULT_CLIENT):
        """`client=None` builds the real Authlib client from the provider's discovery document."""
        auth.enable(web_app.app, config or settings(), web_app.COLLECTOR_ENDPOINTS,
                    client=FakeClient() if client is DEFAULT_CLIENT else client)
        return web

    yield login
    auth.disable(web_app.app)


def sign_in(client, name="alice"):
    with client.session_transaction() as sess:
        sess["user"] = name


# ---------------------------------------------------------------- settings

def test_not_configured_means_disabled():
    assert auth.Settings.from_env({}) is None


def test_incomplete_configuration_is_refused():
    with pytest.raises(auth.ConfigError, match="OIDC_CLIENT_ID"):
        auth.Settings.from_env({"OIDC_ISSUER": "https://auth.example.com"})


def test_settings_from_env():
    config = auth.Settings.from_env({
        "OIDC_ISSUER": "https://auth.example.com/", "OIDC_CLIENT_ID": "a", "OIDC_CLIENT_SECRET": "b",
        "OIDC_REDIRECT_URI": "https://lxc.example.com/auth/callback",
        "OIDC_ALLOWED_GROUPS": "Admins, ops;", "OIDC_ALLOWED_USERS": "Alice"})
    assert config.issuer == "https://auth.example.com"
    assert config.allowed_groups == ("admins", "ops") and config.allowed_users == ("alice",)
    assert config.scopes == "openid profile email groups"


def test_allow_lists():
    open_access = settings()
    assert auth.is_allowed(open_access, {"sub": "x"})
    groups = settings(allowed_groups=("admins",), allowed_users=("bob",))
    assert auth.is_allowed(groups, {"groups": ["Admins"]})
    assert auth.is_allowed(groups, {"preferred_username": "Bob"})
    assert not auth.is_allowed(groups, {"preferred_username": "eve", "groups": ["dev"]})
    assert auth.is_allowed(groups, {"groups": "admins"})


# ---------------------------------------------------------------- access rules

def test_login_is_off_by_default(web):
    assert web.get("/auth/login").status_code == 404
    assert web.get("/api/status").status_code == 200


def test_anonymous_browser_is_sent_to_the_login(secured):
    client = secured()
    response = client.get("/")
    assert response.status_code == 302 and response.headers["Location"].endswith("/auth/login")
    response = client.get("/api/state")
    assert response.status_code == 401 and response.get_json()["code"] == "login_required"


def test_signed_in_user_gets_the_page(secured, sample_report, token_headers):
    client = secured()
    client.post("/api/report", json=sample_report, headers=token_headers)
    sign_in(client)
    assert client.get("/").status_code == 200
    assert client.get("/api/state").status_code == 200


def test_collector_keeps_using_its_token(secured, sample_report, token_headers):
    client = secured()
    assert client.post("/api/report", json=sample_report, headers=token_headers).status_code == 200
    assert client.get("/api/jobs/pending", headers=token_headers).status_code == 200
    assert client.get("/healthz").status_code == 200
    denied = client.post("/api/report", json=sample_report)
    assert denied.status_code == 401 and denied.get_json()["code"] == "unauthorized"


def test_every_other_route_needs_a_session(secured):
    import app as web_app
    client = secured()
    open_ones = auth.OPEN_ENDPOINTS | web_app.COLLECTOR_ENDPOINTS
    for rule in web_app.app.url_map.iter_rules():
        if rule.endpoint in open_ones:
            continue
        path = rule.rule.replace("<job_id>", "x")
        for method in rule.methods & {"GET", "POST"}:
            response = client.open(path, method=method, json={} if method == "POST" else None)
            assert response.status_code in (302, 401), (rule.endpoint, method, response.status_code)


def test_every_collector_endpoint_still_requires_the_token(secured):
    import app as web_app
    client = secured()
    for rule in web_app.app.url_map.iter_rules():
        if rule.endpoint in web_app.COLLECTOR_ENDPOINTS:
            for method in rule.methods & {"GET", "POST"}:
                response = client.open(rule.rule.replace("<job_id>", "x"), method=method, json={})
                assert response.status_code == 401, rule.endpoint


def test_actions_need_a_session(secured):
    client = secured()
    assert client.post("/api/actions", json={"action": "upgrade_os"}).status_code == 401


def test_logout(secured):
    client = secured()
    sign_in(client)
    response = client.post("/auth/logout")
    assert response.status_code == 302 and response.headers["Location"].endswith("/auth/logged-out")
    assert client.get("/api/status").status_code == 401
    page = client.get("/auth/logged-out")                    # open to anonymous users: no bounce back to the provider
    assert page.status_code == 200 and 'href="/auth/login"' in page.get_data(as_text=True)
    assert "Se reconnecter" in client.get("/auth/logged-out", headers={"Accept-Language": "fr"}).get_data(as_text=True)


def test_logout_is_post_only(secured):
    assert secured().get("/auth/logout").status_code == 405


def test_page_shows_who_is_signed_in(secured, sample_report, token_headers):
    client = secured()
    client.post("/api/report", json=sample_report, headers=token_headers)
    sign_in(client, "alice")
    assert "alice" in client.get("/").get_data(as_text=True)


# ---------------------------------------------------------------- callback decisions (fake client)

def test_callback_signs_the_user_in(secured):
    client = secured(client=FakeClient({"preferred_username": "alice"}))
    assert client.get("/auth/callback").status_code == 302
    assert client.get("/api/status").status_code == 200


def test_callback_failure_signs_nobody_in(secured):
    client = secured(client=FakeClient(fail=True))
    assert client.get("/auth/callback").status_code == 400
    assert client.get("/api/status").status_code == 401


def test_callback_without_identity_is_refused(secured):
    client = secured(client=FakeClient({}))
    assert client.get("/auth/callback").status_code == 400


def test_groups_missing_from_the_id_token_are_read_from_userinfo(secured):
    client = secured(settings(allowed_groups=("admins",)), FakeClient({"preferred_username": "alice"}))
    assert client.get("/auth/callback").status_code == 302


def test_user_outside_the_allow_list_is_refused(secured):
    client = secured(settings(allowed_users=("bob",)), FakeClient({"preferred_username": "alice", "groups": []}))
    assert client.get("/auth/callback").status_code == 403
    assert client.get("/api/status").status_code == 401


# ---------------------------------------------------------------- whole flow against a real (fake) provider

class Provider:
    """Minimal OpenID Connect provider: discovery, authorization code + PKCE, signed ID token, userinfo."""

    def __init__(self, groups):
        self.groups, self.codes = groups, {}
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.flask = Flask("idp")
        self.server = make_server("127.0.0.1", 0, self.flask)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.routes()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def routes(self):
        f = self.flask

        @f.get("/.well-known/openid-configuration")
        def discovery():
            return jsonify(issuer=self.base, authorization_endpoint=self.base + "/authorize",
                           token_endpoint=self.base + "/token", jwks_uri=self.base + "/jwks",
                           userinfo_endpoint=self.base + "/userinfo", response_types_supported=["code"],
                           subject_types_supported=["public"], id_token_signing_alg_values_supported=["RS256"],
                           code_challenge_methods_supported=["S256"])

        @f.get("/jwks")
        def jwks():
            return jsonify(keys=[self.key.as_dict(private=False)])

        @f.get("/authorize")
        def authorize():
            query = request.args
            assert query["code_challenge_method"] == "S256" and query["client_id"] == CLIENT_ID
            code = f"code{len(self.codes)}"
            self.codes[code] = {"nonce": query["nonce"], "challenge": query["code_challenge"]}
            return redirect(f"{query['redirect_uri']}?code={code}&state={query['state']}")

        @f.post("/token")
        def token():
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("Authorization") != f"Basic {expected}":
                return jsonify(error="invalid_client"), 401
            entry = self.codes.pop(request.form["code"])
            digest = base64.urlsafe_b64encode(hashlib.sha256(request.form["code_verifier"].encode()).digest())
            if digest.rstrip(b"=").decode() != entry["challenge"]:
                return jsonify(error="invalid_grant"), 400
            claims = {"iss": self.base, "sub": "u1", "aud": CLIENT_ID, "iat": 1, "exp": 4_000_000_000,
                      "nonce": entry["nonce"], "preferred_username": "alice"}
            id_token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)
            return jsonify(access_token="at", token_type="Bearer", id_token=id_token, expires_in=3600)

        @f.get("/userinfo")
        def userinfo():
            return jsonify(sub="u1", preferred_username="alice", groups=self.groups)

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def provider(request):
    idp = Provider(getattr(request, "param", ["admins"]))
    yield idp
    idp.stop()


def browse(client, idp):
    """Follow the login the way a browser would: the app, then the provider, then back to the app."""
    start = client.get("/auth/login")
    assert start.status_code == 302
    to_idp = requests.get(start.headers["Location"], allow_redirects=False, timeout=5)
    assert to_idp.status_code == 302
    back = urlparse(to_idp.headers["Location"])
    return client.get(f"{back.path}?{back.query}")


def test_full_login_flow(secured, provider):
    client = secured(settings(issuer=provider.base, allowed_groups=("admins",)), client=None)
    # the client was built by Authlib from the provider's discovery document
    import app as web_app
    assert web_app.app.config["OIDC"]["client"].__class__.__name__ != "FakeClient"
    assert browse(client, provider).status_code == 302
    assert client.get("/api/status").status_code == 200


def test_full_flow_refuses_a_user_outside_the_groups(secured, provider):
    client = secured(settings(issuer=provider.base, allowed_groups=("ops",)), client=None)
    assert browse(client, provider).status_code == 403
    assert client.get("/api/status").status_code == 401


def test_forged_state_is_refused(secured, provider):
    client = secured(settings(issuer=provider.base), client=None)
    assert client.get("/auth/login").status_code == 302
    assert client.get("/auth/callback?code=code0&state=forged").status_code == 400
    assert client.get("/api/status").status_code == 401
