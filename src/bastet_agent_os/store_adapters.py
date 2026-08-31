"""Authenticated status and crash-recovery adapters for mobile stores.

Binary upload paths remain explicit trusted-host commands whose structured
receipt supplies the immutable provider object identifier. These adapters observe
that exact object and can recover a receipt after provider success but before local
persistence. A narrowly scoped Google Play internal-track adapter also owns its edit,
AAB upload, validation, and safe commit. The Apple adapter promotes an already processed
build through version attachment and review submission. Status and lookup paths never
mutate a release.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

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

APPLE_SUBMITTED_REVIEW_STATES = {
    "WAITING_FOR_REVIEW",
    "IN_REVIEW",
    "UNRESOLVED_ISSUES",
    "CANCELING",
    "COMPLETING",
    "COMPLETE",
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


def _response_ok(response: httpx.Response, provider: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(response, "status_code", "unknown")
        raise StoreAdapterError(f"{provider} API request failed ({status})") from exc


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


def _apple_artifact(profile: dict[str, Any], workdir: str) -> tuple[Path, int, str, str]:
    artifact = (Path(workdir) / str(profile["artifact_path"])).resolve()
    root = Path(workdir).resolve()
    if not artifact.is_relative_to(root) or not artifact.is_file():
        raise StoreAdapterError(
            f"App Store artifact not found in delivery worktree: {profile['artifact_path']}")
    uti_by_suffix = {".ipa": "com.apple.ipa", ".pkg": "com.apple.pkg"}
    uti = uti_by_suffix.get(artifact.suffix.lower())
    if not uti:
        raise StoreAdapterError("App Store built-in uploader requires an .ipa or .pkg")
    platform = str(profile.get("platform") or "IOS").upper()
    if platform == "MAC_OS" and artifact.suffix.lower() != ".pkg":
        raise StoreAdapterError("MAC_OS build upload requires a .pkg artifact")
    if platform != "MAC_OS" and artifact.suffix.lower() != ".ipa":
        raise StoreAdapterError(f"{platform} build upload requires an .ipa artifact")
    size = artifact.stat().st_size
    if size < 1:
        raise StoreAdapterError("App Store artifact is empty")
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    configured_digest = str(profile.get("artifact_sha256") or "").lower()
    if configured_digest and configured_digest != digest:
        raise StoreAdapterError("App Store artifact SHA-256 does not match delivery profile")
    return artifact, size, digest, uti


def _state(attributes: dict[str, Any], field: str) -> str:
    value = attributes.get(field) or ""
    if isinstance(value, dict):
        return str(value.get("state") or "")
    return str(value)


def _profile_list(profile: dict[str, Any], field: str, default: str = "") -> list[str]:
    raw = profile.get(field, default)
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    return sorted({str(value).strip() for value in values if str(value).strip()})


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
        if submission.get("phase") == "build_upload":
            upload_id = quote(str(submission["build_upload_id"]), safe="")
            payload = _response_json(self.client.get(
                f"{self.apple_api_url}/v1/buildUploads/{upload_id}",
                params={
                    "fields[buildUploads]": "state,build",
                    "include": "build",
                    "fields[builds]": "processingState",
                }, headers=headers),
                "App Store Connect")
            data = payload.get("data")
            if not isinstance(data, dict) or str(data.get("id") or "") != \
                    str(submission["build_upload_id"]):
                raise StoreAdapterError("App Store Connect returned the wrong build upload")
            provider_status = _state(data.get("attributes") or {}, "state")
            if provider_status == "FAILED":
                raise StoreAdapterError("App Store Connect build upload failed")
            if provider_status not in {"AWAITING_UPLOAD", "PROCESSING", "COMPLETE"}:
                raise StoreAdapterError(
                    f"unsupported App Store build upload state: {provider_status or 'missing'}")
            included = payload.get("included") or []
            if not isinstance(included, list):
                raise StoreAdapterError("App Store Connect build upload include is invalid")
            build_states = [
                str((row.get("attributes") or {}).get("processingState") or "")
                for row in included
                if isinstance(row, dict) and row.get("type") == "builds"
            ]
            if len(build_states) > 1:
                raise StoreAdapterError("App Store Connect build upload has multiple builds")
            if build_states and build_states[0] == "INVALID":
                return _receipt(submission, profile, "rejected", "INVALID_BINARY")
            return _receipt(submission, profile, "uploaded", provider_status)
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
        build_id, version_row = self._apple_build_and_version(profile, release, headers)
        if not build_id or not version_row:
            return None
        attached = ((((version_row.get("relationships") or {}).get("build") or {})
                     .get("data") or {}).get("id"))
        if str(attached or "") != build_id:
            if attached:
                raise StoreAdapterError(
                    "App Store Connect version is attached to a different build")
            return None
        version_id = str(version_row.get("id") or "")
        fields = {
            "app_store_version_id": version_id,
            "build_number": str(profile["build_number"]),
            "build_id": build_id,
        }
        if str(profile.get("submission_adapter") or "command") == "official_api" and \
                str(profile.get("release_goal") or "published") != "uploaded":
            review = self._apple_review_for_version(
                str(profile["app_id"]), version_id, headers)
            if review is None or review[1] not in APPLE_SUBMITTED_REVIEW_STATES:
                return None
            fields["review_submission_id"] = review[0]
            fields["metadata_readiness"] = self._apple_metadata_readiness(
                profile, version_id, headers)
        return _submission_receipt(release, profile, fields)

    def _apple_build_and_version(
            self, profile: dict[str, Any], release: dict[str, Any],
            headers: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
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
            return "", None
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
            return build_id, None
        return build_id, versions[0] or {}

    def _apple_review_for_version(
            self, app_id: str, version_id: str,
            headers: dict[str, str]) -> tuple[str, str] | None:
        payload = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/apps/{quote(app_id, safe='')}/reviewSubmissions",
            params={
                "include": "items",
                "fields[reviewSubmissions]": "state,items",
                "fields[reviewSubmissionItems]": "state,appStoreVersion",
                "limit": "200",
                "limit[items]": "50",
            },
            headers=headers), "App Store Connect")
        submissions = payload.get("data") or []
        included = payload.get("included") or []
        if not isinstance(submissions, list) or not isinstance(included, list):
            raise StoreAdapterError("App Store Connect review response is invalid")
        submission_states = {
            str(row.get("id") or ""): str((row.get("attributes") or {}).get("state") or "")
            for row in submissions if isinstance(row, dict)
        }
        item_submissions: dict[str, str] = {}
        for row in submissions:
            if not isinstance(row, dict):
                continue
            submission_id = str(row.get("id") or "")
            items = (((row.get("relationships") or {}).get("items") or {})
                     .get("data") or [])
            if not isinstance(items, list):
                raise StoreAdapterError("App Store Connect review items are invalid")
            for item_ref in items:
                if isinstance(item_ref, dict) and item_ref.get("id"):
                    item_submissions[str(item_ref["id"])] = submission_id
        matches: list[tuple[str, str]] = []
        for item in included:
            if not isinstance(item, dict) or item.get("type") != "reviewSubmissionItems":
                continue
            relationships = item.get("relationships") or {}
            linked_version = (((relationships.get("appStoreVersion") or {})
                               .get("data") or {}).get("id"))
            submission_id = item_submissions.get(str(item.get("id") or ""))
            if not submission_id:
                submission_id = (((relationships.get("reviewSubmission") or {})
                                  .get("data") or {}).get("id"))
            if str(linked_version or "") == version_id and submission_id:
                submission_id = str(submission_id)
                matches.append((submission_id, submission_states.get(submission_id, "")))
        if len(matches) > 1:
            raise StoreAdapterError(
                "App Store Connect found multiple review items for the exact version")
        return matches[0] if matches else None

    def submit_app_store(
            self, profile: dict[str, Any], release: dict[str, Any],
            env: dict[str, str], workdir: str = ".") -> dict[str, Any]:
        """Promote one exact processed build to a version and optional review submission."""
        headers = self._apple_headers(env)
        build_id, version_row = self._apple_build_and_version(profile, release, headers)
        if not build_id:
            if not str(profile.get("artifact_path") or "").strip():
                raise StoreAdapterError("exact processed App Store Connect build was not found")
            return self._upload_app_store_build(profile, release, headers, workdir)
        app_id = str(profile["app_id"])
        platform = str(profile.get("platform") or "IOS").upper()
        if version_row is None:
            created = _response_json(self.client.post(
                f"{self.apple_api_url}/v1/appStoreVersions",
                json={"data": {
                    "type": "appStoreVersions",
                    "attributes": {
                        "platform": platform,
                        "versionString": str(release["version"]),
                        "releaseType": str(profile.get("apple_release_type") or "MANUAL"),
                    },
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": app_id}},
                    },
                }}, headers=headers), "App Store Connect")
            version_row = created.get("data")
            if not isinstance(version_row, dict) or not version_row.get("id"):
                raise StoreAdapterError("App Store Connect did not create a version")
        version_id = str(version_row["id"])
        attached = ((((version_row.get("relationships") or {}).get("build") or {})
                     .get("data") or {}).get("id"))
        if attached and str(attached) != build_id:
            raise StoreAdapterError(
                "App Store Connect version is attached to a different build")
        if not attached:
            _response_ok(self.client.patch(
                f"{self.apple_api_url}/v1/appStoreVersions/"
                f"{quote(version_id, safe='')}/relationships/build",
                json={"data": {"type": "builds", "id": build_id}}, headers=headers),
                "App Store Connect")
        fields = {
            "app_store_version_id": version_id,
            "build_number": str(profile["build_number"]),
            "build_id": build_id,
        }
        if str(profile.get("release_goal") or "published") == "uploaded":
            return _submission_receipt(release, profile, fields)

        fields["metadata_readiness"] = self._apple_metadata_readiness(
            profile, version_id, headers)
        review = self._apple_review_for_version(app_id, version_id, headers)
        if review is None:
            submission_payload = _response_json(self.client.post(
                f"{self.apple_api_url}/v1/reviewSubmissions",
                json={"data": {
                    "type": "reviewSubmissions",
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": app_id}},
                    },
                }}, headers=headers), "App Store Connect")
            submission = submission_payload.get("data")
            if not isinstance(submission, dict) or not submission.get("id"):
                raise StoreAdapterError("App Store Connect did not create a review submission")
            review_id = str(submission["id"])
            _response_json(self.client.post(
                f"{self.apple_api_url}/v1/reviewSubmissionItems",
                json={"data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {"data": {
                            "type": "reviewSubmissions", "id": review_id}},
                        "appStoreVersion": {"data": {
                            "type": "appStoreVersions", "id": version_id}},
                    },
                }}, headers=headers), "App Store Connect")
            review_state = str((submission.get("attributes") or {}).get("state") or "")
        else:
            review_id, review_state = review
        if review_state not in APPLE_SUBMITTED_REVIEW_STATES:
            submitted = _response_json(self.client.patch(
                f"{self.apple_api_url}/v1/reviewSubmissions/"
                f"{quote(review_id, safe='')}",
                json={"data": {
                    "type": "reviewSubmissions", "id": review_id,
                    "attributes": {"submitted": True},
                }}, headers=headers), "App Store Connect")
            state = str(((submitted.get("data") or {}).get("attributes") or {})
                        .get("state") or "")
            if state and state not in APPLE_SUBMITTED_REVIEW_STATES:
                raise StoreAdapterError(
                    f"App Store Connect review submission did not advance: {state}")
        fields["review_submission_id"] = review_id
        return _submission_receipt(release, profile, fields)

    def _apple_metadata_readiness(
            self, profile: dict[str, Any], version_id: str,
            headers: dict[str, str]) -> dict[str, Any]:
        payload = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/appStoreVersions/{quote(version_id, safe='')}",
            params={
                "include": "appStoreVersionLocalizations,appStoreReviewDetail",
                "fields[appStoreVersions]":
                    "appStoreVersionLocalizations,appStoreReviewDetail",
                "fields[appStoreVersionLocalizations]":
                    "locale,description,keywords,marketingUrl,promotionalText,"
                    "supportUrl,whatsNew",
                "fields[appStoreReviewDetails]":
                    "contactFirstName,contactLastName,contactPhone,contactEmail,"
                    "demoAccountName,demoAccountPassword,demoAccountRequired,notes",
                "limit[appStoreVersionLocalizations]": "50",
            }, headers=headers), "App Store Connect")
        data = payload.get("data")
        if not isinstance(data, dict) or str(data.get("id") or "") != version_id:
            raise StoreAdapterError(
                "App Store Connect returned the wrong version metadata object")
        included = payload.get("included") or []
        if not isinstance(included, list):
            raise StoreAdapterError("App Store Connect metadata include is invalid")
        localizations = [row for row in included if isinstance(row, dict)
                         and row.get("type") == "appStoreVersionLocalizations"]
        review_details = [row for row in included if isinstance(row, dict)
                          and row.get("type") == "appStoreReviewDetails"]
        locale_rows: dict[str, dict[str, Any]] = {}
        for row in localizations:
            attributes = row.get("attributes") or {}
            if not isinstance(attributes, dict):
                raise StoreAdapterError(
                    "App Store Connect version localization attributes are invalid")
            locale = str(attributes.get("locale") or "")
            if not locale or locale in locale_rows:
                raise StoreAdapterError(
                    "App Store Connect version localizations are ambiguous")
            localization_id = str(row.get("id") or "")
            if not localization_id:
                raise StoreAdapterError(
                    "App Store Connect version localization has no id")
            locale_rows[locale] = {**attributes, "_resource_id": localization_id}
        required_locales = _profile_list(profile, "apple_required_locales")
        checked_locales = required_locales or sorted(locale_rows)
        if not checked_locales:
            raise StoreAdapterError(
                "App Store metadata readiness failed: no version localization")
        missing_locales = sorted(set(checked_locales) - set(locale_rows))
        required_fields = _profile_list(
            profile, "apple_metadata_required_fields",
            "description,supportUrl,whatsNew")
        missing_fields = []
        for locale in checked_locales:
            attributes = locale_rows.get(locale) or {}
            for field in required_fields:
                if not str(attributes.get(field) or "").strip():
                    missing_fields.append(f"{locale}.{field}")
        if len(review_details) != 1:
            missing_fields.append("appStoreReviewDetail")
            review_attributes: dict[str, Any] = {}
        else:
            review_attributes = review_details[0].get("attributes") or {}
            if not isinstance(review_attributes, dict):
                raise StoreAdapterError(
                    "App Store Connect review detail attributes are invalid")
            for field in ("contactFirstName", "contactLastName",
                          "contactPhone", "contactEmail"):
                if not str(review_attributes.get(field) or "").strip():
                    missing_fields.append(f"appStoreReviewDetail.{field}")
            if review_attributes.get("demoAccountRequired") is True:
                for field in ("demoAccountName", "demoAccountPassword"):
                    if not str(review_attributes.get(field) or "").strip():
                        missing_fields.append(f"appStoreReviewDetail.{field}")
        missing = [*[f"locale:{locale}" for locale in missing_locales],
                   *missing_fields]
        if missing:
            raise StoreAdapterError(
                "App Store metadata readiness failed: " + ", ".join(missing))
        app_info = self._apple_app_info_locales(profile, headers)
        version_locales = sorted(locale_rows)
        if version_locales != app_info["locales"]:
            raise StoreAdapterError(
                "App Store metadata readiness failed: localization parity "
                f"version={version_locales}, appInfo={app_info['locales']}")
        screenshots = self._apple_screenshot_readiness(
            profile, locale_rows, headers)
        return {
            "status": "ready",
            "locales": checked_locales,
            "required_fields": required_fields,
            "review_contact": True,
            "demo_account_required": review_attributes.get(
                "demoAccountRequired") is True,
            "app_info_id": app_info["id"],
            "localization_parity": True,
            "screenshots": screenshots,
        }

    def _apple_app_info_locales(
            self, profile: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        app_id = quote(str(profile["app_id"]), safe="")
        payload = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/apps/{app_id}/appInfos",
            params={"fields[appInfos]": "state", "limit": "50"},
            headers=headers), "App Store Connect")
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise StoreAdapterError("App Store Connect app infos response is invalid")
        configured_id = str(profile.get("apple_app_info_id") or "").strip()
        if configured_id:
            matches = [row for row in rows if isinstance(row, dict)
                       and str(row.get("id") or "") == configured_id]
            if len(matches) != 1:
                raise StoreAdapterError(
                    "App Store metadata readiness failed: apple_app_info_id "
                    "does not identify exactly one app info")
            app_info_id = configured_id
        elif len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id"):
            app_info_id = str(rows[0]["id"])
        else:
            raise StoreAdapterError(
                "App Store metadata readiness failed: apple_app_info_id is required "
                "when App Store Connect returns multiple app infos")
        localization_payload = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/appInfos/"
            f"{quote(app_info_id, safe='')}/appInfoLocalizations",
            params={"fields[appInfoLocalizations]": "locale", "limit": "200"},
            headers=headers), "App Store Connect")
        localizations = localization_payload.get("data") or []
        if not isinstance(localizations, list):
            raise StoreAdapterError(
                "App Store Connect app info localizations response is invalid")
        locales = []
        for row in localizations:
            attributes = row.get("attributes") if isinstance(row, dict) else None
            locale = str((attributes or {}).get("locale") or "") \
                if isinstance(attributes, dict) else ""
            if not locale or locale in locales:
                raise StoreAdapterError(
                    "App Store Connect app info localizations are ambiguous")
            locales.append(locale)
        return {"id": app_info_id, "locales": sorted(locales)}

    def _apple_screenshot_readiness(
            self, profile: dict[str, Any], locale_rows: dict[str, dict[str, Any]],
            headers: dict[str, str]) -> dict[str, Any]:
        if profile.get("apple_require_screenshots", True) is False:
            return {"required": False, "locales": {}}
        required_types = _profile_list(
            profile, "apple_required_screenshot_display_types")
        evidence: dict[str, dict[str, Any]] = {}
        missing = []
        for locale, localization in sorted(locale_rows.items()):
            localization_id = str(localization.get("_resource_id") or "")
            if not localization_id:
                raise StoreAdapterError(
                    "App Store Connect version localization has no id")
            payload = _response_json(self.client.get(
                f"{self.apple_api_url}/v1/appStoreVersionLocalizations/"
                f"{quote(localization_id, safe='')}/appScreenshotSets",
                params={
                    "include": "appScreenshots",
                    "fields[appScreenshotSets]":
                        "screenshotDisplayType,appScreenshots",
                    "fields[appScreenshots]":
                        "fileName,assetDeliveryState,appScreenshotSet",
                    "limit": "200",
                    "limit[appScreenshots]": "50",
                }, headers=headers), "App Store Connect")
            sets = payload.get("data") or []
            included = payload.get("included") or []
            if not isinstance(sets, list) or not isinstance(included, list):
                raise StoreAdapterError(
                    "App Store Connect screenshot response is invalid")
            screenshots = {
                str(row.get("id") or ""): row for row in included
                if isinstance(row, dict) and row.get("type") == "appScreenshots"
            }
            complete_by_type: dict[str, int] = {}
            for screenshot_set in sets:
                if not isinstance(screenshot_set, dict):
                    raise StoreAdapterError(
                        "App Store Connect screenshot set is invalid")
                attributes = screenshot_set.get("attributes") or {}
                relationships = screenshot_set.get("relationships") or {}
                display_type = str(attributes.get("screenshotDisplayType") or "") \
                    if isinstance(attributes, dict) else ""
                refs = (((relationships.get("appScreenshots") or {}).get("data"))
                        if isinstance(relationships, dict) else None) or []
                if not display_type or not isinstance(refs, list):
                    raise StoreAdapterError(
                        "App Store Connect screenshot set is ambiguous")
                count = 0
                for ref in refs:
                    screenshot = screenshots.get(str((ref or {}).get("id") or "")) \
                        if isinstance(ref, dict) else None
                    screenshot_attributes = (screenshot or {}).get("attributes") or {}
                    state = (screenshot_attributes.get("assetDeliveryState") or {}
                             if isinstance(screenshot_attributes, dict) else {})
                    if isinstance(state, dict) and state.get("state") == "COMPLETE":
                        count += 1
                if count:
                    complete_by_type[display_type] = \
                        complete_by_type.get(display_type, 0) + count
            if required_types:
                for display_type in required_types:
                    if not complete_by_type.get(display_type):
                        missing.append(
                            f"{locale}.appScreenshotSets.{display_type}")
            elif not complete_by_type:
                missing.append(f"{locale}.appScreenshotSets")
            evidence[locale] = {
                "complete": sum(complete_by_type.values()),
                "display_types": sorted(complete_by_type),
            }
        if missing:
            raise StoreAdapterError(
                "App Store metadata readiness failed: " + ", ".join(missing))
        return {
            "required": True,
            "required_display_types": required_types,
            "locales": evidence,
        }

    def _upload_app_store_build(
            self, profile: dict[str, Any], release: dict[str, Any],
            headers: dict[str, str], workdir: str) -> dict[str, Any]:
        artifact, artifact_size, digest, uti = _apple_artifact(profile, workdir)
        app_id = str(profile["app_id"])
        platform = str(profile.get("platform") or "IOS").upper()
        build_number = str(profile["build_number"])
        version = str(release["version"])
        uploads_payload = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/apps/{quote(app_id, safe='')}/buildUploads",
            params={
                "filter[cfBundleShortVersionString]": version,
                "filter[cfBundleVersion]": build_number,
                "filter[platform]": platform,
                "fields[buildUploads]": "cfBundleShortVersionString,cfBundleVersion,"
                "platform,state,buildUploadFiles",
                "limit": "2",
            }, headers=headers), "App Store Connect")
        uploads = uploads_payload.get("data") or []
        if not isinstance(uploads, list):
            raise StoreAdapterError("App Store Connect build uploads response is invalid")
        if len(uploads) > 1:
            raise StoreAdapterError("App Store Connect found multiple exact build uploads")
        if uploads:
            upload = uploads[0] or {}
        else:
            created = _response_json(self.client.post(
                f"{self.apple_api_url}/v1/buildUploads",
                json={"data": {
                    "type": "buildUploads",
                    "attributes": {
                        "cfBundleShortVersionString": version,
                        "cfBundleVersion": build_number,
                        "platform": platform,
                    },
                    "relationships": {
                        "app": {"data": {"type": "apps", "id": app_id}},
                    },
                }}, headers=headers), "App Store Connect")
            upload = created.get("data")
        if not isinstance(upload, dict) or not upload.get("id"):
            raise StoreAdapterError("App Store Connect did not provide a build upload id")
        upload_id = str(upload["id"])
        upload_attributes = upload.get("attributes") or {}
        upload_identity = {
            "cfBundleShortVersionString": version,
            "cfBundleVersion": build_number,
            "platform": platform,
        }
        mismatches = [
            field for field, wanted in upload_identity.items()
            if upload_attributes.get(field) is not None
            and str(upload_attributes[field]) != wanted
        ]
        if mismatches:
            raise StoreAdapterError(
                "App Store Connect build upload identity mismatch: "
                + ", ".join(mismatches))
        upload_state = _state(upload_attributes, "state")
        if upload_state == "FAILED":
            raise StoreAdapterError("exact App Store Connect build upload has failed")
        if upload_state in {"PROCESSING", "COMPLETE"}:
            return _submission_receipt(release, profile, {
                "phase": "build_upload", "build_upload_id": upload_id,
                "build_number": build_number, "artifact_sha256": digest,
            })
        if upload_state and upload_state != "AWAITING_UPLOAD":
            raise StoreAdapterError(
                f"unsupported App Store build upload state: {upload_state}")

        files_payload = _response_json(self.client.get(
            f"{self.apple_api_url}/v1/buildUploads/"
            f"{quote(upload_id, safe='')}/buildUploadFiles",
            params={"fields[buildUploadFiles]": "assetDeliveryState,fileName,fileSize,"
                    "sourceFileChecksums,uploadOperations,uti", "limit": "2"},
            headers=headers), "App Store Connect")
        files = files_payload.get("data") or []
        if not isinstance(files, list):
            raise StoreAdapterError("App Store Connect build upload files response is invalid")
        if len(files) > 1:
            raise StoreAdapterError("App Store Connect build upload has multiple asset files")
        if files:
            upload_file = files[0] or {}
        else:
            reserved = _response_json(self.client.post(
                f"{self.apple_api_url}/v1/buildUploadFiles",
                json={"data": {
                    "type": "buildUploadFiles",
                    "attributes": {
                        "fileName": artifact.name,
                        "fileSize": artifact_size,
                        "uti": uti,
                        "assetType": "ASSET",
                    },
                    "relationships": {"buildUpload": {"data": {
                        "type": "buildUploads", "id": upload_id,
                    }}},
                }}, headers=headers), "App Store Connect")
            upload_file = reserved.get("data")
        if not isinstance(upload_file, dict) or not upload_file.get("id"):
            raise StoreAdapterError("App Store Connect did not reserve a build upload file")
        attributes = upload_file.get("attributes") or {}
        if str(attributes.get("fileName") or "") != artifact.name or \
                int(attributes.get("fileSize") or 0) != artifact_size or \
                str(attributes.get("uti") or "") != uti:
            raise StoreAdapterError(
                "App Store Connect build upload file does not match local artifact")
        remote_checksum = (((attributes.get("sourceFileChecksums") or {}).get("file")
                            or {}).get("hash"))
        if remote_checksum and str(remote_checksum).lower() != digest:
            raise StoreAdapterError(
                "App Store Connect build upload file has a different SHA-256")
        file_state = _state(attributes, "assetDeliveryState")
        if file_state == "FAILED":
            raise StoreAdapterError("App Store Connect build upload asset has failed")
        if file_state not in {"UPLOAD_COMPLETE", "COMPLETE"}:
            self._upload_apple_parts(
                artifact, artifact_size, attributes.get("uploadOperations") or [],
                int(profile.get("apple_upload_parallelism") or 4))
            file_id = quote(str(upload_file["id"]), safe="")
            _response_json(self.client.patch(
                f"{self.apple_api_url}/v1/buildUploadFiles/{file_id}",
                json={"data": {
                    "type": "buildUploadFiles", "id": str(upload_file["id"]),
                    "attributes": {
                        "uploaded": True,
                        "sourceFileChecksums": {"file": {
                            "hash": digest, "algorithm": "SHA_256",
                        }},
                    },
                }}, headers=headers), "App Store Connect")
        return _submission_receipt(release, profile, {
            "phase": "build_upload", "build_upload_id": upload_id,
            "build_upload_file_id": str(upload_file["id"]),
            "build_number": build_number, "artifact_sha256": digest,
        })

    def _upload_apple_parts(
            self, artifact: Path, artifact_size: int,
            operations: Any, parallelism: int) -> None:
        if not isinstance(operations, list) or not operations:
            raise StoreAdapterError("App Store Connect returned no build upload operations")
        normalized = []
        for operation in operations:
            if not isinstance(operation, dict):
                raise StoreAdapterError("App Store Connect upload operation is invalid")
            method = str(operation.get("method") or "").upper()
            url = str(operation.get("url") or "")
            offset = int(operation.get("offset") or 0)
            length = int(operation.get("length") or 0)
            if method != "PUT" or urlparse(url).scheme != "https" or length < 1:
                raise StoreAdapterError("App Store Connect upload operation is unsafe")
            raw_headers = operation.get("requestHeaders") or []
            if not isinstance(raw_headers, list):
                raise StoreAdapterError("App Store Connect upload headers are invalid")
            upload_headers = {}
            for header in raw_headers:
                if not isinstance(header, dict) or not header.get("name"):
                    raise StoreAdapterError("App Store Connect upload header is invalid")
                name = str(header["name"])
                if name.lower() == "authorization":
                    raise StoreAdapterError(
                        "App Store Connect upload operation must not forward authorization")
                upload_headers[name] = str(header.get("value") or "")
            normalized.append((offset, length, url, upload_headers))
        normalized.sort(key=lambda row: row[0])
        cursor = 0
        for offset, length, _, _ in normalized:
            if offset != cursor or offset + length > artifact_size:
                raise StoreAdapterError(
                    "App Store Connect upload operations do not cover the artifact exactly")
            cursor += length
        if cursor != artifact_size:
            raise StoreAdapterError(
                "App Store Connect upload operations do not cover the artifact exactly")

        def upload(operation: tuple[int, int, str, dict[str, str]]) -> None:
            offset, length, url, upload_headers = operation
            with artifact.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(length)
            if len(chunk) != length:
                raise StoreAdapterError("could not read an App Store upload chunk")
            _response_ok(self.client.put(url, content=chunk, headers=upload_headers),
                         "App Store upload")

        with ThreadPoolExecutor(max_workers=min(parallelism, len(normalized))) as pool:
            list(pool.map(upload, normalized))

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
        if provider == "app_store_connect":
            return client_adapter.submit_app_store(profile, release, env, workdir)
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
