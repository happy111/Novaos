"""Tests for the neptune-check Lambda handler (src/lambda/neptune-check/app.py)."""

import importlib.util
import json
from pathlib import Path

import pytest

_APP_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "lambda" / "neptune-check" / "app.py"
)


@pytest.fixture
def handler():
    """Load the handler by path- the lambda dir is not an importable package."""
    spec = importlib.util.spec_from_file_location("neptune_check_app", _APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.handler


def test_returns_ok_with_env_vars(handler, monkeypatch):
    monkeypatch.setenv("APP_PREFIX", "novos-ar")
    monkeypatch.setenv("ENV", "dev")

    response = handler({"source": "test"}, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "status": "ok",
        "app_prefix": "novos-ar",
        "env": "dev",
    }


def test_falls_back_to_unknown_without_env_vars(handler, monkeypatch):
    monkeypatch.delenv("APP_PREFIX", raising=False)
    monkeypatch.delenv("ENV", raising=False)

    body = json.loads(handler({}, None)["body"])

    assert body["app_prefix"] == "unknown"
    assert body["env"] == "unknown"
