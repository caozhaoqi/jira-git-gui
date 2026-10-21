from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.k8s import routes_k8s_env


VALID_KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- name: test
  cluster:
    server: https://127.0.0.1:6443
"""


def make_client():
    app = FastAPI()
    app.include_router(routes_k8s_env.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_incoming_queue(monkeypatch):
    monkeypatch.setattr(routes_k8s_env, "_INCOMING_KUBECONFIGS", routes_k8s_env.deque(maxlen=5))


def test_pushed_kubeconfig_is_staged_then_imported_only_after_accept(tmp_path, monkeypatch):
    import core.k8s.env as k8s_env

    monkeypatch.setattr(k8s_env, "ENV_CONFIG_PATH", tmp_path / "k8s_envs.json")
    monkeypatch.setattr(k8s_env, "KUBECONFIG_DIR", tmp_path / "kubeconfigs")
    client = make_client()

    response = client.post("/api/k8s/env/incoming", json={
        "name": "ci-test",
        "label": "CI Test",
        "namespace": "default",
        "content": VALID_KUBECONFIG,
    })
    assert response.status_code == 200
    draft_id = response.json()["id"]
    assert not (tmp_path / "kubeconfigs" / "ci-test.kubeconfig").exists()

    pending = client.get("/api/k8s/env/incoming").json()["pending"]
    assert len(pending) == 1
    assert pending[0]["name"] == "ci-test"
    assert pending[0]["overwrite"] is False
    assert "content" not in pending[0]

    accepted = client.post(f"/api/k8s/env/incoming/{draft_id}/accept", json={})
    assert accepted.status_code == 200
    kubeconfig_path = tmp_path / "kubeconfigs" / "ci-test.kubeconfig"
    assert kubeconfig_path.read_text(encoding="utf-8") == VALID_KUBECONFIG.strip()
    assert client.get("/api/k8s/env/incoming").json()["pending"] == []


def test_pushed_kubeconfig_rejects_unsafe_name_or_invalid_yaml():
    client = make_client()
    unsafe = client.post("/api/k8s/env/incoming", json={
        "name": "../outside",
        "content": VALID_KUBECONFIG,
    })
    invalid = client.post("/api/k8s/env/incoming", json={
        "name": "safe",
        "content": "not a kubeconfig",
    })
    assert unsafe.status_code == 400
    assert invalid.status_code == 400


def test_pushed_kubeconfig_queue_rejects_overflow_without_dropping_pending_items():
    client = make_client()
    payload = {"name": "ci-test", "content": VALID_KUBECONFIG}
    responses = [client.post("/api/k8s/env/incoming", json=payload) for _ in range(6)]
    assert [response.status_code for response in responses] == [200] * 5 + [429]
    assert len(client.get("/api/k8s/env/incoming").json()["pending"]) == 5


def test_existing_environment_requires_explicit_overwrite_confirmation(tmp_path, monkeypatch):
    import core.k8s.env as k8s_env

    monkeypatch.setattr(k8s_env, "ENV_CONFIG_PATH", tmp_path / "k8s_envs.json")
    monkeypatch.setattr(k8s_env, "KUBECONFIG_DIR", tmp_path / "kubeconfigs")
    k8s_env.add_or_update_env("ci-test", label="Existing")
    client = make_client()
    queued = client.post("/api/k8s/env/incoming", json={
        "name": "ci-test",
        "content": VALID_KUBECONFIG,
    })
    draft_id = queued.json()["id"]

    rejected = client.post(f"/api/k8s/env/incoming/{draft_id}/accept", json={})
    assert rejected.status_code == 409
    assert not (tmp_path / "kubeconfigs" / "ci-test.kubeconfig").exists()

    accepted = client.post(
        f"/api/k8s/env/incoming/{draft_id}/accept",
        json={"confirm_overwrite": True},
    )
    assert accepted.status_code == 200
