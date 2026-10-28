"""Smoke tests for the CDK app entrypoint (cdk_pipeline/app.py)."""

import json
import runpy
from pathlib import Path

import aws_cdk as cdk
import pytest

_APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
_DEV_CONFIG = json.loads((_APP_PATH.parent / "environments" / "dev.json").read_text())
_STACK_PREFIX = _DEV_CONFIG["stack_prefix"]
_APP_PREFIX = _DEV_CONFIG["app_prefix"]
_LAMBDAS_STACK_NAME = f"{_STACK_PREFIX}-Lambdas-{_APP_PREFIX}-dev"

EXPECTED_STACKS = [
    f"{_STACK_PREFIX}-IamRoles-{_APP_PREFIX}-dev",
    f"{_STACK_PREFIX}-S3Buckets-{_APP_PREFIX}-dev",
    f"{_STACK_PREFIX}-Notifications-{_APP_PREFIX}-dev",
    _LAMBDAS_STACK_NAME,
    f"{_STACK_PREFIX}-CohortLabel-{_APP_PREFIX}-dev",
    f"{_STACK_PREFIX}-ModelReadyDataset-{_APP_PREFIX}-dev",
]


def _synth(tmp_path, monkeypatch, environment):
    """Run app.py the way the CDK CLI does- context-driven, no AWS calls.

    cdk.App() is patched rather than the CDK_* env vars because the jsii Node
    runtime has already captured the environment by the time a test runs.
    """
    outdir = tmp_path / "cdk.out"
    real_app = cdk.App
    monkeypatch.setattr(
        cdk,
        "App",
        lambda **kwargs: real_app(
            outdir=str(outdir), context={"environment": environment}, **kwargs
        ),
    )
    runpy.run_path(str(_APP_PATH), run_name="__main__")
    return outdir


def test_dev_app_synthesizes_expected_stacks(tmp_path, monkeypatch):
    outdir = _synth(tmp_path, monkeypatch, "dev")

    for stack_name in EXPECTED_STACKS:
        assert (outdir / f"{stack_name}.template.json").exists(), stack_name


def test_synth_applies_mandatory_tags(tmp_path, monkeypatch):
    outdir = _synth(tmp_path, monkeypatch, "dev")

    template = json.loads((outdir / f"{_LAMBDAS_STACK_NAME}.template.json").read_text())
    tagged = [
        res
        for res in template["Resources"].values()
        if isinstance(res.get("Properties", {}).get("Tags"), list)
    ]
    assert tagged, "No tagged resources found in the synthesized Lambdas stack"
    for res in tagged:
        keys = {tag["Key"] for tag in res["Properties"]["Tags"]}
        assert {"ClarityID", "DataProductName", "Environment", "BU"} <= keys


def test_unknown_environment_raises(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        _synth(tmp_path, monkeypatch, "no-such-env")
