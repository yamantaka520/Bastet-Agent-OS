"""Authenticated status and crash-recovery adapters for mobile stores.

The upload/submission remains an explicit trusted-host command.  Its structured
receipt supplies the immutable provider object identifier; these adapters then
observe that exact object through the provider API and normalize its state.  An
optional lookup path can also recover a receipt after the provider accepted the
command but the host died before persisting its output.  Lookup never mutates a
provider object.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
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


@dataclass
class StoreStatusAdapter:
    """Provider client with injectable endpoints/transport for deterministic tests."""

    client: httpx.Client
    apple_api_url: str = APPLE_API_URL
    google_api_url: str = GOOGLE_API_URL
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
            env: dict[str, str]) -> dict[str, Any] | None:
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
        try:
            track_payload = _response_json(self.client.get(
                f"{self.google_api_url}/applications/{package}/edits/{edit_id}/tracks/{track}",
                headers=headers), "Google Play")
        finally:
            try:
                self.client.delete(
                    f"{self.google_api_url}/applications/{package}/edits/{edit_id}",
                    headers=headers)
            except httpx.HTTPError:
                pass
        wanted = str(profile["version_code"])
        matching = [release_row for release_row in (track_payload.get("releases") or [])
                    if wanted in [str(value)
                                  for value in release_row.get("versionCodes", [])]]
        if len(matching) > 1:
            raise StoreAdapterError(
                "Google Play lookup found multiple releases with the exact versionCode")
        if not matching:
            return None
        return _submission_receipt(release, profile, {"version_code": wanted})


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
        *, adapter: StoreStatusAdapter | None = None) -> dict[str, Any] | None:
    """Recover an exact provider receipt without creating or changing anything."""
    provider = str(profile.get("provider") or "")

    def lookup(client_adapter: StoreStatusAdapter) -> dict[str, Any] | None:
        if provider == "app_store_connect":
            return client_adapter.lookup_app_store_submission(profile, release, env)
        if provider == "google_play":
            return client_adapter.lookup_google_play_submission(profile, release, env)
        raise StoreAdapterError(f"no official submission lookup for provider: {provider}")

    if adapter is not None:
        return lookup(adapter)
    with httpx.Client(timeout=30) as client:
        return lookup(StoreStatusAdapter(client))


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
