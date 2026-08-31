"""Authenticated provider adapters bind official state to one submission receipt."""

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from bastet_agent_os.delivery import DeliveryError, _submission_receipt, validate_profile
from bastet_agent_os.store_adapters import StoreAdapterError, StoreStatusAdapter


def _decode_part(value: str) -> dict:
    value += "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value))


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _submission(provider: str) -> dict:
    base = {
        "provider": provider,
        "commit_sha": "a" * 40,
        "version": "1.4.0",
        "target": "mobile-production",
    }
    if provider == "app_store_connect":
        return {**base, "app_id": "123456", "app_store_version_id": "version-7"}
    return {
        **base,
        "package_name": "com.example.canary",
        "track": "production",
        "version_code": "10400",
    }


def test_app_store_adapter_signs_request_and_normalizes_exact_version():
    private_key = ec.generate_private_key(ec.SECP256R1())
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={
            "data": {
                "type": "appStoreVersions",
                "id": "version-7",
                "attributes": {
                    "versionString": "1.4.0",
                    "appStoreState": "IN_REVIEW",
                },
                "relationships": {"app": {"data": {"type": "apps", "id": "123456"}}},
            },
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = StoreStatusAdapter(client, apple_api_url="https://apple.test", now=lambda: 1000)
    receipt = adapter.app_store_connect(
        {"provider": "app_store_connect", "app_id": "123456"},
        _submission("app_store_connect"),
        {
            "APP_STORE_CONNECT_KEY_ID": "KEY123",
            "APP_STORE_CONNECT_ISSUER_ID": "issuer-1",
            "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
        },
    )

    request = seen["request"]
    assert str(request.url) == \
        "https://apple.test/v1/appStoreVersions/version-7?include=app"
    token = request.headers["Authorization"].removeprefix("Bearer ")
    header, claims, signature = token.split(".")
    assert _decode_part(header) == {"alg": "ES256", "kid": "KEY123", "typ": "JWT"}
    assert _decode_part(claims) == {
        "aud": "appstoreconnect-v1", "exp": 2200, "iat": 1000, "iss": "issuer-1"}
    assert len(base64.urlsafe_b64decode(signature + "==")) == 64
    assert receipt == {
        "provider": "app_store_connect",
        "app_id": "123456",
        "target": "mobile-production",
        "version": "1.4.0",
        "commit_sha": "a" * 40,
        "milestone": "submitted",
        "provider_status": "IN_REVIEW",
    }


def test_app_store_adapter_rejects_wrong_app_relationship():
    key = ec.generate_private_key(ec.SECP256R1())
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"data": {
            "id": "version-7",
            "attributes": {"appStoreState": "READY_FOR_DISTRIBUTION"},
            "relationships": {"app": {"data": {"id": "someone-elses-app"}}},
        }})))
    adapter = StoreStatusAdapter(client)

    with pytest.raises(StoreAdapterError, match="does not belong"):
        adapter.app_store_connect(
            {"provider": "app_store_connect", "app_id": "123456"},
            _submission("app_store_connect"), {
                "APP_STORE_CONNECT_KEY_ID": "key",
                "APP_STORE_CONNECT_ISSUER_ID": "issuer",
                "APP_STORE_CONNECT_PRIVATE_KEY": _pem(key),
            })


def test_google_play_adapter_uses_oauth_read_edit_and_exact_version_code():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/token":
            form = parse_qs(request.content.decode())
            assertion = form["assertion"][0]
            header, claims, signature = assertion.split(".")
            private_key.public_key().verify(
                base64.urlsafe_b64decode(signature + "=="),
                f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
            assert _decode_part(claims)["scope"] == \
                "https://www.googleapis.com/auth/androidpublisher"
            assert _decode_part(claims)["aud"] == "https://google.test/token"
            return httpx.Response(200, json={"access_token": "oauth-token"})
        assert request.headers["Authorization"] == "Bearer oauth-token"
        if path.endswith("/edits") and request.method == "POST":
            return httpx.Response(200, json={"id": "read-edit"})
        if path.endswith("/tracks/production"):
            return httpx.Response(200, json={
                "track": "production",
                "releases": [
                    {"versionCodes": ["10300"], "status": "completed"},
                    {"versionCodes": ["10400"], "status": "inProgress"},
                ],
            })
        if path.endswith("/edits/read-edit") and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = StoreStatusAdapter(
        client,
        google_api_url="https://google.test/androidpublisher/v3",
        google_token_url="https://google.test/token",
        now=lambda: 2000,
    )
    receipt = adapter.google_play(
        {"provider": "google_play", "package_name": "com.example.canary",
         "track": "production"},
        _submission("google_play"),
        {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": json.dumps({
            "client_email": "publisher@example.test",
            "private_key_id": "rsa-1",
            "private_key": _pem(private_key),
        })},
    )

    assert [request.method for request in requests] == ["POST", "POST", "GET", "DELETE"]
    assert receipt["milestone"] == "submitted"
    assert receipt["provider_status"] == "inProgress"
    assert receipt["version"] == "1.4.0"
    assert receipt["commit_sha"] == "a" * 40


def test_official_adapter_requires_exact_structured_submission_receipt():
    profile = {
        "provider": "google_play",
        "package_name": "com.example.canary",
        "track": "production",
    }
    receipt = _submission("google_play")
    receipt["commit_sha"] = "wrong"

    with pytest.raises(DeliveryError, match="submission receipt mismatch"):
        _submission_receipt(
            json.dumps(receipt), commit_sha="a" * 40, version="1.4.0",
            target="mobile-production", profile=profile)


def test_official_adapter_profile_does_not_require_verify_command():
    profile = validate_profile({
        "provider": "app_store_connect",
        "status_adapter": "official_api",
        "app_id": "123456",
        "predeploy_command": "test-release",
        "deploy_command": "upload-release",
    }, "production")

    assert profile["status_adapter"] == "official_api"
