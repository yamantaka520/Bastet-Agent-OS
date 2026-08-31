"""Authenticated status and crash-recovery adapters for mobile stores.

Most upload/submission paths remain explicit trusted-host commands whose structured
receipt supplies the immutable provider object identifier. These adapters observe
that exact object and can recover a receipt after provider success but before local
persistence. A narrowly scoped Google Play internal-track adapter also owns its edit,
AAB upload, validation, and safe commit. Status and lookup paths never mutate a release.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


class StoreAdapterError(RuntimeError):
    pass


APPLE_API_URL = "https://api.appstoreconnect.apple.com"
GOOGLE_API_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GOOGLE_UPLOAD_URL = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

APPLE_STATES = {
    "PREPARE_FOR_SUBMISSION": "uploaded",
    "READY_FOR_REVIEW": "uploaded",
    "WAITING_FOR_REVIEW": "submitted",
    "IN_REVIEW": "submitted",
    "ACCEPTED": "approved",
    "PENDING_DEVELOPER_RELEASE": "approved",
    "PENDING_APPLE_RELEASE": "approved",
    "PROCESSING_FOR_DISTRIBUTION": "approved",
    "READY_FOR_DISTRIBUTION": "published",
    # Legacy value still appears in existing App Store Connect records.
    "READY_FOR_SALE": "published",
    "REJECTED": "rejected",
    "METADATA_REJECTED": "rejected",
    "INVALID_BINARY": "rejected",
    "DEVELOPER_REJECTED": "rejected",
}

GOOGLE_STATES = {
    "draft": "uploaded",
    "inProgress": "submitted",
    "halted": "approved",
    "completed": "published",
}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt_part(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def _apple_jwt(key_id: str, issuer_id: str, private_key: str, *, now: int) -> str:
    header = _jwt_part({"alg": "ES256", "kid": key_id, "typ": "JWT"})
    claims = _jwt_part({
        "aud": "appstoreconnect-v1",
        "exp": now + 1200,
        "iat": now,
        "iss": issuer_id,
    })
    signing_input = f"{header}.{claims}".encode()
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TypeError("not an EC private key")
        der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    except (TypeError, ValueError) as exc:
        raise StoreAdapterError("invalid App Store Connect private key") from exc
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_b64url(signature)}"


def _google_assertion(service_account: dict[str, Any], *, now: int,
                      audience: str = GOOGLE_TOKEN_URL) -> str:
    header = _jwt_part({
        "alg": "RS256",
        "kid": str(service_account.get("private_key_id") or ""),
        "typ": "JWT",
    })
    claims = _jwt_part({
        "aud": audience,
        "exp": now + 3600,
        "iat": now,
        "iss": str(service_account.get("client_email") or ""),
        "scope": GOOGLE_SCOPE,
    })
    signing_input = f"{header}.{claims}".encode()
    try:
        key = serialization.load_pem_private_key(
            str(service_account.get("private_key") or "").encode(), password=None)
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (AttributeError, TypeError, ValueError) as exc:
        raise StoreAdapterError("invalid Google Play service-account private key") from exc
    return f"{header}.{claims}.{_b64url(signature)}"


def _response_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        status = getattr(response, "status_code", "unknown")
        raise StoreAdapterError(f"{provider} API request failed ({status})") from exc
    if not isinstance(payload, dict):
        raise StoreAdapterError(f"{provider} API returned a non-object response")
    return payload


def _google_artifact(profile: dict[str, Any], workdir: str) -> tuple[Path, int, str]:
    artifact = (Path(workdir) / str(profile["artifact_path"])).resolve()
    root = Path(workdir).resolve()
    if not artifact.is_relative_to(root) or not artifact.is_file():
        raise StoreAdapterError(
            f"Google Play artifact not found in delivery worktree: {profile['artifact_path']}")
    if artifact.suffix.lower() != ".aab":
        raise StoreAdapterError("Google Play built-in submitter requires an .aab artifact")
    size = artifact.stat().st_size
    if size < 1:
        raise StoreAdapterError("Google Play AAB artifact is empty")
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    configured_digest = str(profile.get("artifact_sha256") or "").lower()
    if configured_digest and configured_digest != digest:
        raise StoreAdapterError("Google Play AAB SHA-256 does not match delivery profile")
    return artifact, size, digest


@dataclass
class StoreStatusAdapter:
    """Provider client with injectable endpoints/transport for deterministic tests."""

    client: httpx.Client
    apple_api_url: str = APPLE_API_URL
    google_api_url: str = GOOGLE_API_URL
    google_upload_url: str = GOOGLE_UPLOAD_URL
    google_token_url: str = GOOGLE_TOKEN_URL
    now: Any = time.time

    def _apple_headers(self, env: dict[str, str]) -> dict[str, str]:
        required_env = ["APP_STORE_CONNECT_KEY_ID", "APP_STORE_CONNECT_ISSUER_ID",
                        "APP_STORE_CONNECT_PRIVATE_KEY"]
        missing = [name for name in required_env if not str(env.get(name) or "").strip()]
        if missing:
            raise StoreAdapterError(
                "missing App Store Connect credentials: " + ", ".join(missing))
        token = _apple_jwt(
            env["APP_STORE_CONNECT_KEY_ID"], env["APP_STORE_CONNECT_ISSUER_ID"],
            env["APP_STORE_CONNECT_PRIVATE_KEY"], now=int(self.now()))
        return {"Authorization": f"Bearer {token}"}

    def _google_headers(self, env: dict[str, str]) -> dict[str, str]:
        raw_account = str(env.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON") or "")
        if not raw_account:
            raise StoreAdapterError(
                "missing Google Play credential: GOOGLE_PLAY_SERVICE_ACCOUNT_JSON")
        try:
            account = json.loads(raw_account)
        except json.JSONDecodeError as exc:
            raise StoreAdapterError("invalid Google Play service-account JSON") from exc
        if not isinstance(account, dict) or not account.get("client_email"):
            raise StoreAdapterError("invalid Google Play service-account JSON")
        assertion = _google_assertion(
            account, now=int(self.now()), audience=self.google_token_url)
        token_payload = _response_json(self.client.post(
            self.google_token_url,
            data={"assertion": assertion,
                  "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer"}),
            "Google OAuth")
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise StoreAdapterError("Google OAuth response has no access token")
        return {"Authorization": f"Bearer {access_token}"}

    def app_store_connect(self, profile: dict[str, Any], submission: dict[str, Any],
                          env: dict[str, str]) -> dict[str, Any]:
        headers = self._apple_headers(env)
        version_id = quote(str(submission["app_store_version_id"]), safe="")
        response = self.client.get(
            f"{self.apple_api_url}/v1/appStoreVersions/{version_id}",
            params={"include": "app"},
            headers=headers)
        payload = _response_json(response, "App Store Connect")
        data = payload.get("data")
        if not isinstance(data, dict) or str(data.get("id") or "") != \
                str(submission["app_store_version_id"]):
            raise StoreAdapterError("App Store Connect returned the wrong version object")
        app_data = (((data.get("relationships") or {}).get("app") or {}).get("data") or {})
        if str(app_data.get("id") or "") != str(profile["app_id"]):
            raise StoreAdapterError("App Store Connect version does not belong to configured app")
        attributes = data.get("attributes") or {}
        provider_status = str(attributes.get("appStoreState")
                              or attributes.get("appVersionState") or "")
        milestone = APPLE_STATES.get(provider_status)
        if not milestone:
            raise StoreAdapterError(
                f"unsupported App Store Connect state: {provider_status or 'missing'}")
        remote_version = str(attributes.get("versionString") or "")
        if remote_version and remote_version.removeprefix("v") != \
                str(submission["version"]).removeprefix("v"):
            raise StoreAdapterError("App Store Connect version string does not match submission")
        return _receipt(submission, profile, milestone, provider_status)

    def google_play(self, profile: dict[str, Any], submission: dict[str, Any],
                    env: dict[str, str]) -> dict[str, Any]:
        headers = self._google_headers(env)
        package = quote(str(profile["package_name"]), safe="")
        edit_payload = _response_json(self.client.post(
            f"{self.google_api_url}/applications/{package}/edits",
            json={}, headers=headers), "Google Play")
        edit_id = quote(str(edit_payload.get("id") or ""), safe="")
        if not edit_id:
            raise StoreAdapterError("Google Play did not create a status-read edit")
        track = quote(str(profile["track"]), safe="")
        try:
            track_payload = _response_json(self.client.get(
                f"{self.google_api_url}/applications/{package}/edits/{edit_id}/tracks/{track}",
                headers=headers), "Google Play")
        finally:
            # A read edit must never be committed. Best-effort cleanup keeps provider state tidy.
            try:
                self.client.delete(
                    f"{self.google_api_url}/applications/{package}/edits/{edit_id}",
                    headers=headers)
            except httpx.HTTPError:
                pass
        wanted = str(submission["version_code"])
        matching = [release for release in (track_payload.get("releases") or [])
                    if wanted in [str(value) for value in release.get("versionCodes", [])]]
        if len(matching) != 1:
            raise StoreAdapterError(
                "Google Play track must contain exactly one matching versionCode")
        provider_status = str(matching[0].get("status") or "")
        milestone = GOOGLE_STATES.get(provider_status)
        if not milestone:
            raise StoreAdapterError(
                f"unsupported Google Play release state: {provider_status or 'missing'}")
        return _receipt(submission, profile, milestone, provider_status)

    def lookup_app_store_submission(
            self, profile: dict[str, Any], release: dict[str, Any],
            env: dict[str, str]) -> dict[str, Any] | None:
        """Find the exact processed build already attached to an App Store version."""
        headers = self._apple_headers(env)
        app_id = str(profile["app_id"])
        version = str(release["version"])
        build_number = str(profile["build_number"])
        platform = str(profile.get("platform") or "IOS").upper()
        builds = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/builds",
            params={
                "filter[app]": app_id,
                "filter[version]": build_number,
                "filter[preReleaseVersion.version]": version,
                "filter[preReleaseVersion.platform]": platform,
                "filter[processingState]": "VALID",
                "limit": "2",
            }, headers=headers), "App Store Connect").get("data") or []
        if not isinstance(builds, list):
            raise StoreAdapterError("App Store Connect builds response is invalid")
        if len(builds) > 1:
            raise StoreAdapterError("App Store Connect lookup found multiple exact builds")
        if not builds:
            return None
        build_id = str((builds[0] or {}).get("id") or "")
        if not build_id:
            raise StoreAdapterError("App Store Connect build has no id")

        versions = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/appStoreVersions",
            params={
                "filter[app]": app_id,
                "filter[versionString]": version,
                "filter[platform]": platform,
                "include": "build",
                "limit": "2",
            }, headers=headers), "App Store Connect").get("data") or []
        if not isinstance(versions, list):
            raise StoreAdapterError("App Store Connect versions response is invalid")
        if len(versions) > 1:
            raise StoreAdapterError("App Store Connect lookup found multiple exact versions")
        if not versions:
            return None
        version_row = versions[0] or {}
        attached = ((((version_row.get("relationships") or {}).get("build") or {})
                     .get("data") or {}).get("id"))
        if str(attached or "") != build_id:
            if attached:
                raise StoreAdapterError(
                    "App Store Connect version is attached to a different build")
            return None
        return _submission_receipt(release, profile, {
            "app_store_version_id": str(version_row.get("id") or ""),
            "build_number": build_number,
        })

    def lookup_google_play_submission(
            self, profile: dict[str, Any], release: dict[str, Any],
            env: dict[str, str], workdir: str | None = None) -> dict[str, Any] | None:
        """Find an exact versionCode on the configured track without committing an edit."""
        headers = self._google_headers(env)
        package = quote(str(profile["package_name"]), safe="")
        edit_payload = _response_json(self.client.post(
            f"{self.google_api_url}/applications/{package}/edits",
            json={}, headers=headers), "Google Play")
        edit_id = quote(str(edit_payload.get("id") or ""), safe="")
        if not edit_id:
            raise StoreAdapterError("Google Play did not create a submission-lookup edit")
        track = quote(str(profile["track"]), safe="")
        artifact_digest = ""
        try:
            track_payload = _response_json(self.client.get(
                f"{self.google_api_url}/applications/{package}/edits/{edit_id}/tracks/{track}",
                headers=headers), "Google Play")
            wanted = str(profile["version_code"])
            matching = [release_row for release_row in (track_payload.get("releases") or [])
                        if wanted in [str(value)
                                      for value in release_row.get("versionCodes", [])]]
            if len(matching) > 1:
                raise StoreAdapterError(
                    "Google Play lookup found multiple releases with the exact versionCode")
            if matching and str(profile.get("submission_adapter") or "command") == \
                    "official_api":
                if workdir is None:
                    raise StoreAdapterError(
                        "built-in Google Play recovery requires the delivery worktree")
                _, _, artifact_digest = _google_artifact(profile, workdir)
                bundle_payload = _response_json(self.client.get(
                    f"{self.google_api_url}/applications/{package}/edits/"
                    f"{edit_id}/bundles", headers=headers), "Google Play")
                bundles = [row for row in (bundle_payload.get("bundles") or [])
                           if str(row.get("versionCode") or "") == wanted]
                if len(bundles) != 1 or str(bundles[0].get("sha256") or "").lower() != \
                        artifact_digest:
                    raise StoreAdapterError(
                        "Google Play recovered versionCode does not match local AAB SHA-256")
        finally:
            try:
                self.client.delete(
                    f"{self.google_api_url}/applications/{package}/edits/{edit_id}",
                    headers=headers)
            except httpx.HTTPError:
                pass
        if not matching:
            return None
        fields = {"version_code": wanted}
        if artifact_digest:
            fields["artifact_sha256"] = artifact_digest
        return _submission_receipt(release, profile, fields)

    def submit_google_play(
            self, profile: dict[str, Any], release: dict[str, Any],
            env: dict[str, str], workdir: str) -> dict[str, Any]:
        """Upload one AAB and append it to an internal track through a safe edit."""
        artifact, artifact_size, digest = _google_artifact(profile, workdir)

        headers = self._google_headers(env)
        package = quote(str(profile["package_name"]), safe="")
        edit_payload = _response_json(self.client.post(
            f"{self.google_api_url}/applications/{package}/edits",
            json={}, headers=headers), "Google Play")
        edit_id = quote(str(edit_payload.get("id") or ""), safe="")
        if not edit_id:
            raise StoreAdapterError("Google Play did not create a release edit")
        committed = False
        wanted = str(profile["version_code"])
        track_name = str(profile["track"])
        track = quote(track_name, safe="")
        marker = f"Bastet {release['version']} [{str(release['idempotency_key'])[:12]}]"
        try:
            bundles_payload = _response_json(self.client.get(
                f"{self.google_api_url}/applications/{package}/edits/{edit_id}/bundles",
                headers=headers), "Google Play")
            bundles = bundles_payload.get("bundles") or []
            matching_bundles = [row for row in bundles
                                if str(row.get("versionCode") or "") == wanted]
            if len(matching_bundles) > 1:
                raise StoreAdapterError(
                    "Google Play edit contains multiple bundles for versionCode")
            if matching_bundles:
                remote_digest = str(matching_bundles[0].get("sha256") or "").lower()
                if not remote_digest or remote_digest != digest:
                    raise StoreAdapterError(
                        "Google Play versionCode exists with a different AAB SHA-256")
            else:
                upload_headers = {
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(artifact_size),
                }
                with artifact.open("rb") as stream:
                    uploaded = _response_json(self.client.post(
                        f"{self.google_upload_url}/applications/{package}/edits/"
                        f"{edit_id}/bundles",
                        params={"uploadType": "media"}, content=stream,
                        headers=upload_headers), "Google Play")
                if str(uploaded.get("versionCode") or "") != wanted:
                    raise StoreAdapterError(
                        "uploaded Google Play AAB versionCode does not match profile")
                remote_digest = str(uploaded.get("sha256") or "").lower()
                if not remote_digest or remote_digest != digest:
                    raise StoreAdapterError(
                        "uploaded Google Play AAB SHA-256 does not match local artifact")

            track_payload = _response_json(self.client.get(
                f"{self.google_api_url}/applications/{package}/edits/{edit_id}/"
                f"tracks/{track}", headers=headers), "Google Play")
            releases = list(track_payload.get("releases") or [])
            matching_releases = [row for row in releases
                                 if wanted in [str(value)
                                               for value in row.get("versionCodes", [])]]
            if len(matching_releases) > 1:
                raise StoreAdapterError(
                    "Google Play track contains versionCode in multiple releases")
            if not matching_releases:
                releases.append({
                    "name": marker,
                    "versionCodes": [wanted],
                    "status": str(profile.get("google_release_status") or "completed"),
                })
                _response_json(self.client.put(
                    f"{self.google_api_url}/applications/{package}/edits/{edit_id}/"
                    f"tracks/{track}",
                    json={"track": track_name, "releases": releases}, headers=headers),
                    "Google Play")
                _response_json(self.client.post(
                    f"{self.google_api_url}/applications/{package}/edits/"
                    f"{edit_id}:validate", content=b"", headers=headers), "Google Play")
                _response_json(self.client.post(
                    f"{self.google_api_url}/applications/{package}/edits/"
                    f"{edit_id}:commit",
                    params={
                        "changesInReviewBehavior": "ERROR_IF_IN_REVIEW",
                        "changesNotSentForReview": str(bool(profile.get(
                            "google_changes_not_sent_for_review", True))).lower(),
                    }, content=b"", headers=headers), "Google Play")
                committed = True
            return _submission_receipt(
                release, profile, {"version_code": wanted, "artifact_sha256": digest})
        finally:
            if not committed:
                try:
                    self.client.delete(
                        f"{self.google_api_url}/applications/{package}/edits/{edit_id}",
                        headers=headers)
                except httpx.HTTPError:
                    pass


def _receipt(submission: dict[str, Any], profile: dict[str, Any], milestone: str,
             provider_status: str) -> dict[str, Any]:
    provider = str(profile["provider"])
    identity = ({"app_id": str(profile["app_id"])}
                if provider == "app_store_connect" else {
                    "package_name": str(profile["package_name"]),
                    "track": str(profile["track"]),
                })
    return {
        "provider": provider,
        **identity,
        "target": str(submission["target"]),
        "version": str(submission["version"]),
        "commit_sha": str(submission["commit_sha"]),
        "milestone": milestone,
        "provider_status": provider_status,
    }


def _submission_receipt(release: dict[str, Any], profile: dict[str, Any],
                        provider_fields: dict[str, Any]) -> dict[str, Any]:
    provider = str(profile["provider"])
    identity = ({"app_id": str(profile["app_id"])}
                if provider == "app_store_connect" else {
                    "package_name": str(profile["package_name"]),
                    "track": str(profile["track"]),
                })
    return {
        "provider": provider,
        **identity,
        "target": str(release["target"]),
        "version": str(release["version"]),
        "commit_sha": str(release["commit_sha"]),
        "idempotency_key": str(release["idempotency_key"]),
        **provider_fields,
    }


def official_submission_lookup(
        profile: dict[str, Any], release: dict[str, Any], env: dict[str, str],
        workdir: str | None = None, *, adapter: StoreStatusAdapter | None = None,
        ) -> dict[str, Any] | None:
    """Recover an exact provider receipt without creating or changing anything."""
    provider = str(profile.get("provider") or "")

    def lookup(client_adapter: StoreStatusAdapter) -> dict[str, Any] | None:
        if provider == "app_store_connect":
            return client_adapter.lookup_app_store_submission(profile, release, env)
        if provider == "google_play":
            return client_adapter.lookup_google_play_submission(
                profile, release, env, workdir)
        raise StoreAdapterError(f"no official submission lookup for provider: {provider}")

    if adapter is not None:
        return lookup(adapter)
    with httpx.Client(timeout=30) as client:
        return lookup(StoreStatusAdapter(client))


def official_submit(
        profile: dict[str, Any], release: dict[str, Any], env: dict[str, str],
        workdir: str, *, adapter: StoreStatusAdapter | None = None) -> dict[str, Any]:
    """Run a narrowly scoped, human-approved provider mutation."""
    provider = str(profile.get("provider") or "")

    def submit(client_adapter: StoreStatusAdapter) -> dict[str, Any]:
        if provider == "google_play":
            return client_adapter.submit_google_play(profile, release, env, workdir)
        raise StoreAdapterError(f"no built-in submitter for provider: {provider}")

    if adapter is not None:
        return submit(adapter)
    with httpx.Client(timeout=180) as client:
        return submit(StoreStatusAdapter(client))


def official_status(profile: dict[str, Any], submission: dict[str, Any],
                    env: dict[str, str], *, adapter: StoreStatusAdapter | None = None,
                    ) -> dict[str, Any]:
    provider = str(profile.get("provider") or "")
    if adapter is not None:
        if provider == "app_store_connect":
            return adapter.app_store_connect(profile, submission, env)
        if provider == "google_play":
            return adapter.google_play(profile, submission, env)
        raise StoreAdapterError(f"no official status adapter for provider: {provider}")
    with httpx.Client(timeout=30) as client:
        live = StoreStatusAdapter(client)
        if provider == "app_store_connect":
            return live.app_store_connect(profile, submission, env)
        if provider == "google_play":
            return live.google_play(profile, submission, env)
    raise StoreAdapterError(f"no official status adapter for provider: {provider}")
