from bastet_agent_os.workspace_hygiene import clean_conflicts, conflicts


def test_numbered_sync_conflict_is_detected_without_deleting_it(tmp_path):
    assets = tmp_path / "src/bastet_agent_os/ui_dist/assets"
    assets.mkdir(parents=True)
    canonical = assets / "index-abc.js"
    duplicate = assets / "index-abc 3.js"
    canonical.write_text("same")
    duplicate.write_text("same")
    assert conflicts(tmp_path) == [(duplicate, canonical)]
    assert duplicate.exists()


def test_clean_removes_only_numbered_copies_below_generated_roots(tmp_path):
    types = tmp_path / "web/node_modules/@types"
    canonical = types / "react"
    duplicate = types / "react 3"
    canonical.mkdir(parents=True)
    duplicate.mkdir()
    (canonical / "index.d.ts").write_text("canonical")
    (duplicate / "index.d.ts").write_text("stale")
    unrelated = tmp_path / "notes 3"
    unrelated.write_text("keep")

    assert clean_conflicts(tmp_path) == [duplicate]
    assert canonical.joinpath("index.d.ts").read_text() == "canonical"
    assert not duplicate.exists()
    assert unrelated.exists()
