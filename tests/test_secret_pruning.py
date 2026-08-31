"""Managed credential files can be reclaimed without touching live or user files."""

import os
import time
from pathlib import Path

from bastet_agent_os import secrets_store
from bastet_agent_os.db import now


def _age(path: Path, hours: int = 48) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def test_prune_is_preview_first_and_only_removes_unreferenced_managed_files(db, tmp_path):
    live_ref = secrets_store.ensure_ref("live-value", tmp_path, "sec_live")
    old_ref = secrets_store.ensure_ref("old-value", tmp_path, "sec_old")
    live = Path(live_ref.removeprefix("file:"))
    old = Path(old_ref.removeprefix("file:"))
    _age(live)
    _age(old)
    stamp = now()
    db.write(
        "INSERT INTO resources(id,kind,name,secret_ref,created_at,updated_at) "
        "VALUES('live','secret','Live',?,?,?)", (live_ref, stamp, stamp))
    user_file = tmp_path / "secrets" / "manually-provisioned.pem"
    user_file.write_text("do not touch")
    _age(user_file)

    preview = secrets_store.prune_managed_files(
        db, tmp_path, minimum_age_s=24 * 3600)
    assert [item["name"] for item in preview["candidates"]] == [old.name]
    assert preview["removed"] == []
    assert old.exists()

    applied = secrets_store.prune_managed_files(
        db, tmp_path, apply=True, minimum_age_s=24 * 3600)
    assert [item["name"] for item in applied["removed"]] == [old.name]
    assert not old.exists()
    assert live.exists()
    assert user_file.exists()


def test_prune_preserves_new_files_and_symlinks(db, tmp_path):
    fresh_ref = secrets_store.ensure_ref("fresh", tmp_path, "sec_fresh")
    fresh = Path(fresh_ref.removeprefix("file:"))
    target = tmp_path / "outside"
    target.write_text("outside")
    link = tmp_path / "secrets" / "sec_link-deadbeef"
    link.symlink_to(target)
    _age(target)

    result = secrets_store.prune_managed_files(
        db, tmp_path, apply=True, minimum_age_s=24 * 3600)
    assert result["candidates"] == []
    assert fresh.exists()
    assert link.is_symlink()
    assert target.exists()


def test_prune_api_is_admin_audited_and_preview_does_not_delete(tmp_path):
    from fastapi.testclient import TestClient

    from bastet_agent_os.config import Home
    from bastet_agent_os.db import Db
    from bastet_agent_os.server import create_app

    home = Home(tmp_path / "home")
    home.ensure()
    orphan = home.root / "secrets" / "sec_orphan-deadbeef"
    orphan.parent.mkdir(mode=0o700, exist_ok=True)
    orphan.write_text("retired")
    _age(orphan)
    with TestClient(create_app(home), base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {home.api_token()}"
        preview = client.post("/api/secrets/prune", json={
            "apply": False, "minimum_age_hours": 24})
        assert preview.status_code == 200, preview.text
        assert preview.json()["candidates"][0]["name"] == orphan.name
        assert orphan.exists()
        applied = client.post("/api/secrets/prune", json={
            "apply": True, "minimum_age_hours": 24})
        assert applied.status_code == 200, applied.text
        assert len(applied.json()["removed"]) == 1
        assert not orphan.exists()
    check = Db(home.db_path)
    assert check.one("SELECT COUNT(*) AS n FROM audit_log WHERE action='secret.prune'")[
        "n"] == 1
    check.close()
