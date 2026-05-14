from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from jose import jwt
from pydantic import ValidationError

from app import main, models, schemas


class FakeResult:
    def __init__(self, item):
        self._item = item

    def scalar_one_or_none(self):
        return self._item


class FakeAsyncSession:
    def __init__(self):
        self.profiles = []
        self._next_profile_id = 1

    async def execute(self, stmt):
        params = stmt.compile().params
        if "user_id_1" in params:
            user_id = params["user_id_1"]
            profile = next((p for p in self.profiles if p.user_id == user_id), None)
            return FakeResult(profile)
        if "username_1" in params:
            username = params["username_1"]
            profile = next((p for p in self.profiles if p.username == username), None)
            return FakeResult(profile)
        return FakeResult(None)

    async def get(self, _, profile_id):
        return next((p for p in self.profiles if p.id == profile_id), None)

    def add(self, profile):
        if profile.id is None:
            profile.id = self._next_profile_id
            self._next_profile_id += 1
        if profile not in self.profiles:
            self.profiles.append(profile)

    async def commit(self):
        return None

    async def refresh(self, _):
        return None

    async def delete(self, profile):
        self.profiles = [p for p in self.profiles if p.id != profile.id]


class FakeAsyncClient:
    response = None
    should_raise = False
    captured_requests = []

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        return await self._call("POST", url, json=json, headers=headers)

    async def get(self, url, headers=None):
        return await self._call("GET", url, json=None, headers=headers)

    async def delete(self, url, headers=None):
        return await self._call("DELETE", url, json=None, headers=headers)

    async def _call(self, method, url, json=None, headers=None):
        if FakeAsyncClient.should_raise:
            raise main.httpx.HTTPError("service unavailable")
        FakeAsyncClient.captured_requests.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        return FakeAsyncClient.response


def _make_token(payload, exp_delta_seconds=600):
    payload = dict(payload)
    payload["exp"] = datetime.now(timezone.utc) + timedelta(seconds=exp_delta_seconds)
    return jwt.encode(payload, main.JWT_SECRET, algorithm=main.JWT_ALGORITHM)


@pytest.fixture()
def fake_db():
    db = FakeAsyncSession()
    db.add(
        models.Profile(
            id=None,
            user_id=1,
            username="michael",
            bio="bio",
            stack=["python"],
            experience="3 years",
            skills=[],
            interests=[],
            short_term_goals=[],
            long_term_goals=[],
        )
    )
    return db


@pytest.fixture()
def client(fake_db, monkeypatch):
    async def override_get_db():
        yield fake_db

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    main.app.dependency_overrides[main.get_db] = override_get_db
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()
    FakeAsyncClient.response = None
    FakeAsyncClient.should_raise = False
    FakeAsyncClient.captured_requests = []


def _auth_headers(payload=None, exp_delta_seconds=600):
    payload = payload or {"user_id": 1}
    token = _make_token(payload, exp_delta_seconds=exp_delta_seconds)
    return {"Authorization": f"Bearer {token}"}


def test_extract_user_id_payload_variants():
    assert main._extract_user_id({"user_id": 123}) == 123
    assert main._extract_user_id({"id": "456"}) == 456
    assert main._extract_user_id({"sub": 789}) == 789


def test_extract_user_id_invalid_payload():
    assert main._extract_user_id({"user_id": "abc"}) is None
    assert main._extract_user_id({"id": None}) is None
    assert main._extract_user_id({}) is None


@pytest.mark.anyio
async def test_get_current_user_id_unauthorized():
    request = Request({"type": "http", "headers": []})
    with pytest.raises(main.HTTPException) as exc:
        await main.get_current_user_id(request)
    assert exc.value.status_code == 401


def test_public_path_without_token(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "Profile Service API"


def test_protected_path_missing_token(client):
    response = client.get("/profile/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing token"


def test_protected_path_invalid_token(client):
    response = client.get(
        "/profile/me", headers={"Authorization": "Bearer not-a-valid-token"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"


def test_protected_path_expired_token_redirect(client):
    response = client.get(
        "/profile/me", headers=_auth_headers(exp_delta_seconds=-60), follow_redirects=False
    )
    assert response.status_code == 307
    assert response.headers["location"].endswith("/auth/login")


def test_create_profile_and_read_by_id(client):
    payload = {
        "username": "alice",
        "bio": "Backend dev",
        "stack": ["python", "fastapi"],
        "experience": "2 years",
        "skills": [],
        "interests": [],
        "short_term_goals": ["api"],
        "long_term_goals": ["architect"],
    }
    create_response = client.post("/profile", json=payload, headers=_auth_headers())
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["user_id"] == 1
    assert created["username"] == "alice"

    get_response = client.get(f"/profile/{created['id']}", headers=_auth_headers())
    assert get_response.status_code == 200
    assert get_response.json()["username"] == "alice"


def test_read_patch_and_delete_profile_me_flow(client):
    read_response = client.get("/profile/me", headers=_auth_headers({"user_id": 1}))
    assert read_response.status_code == 200
    assert read_response.json()["username"] == "michael"

    patch_response = client.patch(
        "/profile/me",
        json={"bio": "updated bio"},
        headers=_auth_headers({"user_id": 1}),
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["bio"] == "updated bio"

    delete_response = client.delete("/profile/me", headers=_auth_headers({"user_id": 1}))
    assert delete_response.status_code == 204

    missing_response = client.get("/profile/me", headers=_auth_headers({"user_id": 1}))
    assert missing_response.status_code == 404


def test_read_profile_by_username_not_found(client):
    response = client.get(
        "/profile/by-username/unknown", headers=_auth_headers({"user_id": 1})
    )
    assert response.status_code == 404


def test_update_profile_by_id_not_found(client):
    response = client.patch(
        "/profile/999",
        json={"bio": "nope"},
        headers=_auth_headers({"user_id": 1}),
    )
    assert response.status_code == 404


def test_monkeytype_proxy_success_json(client):
    FakeAsyncClient.response = main.httpx.Response(
        status_code=200,
        json={"ok": True},
        headers={"content-type": "application/json"},
    )
    response = client.post(
        "/profile/integrations/monkeytype/fetch",
        json={"username": "monkey-user"},
        headers=_auth_headers({"user_id": 1}),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_github_connect_sets_internal_header(client):
    FakeAsyncClient.response = main.httpx.Response(
        status_code=200,
        json={"connected": True},
        headers={"content-type": "application/json"},
    )
    response = client.post(
        "/profile/integrations/github/connect",
        json={"username": "octocat"},
        headers=_auth_headers({"user_id": 1}),
    )
    assert response.status_code == 200
    sent = FakeAsyncClient.captured_requests[-1]
    assert sent["headers"]["X-Internal-User-Id"] == "1"
    assert sent["json"]["internal_user_id"] == 1


def test_integrations_service_unavailable_returns_503(client):
    FakeAsyncClient.should_raise = True
    response = client.get(
        "/profile/integrations/github/private-stats", headers=_auth_headers({"user_id": 1})
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Integrations service is unavailable"


def test_disconnect_returns_204_without_body(client):
    FakeAsyncClient.response = main.httpx.Response(
        status_code=204,
        headers={"content-type": "application/json"},
    )
    response = client.delete(
        "/profile/integrations/github",
        headers=_auth_headers({"user_id": 1}),
    )
    assert response.status_code == 204
    assert response.content == b""


def test_schemas_skill_validation_and_profile_update_exclude_unset():
    skill = schemas.Skill(
        name="Python",
        type="language",
        self_assessment_level="middle",
        years_of_experience=3.5,
    )
    assert skill.name == "Python"

    with pytest.raises(ValidationError):
        schemas.Skill(
            name="K8s",
            type="platform",
            self_assessment_level="middle",
        )

    patch = schemas.ProfileUpdate(bio="new")
    assert patch.model_dump(exclude_unset=True) == {"bio": "new"}
