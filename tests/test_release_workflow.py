"""A release tag must publish every public release surface, including GitHub."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_workflow_creates_a_github_release():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "contents: write" in workflow
    assert "gh release create" in workflow
    assert "--verify-tag" in workflow
    assert "actions/download-artifact" in workflow
