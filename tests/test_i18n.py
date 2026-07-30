"""The WebUI ships five locales; keep them honest.

TypeScript already fails the build when a locale file misses a key (each is
typed against zh-Hant). What it cannot catch is the other two drifts, so we
check them here: a `t("…")` call for a key nobody defined, and a raw CJK
string left hard-coded in a component instead of going through `t()`.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web" / "src"
I18N = WEB / "i18n"
LOCALES = ["zh-Hant", "zh-Hans", "en", "ja", "ko"]

KEY_RE = re.compile(r'^\s{2}"([\w.-]+)":', re.M)
# t("key"), t(`wfrole.${id}`) is dynamic and skipped
CALL_RE = re.compile(r'\bt\(\s*"([\w.-]+)"')
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def keys_of(locale: str) -> set[str]:
    return set(KEY_RE.findall((I18N / f"{locale}.ts").read_text()))


def components() -> list[Path]:
    return [p for p in WEB.rglob("*.tsx") if I18N not in p.parents]


def test_every_locale_defines_the_same_keys():
    canonical = keys_of("zh-Hant")
    assert len(canonical) > 150, "canonical dictionary looks truncated"
    for locale in LOCALES[1:]:
        assert keys_of(locale) == canonical, locale


def test_every_translated_key_exists():
    canonical = keys_of("zh-Hant")
    used = {k for p in components() for k in CALL_RE.findall(p.read_text())}
    assert used - canonical == set()


def test_enter_to_submit_goes_through_the_ime_safe_helper():
    """With a CJK input method, Enter commits the candidate being composed. A
    raw `e.key === "Enter"` handler sends a half-typed message — which is
    exactly what truncated long chat messages."""
    offenders = {p.name for p in components()
                 if 'e.key === "Enter"' in p.read_text()
                 or 'event.key === "Enter"' in p.read_text()}
    assert offenders - {"ui.tsx"} == set(), (
        "use onEnterSubmit() from ui.tsx instead of a raw Enter handler")


def test_chat_composer_owns_its_own_draft():
    """The draft must not live in a component that reloads on WS events: a
    re-render during composition drops the characters being composed."""
    chat = (WEB / "pages" / "Chat.tsx").read_text()
    assert "function Composer(" in chat
    composer = chat.split("function Composer(", 1)[1]
    assert "useState(\"\")" in composer          # draft state is local to it
    assert "onEnterSubmit(submit)" in composer


def test_no_hardcoded_cjk_outside_the_dictionaries():
    """New UI strings must land in i18n/, not inline in a component."""
    offenders = {p.name: CJK_RE.findall(p.read_text())[:3]
                 for p in [*components(), *WEB.rglob("*.ts")]
                 if I18N not in p.parents and CJK_RE.search(p.read_text())}
    assert offenders == {}
