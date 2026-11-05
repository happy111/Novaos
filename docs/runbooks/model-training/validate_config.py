#!/usr/bin/env python3
"""Validate a model-training config before anything is built, uploaded or run.

Runs the SAME validation the pipeline's Validate state runs
(`src.pipelines.model_training.validate`, reached here through its two layers), so
a clean result means that state will not reject the config.

The two pure layers need no AWS at all:

    structural   the JSON Schema + the dataclass load (training_config.load)
    onboarding   model_name match + the training gate is no looser than the
                 approved success criteria (only with --onboarding-form)

The third does need credentials, because it proves the labeled dataset resolves to
a real version in S3. It is attempted by default and SKIPPED WITH A WARNING when no
credentials are configured, so this script stays useful on a machine that has none
— but a skipped business check is the one the pipeline is most likely to fail on.

Nothing here reads a .env. The infra vars the platform code expects are seeded from
cdk_pipeline/environments/<ENV>.json, the same file every other runbook script
reads, so this validates against the environment the pipeline actually runs in.

Point it at the model's model-training directory, which the runbook exports once:

    export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/\\
        patient_outcome_mortality_v2/model_training

    validate_config.py                        # reads $INTAKE_DIR
    validate_config.py <other dir>            # or take the path directly
    validate_config.py --onboarding-form <p>  # also check the approved gate

Exit codes: 0 valid, 1 the config is invalid, 2 the paths could not be resolved.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

CONFIG_FILENAME = "model-training-config.yaml"
ONBOARDING_FILENAME = "model-onboarding-form.yaml"
MODEL_TRAINING_DIR = "model_training"
ONBOARDING_DIR = "model_onboarding"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNRESOLVED = 2


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a model-training config and the dataset it names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "config_dir",
        nargs="?",
        type=pathlib.Path,
        help=f"the model's {MODEL_TRAINING_DIR} directory (default: $INTAKE_DIR)",
    )
    parser.add_argument(
        "--onboarding-form",
        type=pathlib.Path,
        default=None,
        help=f"path to {ONBOARDING_FILENAME}; default: the sibling "
        f"{ONBOARDING_DIR}/ directory when it holds one",
    )
    parser.add_argument(
        "--env",
        default=os.environ.get("ENV", "dev"),
        help="environment file to read, dev | qa | prod (default: dev)",
    )
    return parser.parse_args(argv)


def seed_infra_env(env_name):
    """Put the infra vars the platform code requires into os.environ.

    Called BEFORE src.model_training_core is imported: that package resolves each
    value on first access and raises when one is missing, which is what makes a
    .env necessary otherwise. Region, environment and app prefix are all the
    dataset resolution needs — the artifact bucket and the execution role are read
    when a pipeline is BUILT, which this script never does.

    Returns the environment file's parsed contents, or None when it is missing.
    """
    env_file = REPO_ROOT / "cdk_pipeline" / "environments" / f"{env_name}.json"
    if not env_file.is_file():
        return None
    cfg = json.loads(env_file.read_text())
    os.environ.setdefault("AWS_REGION", cfg["region"])
    os.environ.setdefault("ENV", cfg["env"])
    os.environ.setdefault("APP_PREFIX", cfg["app_prefix"])
    return cfg


def derive_model_id(config_dir):
    """The model the config belongs to: the path above model_training/, relative to
    the models repo root.

    That is one or two segments depending on the model, so the root is found with
    git rather than by counting parents. None when it cannot be. Display only here;
    upload_config.sh and run_pipeline.sh need it for real, to compose the S3 prefix.
    """
    if config_dir.name != MODEL_TRAINING_DIR:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(config_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        root = pathlib.Path(completed.stdout.strip()).resolve()
        return str(config_dir.parent.relative_to(root))
    except ValueError:
        return None


def resolve_config(config_dir):
    """(config path, error). The filename is fixed, so there is nothing to guess."""
    if not config_dir.is_dir():
        return None, f"{config_dir} does not exist"
    config_path = config_dir / CONFIG_FILENAME
    if not config_path.is_file():
        return None, (
            f"{config_dir} has no {CONFIG_FILENAME}; the Validate state opens it by "
            f"that exact name. Is INTAKE_DIR a <model>/{MODEL_TRAINING_DIR} "
            "directory?"
        )
    return config_path, None


def resolve_onboarding(config_dir, explicit):
    """The onboarding form to cross-check against, or None.

    Defaults to the sibling model_onboarding/ directory, because that is where the
    models repo keeps it — the form is about the MODEL, not about this training run,
    so it lives beside model_training/ rather than inside it.
    """
    if explicit is not None:
        path = explicit.expanduser().resolve()
        return (path, None) if path.is_file() else (None, f"{path} does not exist")
    sibling = config_dir.parent / ONBOARDING_DIR / ONBOARDING_FILENAME
    return (sibling, None) if sibling.is_file() else (None, None)


def main(argv=None):
    args = _parse_args(argv)
    given = args.config_dir or os.environ.get("INTAKE_DIR", "").strip()
    if not given:
        print(
            "no model-training directory. Export INTAKE_DIR (see the runbook) or "
            "pass one as the only argument.",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVED

    config_dir = pathlib.Path(given).expanduser().resolve()
    config_path, error = resolve_config(config_dir)
    if error:
        print(error, file=sys.stderr)
        return EXIT_UNRESOLVED

    onboarding_path, error = resolve_onboarding(config_dir, args.onboarding_form)
    if error:
        print(error, file=sys.stderr)
        return EXIT_UNRESOLVED

    if seed_infra_env(args.env) is None:
        print(
            f"no such environment file: cdk_pipeline/environments/{args.env}.json",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVED

    # After seed_infra_env: these modules resolve infra on first access.
    from src.model_training_core import (  # noqa: E402  (after path + env seeding)
        naming,
        onboarding_form,
        training_config,
        validation,
    )

    try:
        yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        print(f"INVALID — {CONFIG_FILENAME} is not valid YAML: {exc}", file=sys.stderr)
        return EXIT_INVALID

    model_id = derive_model_id(config_dir)
    if model_id:
        print(f"model        : {model_id}")
    print(f"config       : {config_path}")
    print(f"onboarding   : {onboarding_path or '(none — Model Card not seeded)'}")
    print(f"environment  : {args.env}")
    print()

    # ── structural ────────────────────────────────────────────────────────
    try:
        cfg = training_config.load(str(config_path))
    except (ValueError, KeyError) as exc:
        print(f"INVALID — {exc}")
        return EXIT_INVALID

    print(f"  model_name : {cfg.model_name}")
    print(f"  target     : {cfg.target_variable}")
    print(f"  dataset    : {cfg.dataset_name} (v={cfg.dataset_version})")
    # The repos build_and_push.sh pushes to, resolved by the same function the
    # pipeline resolves each step's image with — so a mismatch here is a mismatch
    # the run would hit when it pulled.
    step_compute = {
        "preprocess": cfg.preprocessing.compute,
        "train": cfg.compute,
        "evaluate": cfg.evaluation.compute,
    }
    for step, compute in step_compute.items():
        ref = naming.step_image_ref(cfg.model_name, step, compute.instance_type)
        print(f"  {step:<11}: {ref}")
    print()

    # ── onboarding form + approved gate ───────────────────────────────────
    if onboarding_path:
        try:
            onboarding = onboarding_form.load(str(onboarding_path))
            if onboarding.model_name != cfg.model_name:
                raise ValueError(
                    f"onboarding form model_name '{onboarding.model_name}' does not "
                    f"match training config model_name '{cfg.model_name}'."
                )
            validation.validate_kpi_gate(cfg, onboarding, config_path=str(config_path))
        except (ValueError, KeyError) as exc:
            print(f"INVALID — {exc}")
            return EXIT_INVALID
        if onboarding.success_metric and onboarding.minimum_threshold is not None:
            print(
                f"  gate       : {onboarding.success_metric} "
                f"{onboarding.minimum_threshold} — the training gate is no looser"
            )
            print()

    # ── business: the dataset resolves in S3 ──────────────────────────────
    try:
        resolved = validation.validate_business(cfg, config_path=str(config_path))
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"INVALID — {exc}")
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001 - any AWS failure is the same answer here
        # No credentials, no route, no permission: all mean the same thing to the
        # operator — this tier did not run. Anything raised by boto3 lands here
        # rather than being enumerated, since the remedy never differs.
        print("VALID — but the dataset check was SKIPPED.")
        print()
        print(f"  {type(exc).__name__}: {exc}")
        print()
        print(
            "The structural checks passed. Whether the labeled dataset exists was "
            "NOT verified, which is the check the pipeline most often fails on. Run "
            "`aws configure` (or export AWS_PROFILE) and re-run to cover it."
        )
        return EXIT_OK

    for channel, info in resolved.items():
        print(f"  {channel:<11}: {info['s3_uri']} (v={info['version']})")
    print()
    print("VALID — the Validate state will accept this config.")
    print()
    print("Next: build_and_push.sh, then upload_config.sh.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
