from bastet_agent_os.workspace_hygiene import conflicts


def test_numbered_sync_conflict_is_detected_without_deleting_it(tmp_path):
    assets = tmp_path / "src/bastet_agent_os/ui_dist/assets"
    assets.mkdir(parents=True)
    canonical = assets / "index-abc.js"
    duplicate = assets / "index-abc 3.js"
    canonical.write_text("same")
    duplicate.write_text("same")
    assert conflicts(tmp_path) == [(duplicate, canonical)]
    assert duplicate.exists()
