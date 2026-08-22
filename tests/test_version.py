"""One version number, everywhere — enforced.

The WebUI prints the version next to the title, so a release that forgets to
bump it silently lies to the user. These tests keep the Python package, the
web bundle and the changelog on the same number.
"""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from bastet_agent_os import __version__
from bastet_agent_os.config import Home
from bastet_agent_os.server import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_version_endpoint_reports_package_version(tmp_path):
    client = TestClient(create_app(Home(tmp_path / "home")), base_url="http://127.0.0.1")
    body = client.get("/api/version").json()  # no token: the login gate shows it too
    assert body == {"name": "Bastet Agent OS", "version": __version__}


def test_semver_shape():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.]+)?", __version__), __version__


def test_pyproject_reads_the_version_from_the_package():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "src/bastet_agent_os/__init__.py"' in pyproject


def test_web_bundle_version_matches():
    package = json.loads((ROOT / "web" / "package.json").read_text())
    assert package["version"] == __version__


def test_changelog_top_release_matches():
    """The newest released section must be this version — bump both together."""
    heads = re.findall(r"^## \[([^\]]+)\]", (ROOT / "CHANGELOG.md").read_text(), re.M)
    releases = [h for h in heads if h.lower() != "unreleased"]
    assert releases and releases[0] == __version__, heads[:3]


def test_progress_states_the_current_release():
    """PROGRESS.md claims a "Released: vX.Y.Z" — a claim that goes stale the
    moment we ship, which is exactly what happened at 0.30.1 (the docs were
    updated in the same release that made them wrong). Make the claim testable
    instead of trusting memory. Historical mentions elsewhere in the file are
    left alone: "landed in 0.29.1" stays true forever."""
    text = (ROOT / "PROGRESS.md").read_text()
    stated = re.search(r"^- Released: \*\*v([0-9][^*]*)\*\*", text, re.M)
    assert stated, "PROGRESS.md no longer states a current release"
    assert stated.group(1) == __version__, \
        f"PROGRESS.md says v{stated.group(1)}, package says {__version__}"
