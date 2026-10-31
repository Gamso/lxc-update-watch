import re
from pathlib import Path

import pytest
from werkzeug.datastructures import LanguageAccept

from i18n import DEFAULT, JS_KEYS, LANGUAGE_NAMES, STRINGS, SUPPORTED, Translator, pick_language

ROOT = Path(__file__).parent.parent
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_every_language_has_the_same_keys():
    reference = set(STRINGS[DEFAULT])
    for lang in SUPPORTED:
        assert set(STRINGS[lang]) == reference, f"{lang} differs from {DEFAULT}"


def test_placeholders_match_between_languages():
    for key, english in STRINGS["en"].items():
        for lang in SUPPORTED:
            assert set(PLACEHOLDER.findall(STRINGS[lang][key])) == set(PLACEHOLDER.findall(english)), (lang, key)


def test_every_language_has_a_display_name():
    assert set(LANGUAGE_NAMES) == set(SUPPORTED) == set(STRINGS)


def test_plural_keys_come_in_pairs():
    for lang in SUPPORTED:
        for key in STRINGS[lang]:
            if key.endswith("_one"):
                assert key[:-4] + "_other" in STRINGS[lang], key


def test_browser_keys_exist():
    assert set(JS_KEYS) <= set(STRINGS[DEFAULT])


def test_template_only_uses_known_keys():
    template = (ROOT / "server" / "templates" / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r"\bt\('([a-z_.]+)'", template))
    used |= {f"job_{action}" for action in ("upgrade_os", "update_apps", "update_pve_scripts")}  # built dynamically
    assert used, "no translation key found in the template"
    assert used <= set(STRINGS[DEFAULT]), used - set(STRINGS[DEFAULT])


def test_python_only_uses_known_keys():
    source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'\btr\("([a-z_]+)"', source))
    plurals = set(re.findall(r'\btr\.plural\("([a-z_]+)"', source))
    assert keys <= set(STRINGS[DEFAULT]), keys - set(STRINGS[DEFAULT])
    for base in plurals:
        assert f"{base}_one" in STRINGS[DEFAULT] and f"{base}_other" in STRINGS[DEFAULT], base


def test_collector_error_codes_are_translated():
    source = (ROOT / "collector" / "lxc_collect.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'"error"\] = "([a-z_]+)"', source)) | set(re.findall(r'"error": "([a-z_]+)"', source))
    codes |= set(re.findall(r'else "([a-z_]+_unavailable)"', source))
    assert codes, "no error code found in the collector"
    for code in codes:
        assert f"err.{code}" in STRINGS[DEFAULT], code


def test_translator_formats_and_falls_back():
    assert Translator("en")("last_check", host="pve", when="now") == "pve · last check: now"
    assert Translator("fr")("last_check", host="pve", when="maintenant") == "pve · dernière vérification : maintenant"
    assert Translator("de")("check_now") == STRINGS["en"]["check_now"]  # unsupported language -> English
    assert Translator("en")("no.such.key") == "no.such.key"


def test_plural_and_error_codes():
    en = Translator("en")
    assert en.plural("b_packages", 1) == "1 package"
    assert en.plural("b_packages", 4) == "4 packages"
    assert Translator("fr").plural("b_packages", 4) == "4 paquets"
    assert en.error("registry_unreachable") == "registry unreachable or private"
    assert en.error("some legacy text") == "some legacy text"  # old reports may carry free text


@pytest.mark.parametrize("cookie, header, expected", [
    ("fr", "en", "fr"),          # the cookie wins
    (None, "fr-FR,fr;q=0.9", "fr"),
    (None, "de,en;q=0.5", "en"),
    (None, "", "en"),
    ("xx", "fr", "fr"),          # an invalid cookie is ignored
])
def test_language_selection(cookie, header, expected):
    accept = LanguageAccept([(part.split(";")[0].strip(), 1) for part in header.split(",") if part])
    assert pick_language(cookie, accept) == expected
