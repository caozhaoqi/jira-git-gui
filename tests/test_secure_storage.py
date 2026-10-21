import asyncio
import json

from core.config import cf, connect, hcm, session
from core.k8s import env as k8s_env
from core.models import ConnectConfig
import api.routes_services_config as services_config


def test_legacy_cf_password_migrates_out_of_json():
    legacy = {"accounts": [{"name": "prod", "password": "secret"}]}

    protected = cf.protect_cf_accounts(legacy)
    assert "password" not in protected["accounts"][0]
    assert protected["accounts"][0]["password_ref"].startswith("keyring://")
    assert cf.reveal_cf_accounts(protected)["accounts"][0]["password"] == "secret"
    assert legacy["accounts"][0]["password"] == "secret"


def test_hcm_token_is_stored_outside_local_json():
    protected = hcm.protect_hcm_config({"proxy_target": {"base_url": "http://hcm"}, "token": "hcm-secret"})
    assert "token" not in protected
    assert protected["token_ref"].startswith("keyring://")
    assert hcm.reveal_hcm_config(protected)["token"] == "hcm-secret"


def test_session_cookie_migrates_to_os_store(tmp_path, monkeypatch):
    session_file = tmp_path / ".session.json"
    session_file.write_text(json.dumps({
        "cookie": "JSESSIONID=secret",
        "jira_url": "https://jira.example",
    }), encoding="utf-8")
    monkeypatch.setattr(session, "_SESSION_FILE", session_file)
    monkeypatch.setattr(session, "_BASE", tmp_path)

    loaded = session.load_session()

    persisted = json.loads(session_file.read_text(encoding="utf-8"))
    assert loaded["cookie"] == "JSESSIONID=secret"
    assert "cookie" not in persisted
    assert persisted["cookie_ref"].startswith("keyring://")


def test_jira_env_secrets_migrate_and_load(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JIRA_URL=https://jira.example\nJIRA_PAT=secret-pat\n",
        encoding="utf-8",
    )

    connect._migrate_env_secrets(env_file)
    persisted = env_file.read_text(encoding="utf-8")
    loaded = connect._parse_env_file(env_file)

    assert "secret-pat" not in persisted
    assert loaded["JIRA_PAT"] == "secret-pat"


def test_save_jira_config_replaces_existing_secret_alias(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("JIRA_PAT=old-pat\nJIRA_URL=https://jira.example\n", encoding="utf-8")
    monkeypatch.setattr(services_config, "_ENV_BASE", tmp_path)
    monkeypatch.setattr(
        services_config,
        "_load_connect_config",
        lambda: (ConnectConfig(jira_url="https://jira.example"), True, str(env_file)),
    )
    from api.common import client
    monkeypatch.setattr(client, "set_config", lambda _config: None)
    monkeypatch.setattr(client, "_drop_http_client", lambda: None)

    result = asyncio.run(services_config.save_jira_config(
        services_config.JiraConfigReq(
            jira_url="https://jira.example",
            username="user",
            mode="pat",
            pat="new-pat",
        )
    ))

    persisted = env_file.read_text(encoding="utf-8")
    assert result["ok"] is True
    assert "old-pat" not in persisted and "new-pat" not in persisted
    assert connect._parse_env_file(env_file)["JIRA_PAT"] == "new-pat"


def test_k8s_ssh_password_migrates_out_of_env_json(tmp_path, monkeypatch):
    env_file = tmp_path / "k8s_envs.json"
    monkeypatch.setattr(k8s_env, "ENV_CONFIG_PATH", env_file)

    k8s_env.save_envs({
        "current": "prod",
        "environments": {"prod": {"ssh_password": "ssh-secret"}},
    })
    persisted = json.loads(env_file.read_text(encoding="utf-8"))

    assert "ssh_password" not in persisted["environments"]["prod"]
    assert k8s_env.load_envs()["environments"]["prod"]["ssh_password"] == "ssh-secret"
