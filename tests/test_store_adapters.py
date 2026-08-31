"""Authenticated provider adapters bind official state to one submission receipt."""

import base64
import hashlib
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


def _apple_metadata(*, whats_new: str = "Safer delivery.",
                    demo_required: bool = False,
                    demo_password: str = "") -> dict:
    return {
        "data": {"type": "appStoreVersions", "id": "version-7"},
        "included": [
            {
                "type": "appStoreVersionLocalizations",
                "id": "localization-1",
                "attributes": {
                    "locale": "en-US",
                    "description": "Bastet companion app",
                    "supportUrl": "https://example.test/support",
                    "whatsNew": whats_new,
                },
            },
            {
                "type": "appStoreReviewDetails",
                "id": "review-detail-1",
                "attributes": {
                    "contactFirstName": "Bastet",
                    "contactLastName": "Release",
                    "contactPhone": "+1 555 0100",
                    "contactEmail": "release@example.test",
                    "demoAccountRequired": demo_required,
                    "demoAccountName": "reviewer" if demo_required else "",
                    "demoAccountPassword": demo_password,
                },
            },
        ],
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


def test_app_store_lookup_recovers_only_exact_attached_processed_build():
    private_key = ec.generate_private_key(ec.SECP256R1())
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/builds":
            assert request.url.params["filter[app]"] == "123456"
            assert request.url.params["filter[version]"] == "87"
            assert request.url.params["filter[preReleaseVersion.version]"] == "1.4.0"
            assert request.url.params["filter[preReleaseVersion.platform]"] == "IOS"
            assert request.url.params["filter[processingState]"] == "VALID"
            return httpx.Response(200, json={"data": [{"id": "build-87"}]})
        if request.url.path == "/v1/appStoreVersions":
            assert request.url.params["filter[versionString]"] == "1.4.0"
            return httpx.Response(200, json={"data": [{
                "id": "version-7",
                "relationships": {"build": {"data": {"id": "build-87"}}},
            }]})
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        apple_api_url="https://apple.test", now=lambda: 1000)
    receipt = adapter.lookup_app_store_submission({
        "provider": "app_store_connect",
        "app_id": "123456",
        "build_number": "87",
        "platform": "ios",
    }, {
        "commit_sha": "a" * 40,
        "version": "1.4.0",
        "target": "mobile-production",
        "idempotency_key": "stable-key",
    }, {
        "APP_STORE_CONNECT_KEY_ID": "key",
        "APP_STORE_CONNECT_ISSUER_ID": "issuer",
        "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
    })

    assert [request.method for request in requests] == ["GET", "GET"]
    assert receipt == {
        **_submission("app_store_connect"),
        "idempotency_key": "stable-key",
        "build_number": "87",
        "build_id": "build-87",
    }


def test_app_store_lookup_refuses_version_attached_to_another_build():
    private_key = ec.generate_private_key(ec.SECP256R1())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/builds":
            return httpx.Response(200, json={"data": [{"id": "build-87"}]})
        return httpx.Response(200, json={"data": [{
            "id": "version-7",
            "relationships": {"build": {"data": {"id": "different-build"}}},
        }]})

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(StoreAdapterError, match="different build"):
        adapter.lookup_app_store_submission({
            "provider": "app_store_connect", "app_id": "123456",
            "build_number": "87",
        }, {
            "commit_sha": "a" * 40, "version": "1.4.0",
            "target": "mobile-production", "idempotency_key": "stable-key",
        }, {
            "APP_STORE_CONNECT_KEY_ID": "key",
            "APP_STORE_CONNECT_ISSUER_ID": "issuer",
            "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
        })


def test_google_play_lookup_recovers_exact_track_version_without_committing():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "oauth-token"})
        if request.url.path.endswith("/edits") and request.method == "POST":
            return httpx.Response(200, json={"id": "lookup-edit"})
        if request.url.path.endswith("/tracks/internal"):
            return httpx.Response(200, json={
                "releases": [{"versionCodes": ["10400"], "status": "completed"}],
            })
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        google_api_url="https://google.test/androidpublisher/v3",
        google_token_url="https://google.test/token")
    receipt = adapter.lookup_google_play_submission({
        "provider": "google_play",
        "package_name": "com.example.canary",
        "track": "internal",
        "version_code": "10400",
    }, {
        "commit_sha": "a" * 40,
        "version": "1.4.0",
        "target": "mobile-production",
        "idempotency_key": "stable-key",
    }, {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": json.dumps({
        "client_email": "publisher@example.test",
        "private_key_id": "rsa-1",
        "private_key": _pem(private_key),
    })})

    assert [request.method for request in requests] == ["POST", "POST", "GET", "DELETE"]
    assert receipt == {
        **_submission("google_play"),
        "track": "internal",
        "idempotency_key": "stable-key",
    }


def test_app_store_builtin_submitter_creates_version_attaches_build_and_submits_review():
    private_key = ec.generate_private_key(ec.SECP256R1())
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/builds":
            return httpx.Response(200, json={"data": [{"id": "build-87"}]})
        if path == "/v1/appStoreVersions" and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/appStoreVersions" and request.method == "POST":
            payload = json.loads(request.content)["data"]
            assert payload["attributes"] == {
                "platform": "IOS", "versionString": "1.4.0", "releaseType": "MANUAL"}
            assert payload["relationships"]["app"]["data"]["id"] == "123456"
            return httpx.Response(201, json={"data": {
                "type": "appStoreVersions", "id": "version-7"}})
        if path.endswith("/appStoreVersions/version-7/relationships/build"):
            assert json.loads(request.content) == {
                "data": {"type": "builds", "id": "build-87"}}
            return httpx.Response(204)
        if path == "/v1/appStoreVersions/version-7" and request.method == "GET":
            assert request.url.params["include"] == \
                "appStoreVersionLocalizations,appStoreReviewDetail"
            return httpx.Response(200, json=_apple_metadata())
        if path.endswith("/apps/123456/reviewSubmissions"):
            assert request.url.params["include"] == "items"
            assert request.url.params["fields[reviewSubmissionItems]"] == \
                "state,appStoreVersion"
            return httpx.Response(200, json={"data": [], "included": []})
        if path == "/v1/reviewSubmissions" and request.method == "POST":
            payload = json.loads(request.content)["data"]
            assert payload["relationships"]["app"]["data"]["id"] == "123456"
            return httpx.Response(201, json={"data": {
                "type": "reviewSubmissions", "id": "review-1",
                "attributes": {"state": "READY_FOR_REVIEW"}}})
        if path == "/v1/reviewSubmissionItems":
            payload = json.loads(request.content)["data"]["relationships"]
            assert payload["reviewSubmission"]["data"]["id"] == "review-1"
            assert payload["appStoreVersion"]["data"]["id"] == "version-7"
            return httpx.Response(201, json={"data": {
                "type": "reviewSubmissionItems", "id": "item-1"}})
        if path == "/v1/reviewSubmissions/review-1" and request.method == "PATCH":
            assert json.loads(request.content)["data"]["attributes"] == {"submitted": True}
            return httpx.Response(200, json={"data": {
                "id": "review-1", "attributes": {"state": "WAITING_FOR_REVIEW"}}})
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        apple_api_url="https://apple.test", now=lambda: 1000)
    receipt = adapter.submit_app_store({
        "provider": "app_store_connect", "app_id": "123456",
        "build_number": "87", "platform": "ios", "release_goal": "submitted",
    }, {
        "commit_sha": "a" * 40, "version": "1.4.0",
        "target": "mobile-production", "idempotency_key": "stable-key",
    }, {
        "APP_STORE_CONNECT_KEY_ID": "key",
        "APP_STORE_CONNECT_ISSUER_ID": "issuer",
        "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
    })

    assert [request.method for request in requests] == [
        "GET", "GET", "POST", "PATCH", "GET", "GET", "POST", "POST", "PATCH"]
    assert receipt["app_store_version_id"] == "version-7"
    assert receipt["build_id"] == "build-87"
    assert receipt["review_submission_id"] == "review-1"
    assert receipt["metadata_readiness"] == {
        "status": "ready",
        "locales": ["en-US"],
        "required_fields": ["description", "supportUrl", "whatsNew"],
        "review_contact": True,
        "demo_account_required": False,
    }


def test_app_store_metadata_gate_blocks_review_mutation_with_exact_missing_fields():
    private_key = ec.generate_private_key(ec.SECP256R1())
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/builds":
            return httpx.Response(200, json={"data": [{"id": "build-87"}]})
        if request.url.path == "/v1/appStoreVersions":
            return httpx.Response(200, json={"data": [{
                "id": "version-7",
                "relationships": {"build": {"data": {"id": "build-87"}}},
            }]})
        if request.url.path == "/v1/appStoreVersions/version-7":
            return httpx.Response(200, json=_apple_metadata(
                whats_new="", demo_required=True, demo_password=""))
        return httpx.Response(500)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        apple_api_url="https://apple.test", now=lambda: 1000)
    with pytest.raises(StoreAdapterError) as exc_info:
        adapter.submit_app_store({
            "provider": "app_store_connect", "app_id": "123456",
            "build_number": "87", "platform": "IOS", "release_goal": "submitted",
            "apple_required_locales": "en-US,zh-Hant",
        }, {
            "commit_sha": "a" * 40, "version": "1.4.0",
            "target": "mobile-production", "idempotency_key": "stable-key",
        }, {
            "APP_STORE_CONNECT_KEY_ID": "key",
            "APP_STORE_CONNECT_ISSUER_ID": "issuer",
            "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
        })

    message = str(exc_info.value)
    assert "locale:zh-Hant" in message
    assert "en-US.whatsNew" in message
    assert "appStoreReviewDetail.demoAccountPassword" in message
    assert [request.url.path for request in requests] == [
        "/v1/builds", "/v1/appStoreVersions", "/v1/appStoreVersions/version-7"]


def test_app_store_builtin_uploader_reserves_and_uploads_parts_in_parallel(tmp_path):
    private_key = ec.generate_private_key(ec.SECP256R1())
    artifact = tmp_path / "release.ipa"
    artifact.write_bytes(b"abcdefgh")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/builds":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/apps/123456/buildUploads":
            assert request.url.params["filter[cfBundleVersion]"] == "87"
            return httpx.Response(200, json={"data": []})
        if path == "/v1/buildUploads" and request.method == "POST":
            body = json.loads(request.content)["data"]
            assert body["attributes"] == {
                "cfBundleShortVersionString": "1.4.0",
                "cfBundleVersion": "87", "platform": "IOS"}
            return httpx.Response(201, json={"data": {
                "type": "buildUploads", "id": "upload-1",
                "attributes": {"state": "AWAITING_UPLOAD"}}})
        if path == "/v1/buildUploads/upload-1/buildUploadFiles":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/buildUploadFiles" and request.method == "POST":
            body = json.loads(request.content)["data"]
            assert body["attributes"] == {
                "fileName": "release.ipa", "fileSize": 8,
                "uti": "com.apple.ipa", "assetType": "ASSET"}
            return httpx.Response(201, json={"data": {
                "type": "buildUploadFiles", "id": "file-1",
                "attributes": {
                    **body["attributes"],
                    "assetDeliveryState": {"state": "AWAITING_UPLOAD"},
                    "uploadOperations": [
                        {"method": "PUT", "url": "https://upload.test/part-1",
                         "offset": 0, "length": 4,
                         "requestHeaders": [{"name": "x-part", "value": "1"}]},
                        {"method": "PUT", "url": "https://upload.test/part-2",
                         "offset": 4, "length": 4,
                         "requestHeaders": [{"name": "x-part", "value": "2"}]},
                    ],
                },
            }})
        if path == "/part-1":
            assert request.headers["x-part"] == "1"
            assert "Authorization" not in request.headers
            assert request.content == b"abcd"
            return httpx.Response(200)
        if path == "/part-2":
            assert request.headers["x-part"] == "2"
            assert "Authorization" not in request.headers
            assert request.content == b"efgh"
            return httpx.Response(200)
        if path == "/v1/buildUploadFiles/file-1" and request.method == "PATCH":
            attrs = json.loads(request.content)["data"]["attributes"]
            assert attrs == {"uploaded": True, "sourceFileChecksums": {
                "file": {"hash": digest, "algorithm": "SHA_256"}}}
            return httpx.Response(200, json={"data": {
                "type": "buildUploadFiles", "id": "file-1"}})
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        apple_api_url="https://apple.test", now=lambda: 1000)
    receipt = adapter.submit_app_store({
        "provider": "app_store_connect", "app_id": "123456",
        "build_number": "87", "platform": "IOS", "release_goal": "submitted",
        "artifact_path": "release.ipa", "apple_upload_parallelism": 2,
    }, {
        "commit_sha": "a" * 40, "version": "1.4.0",
        "target": "mobile-production", "idempotency_key": "stable-key",
    }, {
        "APP_STORE_CONNECT_KEY_ID": "key",
        "APP_STORE_CONNECT_ISSUER_ID": "issuer",
        "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
    }, str(tmp_path))

    assert receipt["phase"] == "build_upload"
    assert receipt["build_upload_id"] == "upload-1"
    assert receipt["artifact_sha256"] == digest
    assert sorted(request.url.path for request in requests if request.method == "PUT") == \
        ["/part-1", "/part-2"]


def test_app_store_status_reports_pending_build_upload_without_version_lookup():
    private_key = ec.generate_private_key(ec.SECP256R1())
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {
            "type": "buildUploads", "id": "upload-1",
            "attributes": {"state": "PROCESSING"},
        }})

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        apple_api_url="https://apple.test", now=lambda: 1000)
    receipt = adapter.app_store_connect({
        "provider": "app_store_connect", "app_id": "123456",
    }, {
        **_submission("app_store_connect"),
        "phase": "build_upload", "build_upload_id": "upload-1",
    }, {
        "APP_STORE_CONNECT_KEY_ID": "key",
        "APP_STORE_CONNECT_ISSUER_ID": "issuer",
        "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
    })

    assert requests[0].url.path == "/v1/buildUploads/upload-1"
    assert receipt["milestone"] == "uploaded"
    assert receipt["provider_status"] == "PROCESSING"


def test_app_store_status_rejects_completed_upload_with_invalid_build():
    private_key = ec.generate_private_key(ec.SECP256R1())
    adapter = StoreStatusAdapter(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "data": {"type": "buildUploads", "id": "upload-1",
                     "attributes": {"state": "COMPLETE"}},
            "included": [{"type": "builds", "id": "build-87",
                          "attributes": {"processingState": "INVALID"}}],
        }))), apple_api_url="https://apple.test", now=lambda: 1000)

    receipt = adapter.app_store_connect({
        "provider": "app_store_connect", "app_id": "123456",
    }, {
        **_submission("app_store_connect"),
        "phase": "build_upload", "build_upload_id": "upload-1",
    }, {
        "APP_STORE_CONNECT_KEY_ID": "key",
        "APP_STORE_CONNECT_ISSUER_ID": "issuer",
        "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
    })

    assert receipt["milestone"] == "rejected"
    assert receipt["provider_status"] == "INVALID_BINARY"


def test_app_store_builtin_recovery_requires_submitted_review_for_submitted_goal():
    private_key = ec.generate_private_key(ec.SECP256R1())
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/v1/builds":
            return httpx.Response(200, json={"data": [{"id": "build-87"}]})
        if request.url.path == "/v1/appStoreVersions":
            return httpx.Response(200, json={"data": [{
                "id": "version-7",
                "relationships": {"build": {"data": {"id": "build-87"}}},
            }]})
        if request.url.path == "/v1/appStoreVersions/version-7":
            return httpx.Response(200, json=_apple_metadata())
        return httpx.Response(200, json={
            "data": [{
                "type": "reviewSubmissions", "id": "review-1",
                "attributes": {"state": "WAITING_FOR_REVIEW"},
                "relationships": {"items": {"data": [{"id": "item-1"}]}},
            }],
            "included": [{
                "type": "reviewSubmissionItems", "id": "item-1",
                "relationships": {
                    "appStoreVersion": {"data": {"id": "version-7"}},
                },
            }],
        })

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        apple_api_url="https://apple.test", now=lambda: 1000)
    receipt = adapter.lookup_app_store_submission({
        "provider": "app_store_connect", "app_id": "123456",
        "build_number": "87", "submission_adapter": "official_api",
        "release_goal": "submitted",
    }, {
        "commit_sha": "a" * 40, "version": "1.4.0",
        "target": "mobile-production", "idempotency_key": "stable-key",
    }, {
        "APP_STORE_CONNECT_KEY_ID": "key",
        "APP_STORE_CONNECT_ISSUER_ID": "issuer",
        "APP_STORE_CONNECT_PRIVATE_KEY": _pem(private_key),
    })

    assert methods == ["GET", "GET", "GET", "GET"]
    assert receipt["review_submission_id"] == "review-1"
    assert receipt["metadata_readiness"]["status"] == "ready"


def test_builtin_google_recovery_binds_version_code_to_local_aab_hash(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    artifact = tmp_path / "release.aab"
    artifact.write_bytes(b"signed-aab-payload")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "oauth-token"})
        if request.url.path.endswith("/edits") and request.method == "POST":
            return httpx.Response(200, json={"id": "lookup-edit"})
        if request.url.path.endswith("/tracks/internal"):
            return httpx.Response(200, json={
                "releases": [{"versionCodes": ["10400"], "status": "completed"}],
            })
        if request.url.path.endswith("/bundles"):
            return httpx.Response(200, json={
                "bundles": [{"versionCode": 10400, "sha256": digest}],
            })
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        google_token_url="https://google.test/token")
    receipt = adapter.lookup_google_play_submission({
        "provider": "google_play", "package_name": "com.example.canary",
        "track": "internal", "version_code": "10400",
        "submission_adapter": "official_api", "artifact_path": "release.aab",
    }, {
        "commit_sha": "a" * 40, "version": "1.4.0",
        "target": "mobile-production", "idempotency_key": "stable-key",
    }, {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": json.dumps({
        "client_email": "publisher@example.test",
        "private_key_id": "rsa-1", "private_key": _pem(private_key),
    })}, str(tmp_path))

    assert methods == ["POST", "POST", "GET", "GET", "DELETE"]
    assert receipt["artifact_sha256"] == digest


def test_google_play_builtin_submitter_uploads_validates_and_commits_internal_track(
        tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    artifact = tmp_path / "release.aab"
    artifact.write_bytes(b"signed-aab-payload")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"access_token": "oauth-token"})
        if path.endswith("/edits") and request.method == "POST":
            return httpx.Response(200, json={"id": "release-edit"})
        if path.endswith("/bundles") and request.method == "GET":
            return httpx.Response(200, json={"bundles": []})
        if path.startswith("/upload/"):
            assert request.url.params["uploadType"] == "media"
            assert request.headers["Content-Type"] == "application/octet-stream"
            assert request.content == b"signed-aab-payload"
            return httpx.Response(200, json={
                "versionCode": 10400, "sha256": digest,
            })
        if path.endswith("/tracks/internal") and request.method == "GET":
            return httpx.Response(200, json={
                "track": "internal",
                "releases": [{
                    "name": "existing", "versionCodes": ["10300"],
                    "status": "completed",
                }],
            })
        if path.endswith("/tracks/internal") and request.method == "PUT":
            payload = json.loads(request.content)
            assert payload["releases"][0]["versionCodes"] == ["10300"]
            assert payload["releases"][1] == {
                "name": "Bastet 1.4.0 [stable-key]",
                "versionCodes": ["10400"],
                "status": "completed",
            }
            return httpx.Response(200, json=payload)
        if path.endswith("/edits/release-edit:validate"):
            return httpx.Response(200, json={"id": "release-edit"})
        if path.endswith("/edits/release-edit:commit"):
            assert request.url.params["changesInReviewBehavior"] == "ERROR_IF_IN_REVIEW"
            assert request.url.params["changesNotSentForReview"] == "true"
            return httpx.Response(200, json={"id": "release-edit"})
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        google_api_url="https://google.test/androidpublisher/v3",
        google_upload_url="https://google.test/upload/androidpublisher/v3",
        google_token_url="https://google.test/token")
    receipt = adapter.submit_google_play({
        "provider": "google_play",
        "package_name": "com.example.canary",
        "track": "internal",
        "version_code": "10400",
        "artifact_path": "release.aab",
    }, {
        "commit_sha": "a" * 40,
        "version": "1.4.0",
        "target": "mobile-production",
        "idempotency_key": "stable-key",
    }, {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": json.dumps({
        "client_email": "publisher@example.test",
        "private_key_id": "rsa-1",
        "private_key": _pem(private_key),
    })}, str(tmp_path))

    assert [request.method for request in requests] == [
        "POST", "POST", "GET", "POST", "GET", "PUT", "POST", "POST"]
    assert receipt["version_code"] == "10400"
    assert receipt["artifact_sha256"] == digest
    assert not any(request.method == "DELETE" for request in requests)


def test_google_play_builtin_submitter_deletes_uncommitted_edit_on_hash_conflict(
        tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "release.aab").write_bytes(b"new-payload")
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "oauth-token"})
        if request.url.path.endswith("/edits") and request.method == "POST":
            return httpx.Response(200, json={"id": "release-edit"})
        if request.url.path.endswith("/bundles"):
            return httpx.Response(200, json={
                "bundles": [{"versionCode": 10400, "sha256": "wrong"}],
            })
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    adapter = StoreStatusAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        google_token_url="https://google.test/token")
    with pytest.raises(StoreAdapterError, match="different AAB SHA-256"):
        adapter.submit_google_play({
            "provider": "google_play", "package_name": "com.example.canary",
            "track": "internal", "version_code": "10400",
            "artifact_path": "release.aab",
        }, {
            "commit_sha": "a" * 40, "version": "1.4.0",
            "target": "mobile-production", "idempotency_key": "stable-key",
        }, {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": json.dumps({
            "client_email": "publisher@example.test",
            "private_key_id": "rsa-1", "private_key": _pem(private_key),
        })}, str(tmp_path))

    assert methods == ["POST", "POST", "GET", "DELETE"]


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


def test_store_submitter_must_echo_the_engine_idempotency_key():
    profile = {
        "provider": "google_play",
        "package_name": "com.example.canary",
        "track": "production",
    }

    with pytest.raises(DeliveryError, match="idempotency_key expected"):
        _submission_receipt(
            json.dumps(_submission("google_play")), commit_sha="a" * 40,
            version="1.4.0", target="mobile-production", profile=profile,
            idempotency_key="stable-action-key")


def test_official_adapter_profile_does_not_require_verify_command():
    profile = validate_profile({
        "provider": "app_store_connect",
        "status_adapter": "official_api",
        "app_id": "123456",
        "predeploy_command": "test-release",
        "deploy_command": "upload-release",
    }, "production")

    assert profile["status_adapter"] == "official_api"


@pytest.mark.parametrize("provider,identity,missing", [
    ("app_store_connect", {"app_id": "123456"}, "build_number"),
    ("google_play", {"package_name": "com.example.canary", "track": "internal"},
     "version_code"),
])
def test_official_submission_recovery_requires_immutable_artifact_identity(
        provider, identity, missing):
    with pytest.raises(ValueError, match=missing):
        validate_profile({
            "provider": provider,
            **identity,
            "status_adapter": "official_api",
            "submission_recovery": "official_api",
            "predeploy_command": "test-release",
            "deploy_command": "upload-release",
        }, "production")


def test_builtin_google_submission_is_internal_only_and_needs_no_deploy_command():
    profile = validate_profile({
        "provider": "google_play",
        "status_adapter": "official_api",
        "submission_recovery": "official_api",
        "submission_adapter": "official_api",
        "package_name": "com.example.canary",
        "track": "internal",
        "version_code": "10400",
        "artifact_path": "app/build/outputs/bundle/release/app-release.aab",
        "predeploy_command": "run-mobile-tests",
    }, "production")

    assert profile["submission_adapter"] == "official_api"
    with pytest.raises(ValueError, match="restricted to the internal track"):
        validate_profile({**profile, "track": "production"}, "production")


def test_builtin_app_store_submission_promotes_processed_build_without_deploy_command():
    profile = validate_profile({
        "provider": "app_store_connect",
        "status_adapter": "official_api",
        "submission_recovery": "official_api",
        "submission_adapter": "official_api",
        "app_id": "123456",
        "platform": "IOS",
        "build_number": "87",
        "release_goal": "submitted",
        "predeploy_command": "run-mobile-tests",
    }, "production")

    assert profile["submission_adapter"] == "official_api"
    with pytest.raises(ValueError, match="MANUAL"):
        validate_profile({**profile, "apple_release_type": "SCHEDULED"}, "production")

    upload_profile = validate_profile({
        **profile, "artifact_path": "release.ipa", "apple_upload_parallelism": 8,
    }, "production")
    assert upload_profile["artifact_path"] == "release.ipa"
    with pytest.raises(ValueError, match="between 1 and 8"):
        validate_profile({**upload_profile, "apple_upload_parallelism": 9}, "production")
    with pytest.raises(ValueError, match="unsupported apple_metadata_required_fields"):
        validate_profile({
            **upload_profile, "apple_metadata_required_fields": "description,screenshots",
        }, "production")
    with pytest.raises(ValueError, match="requires metadata fields"):
        validate_profile({
            **upload_profile, "apple_metadata_required_fields": "",
        }, "production")
