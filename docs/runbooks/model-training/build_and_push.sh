#!/usr/bin/env bash
# Build the model-training images for one model's three pipeline stages
# (preprocessing / training / evaluation) and push them to ECR.
#
# Run from anywhere, with the model's model-training directory exported once (see
# the runbook):
#
#   export INTAKE_DIR=~/pwsa0002037_novoas_ar_sample_models/brand_abc/patient_outcome_mortality_v2/model_training
#   pwsa0002037_novaos_ar/docs/runbooks/model-training/build_and_push.sh
#
#   [stage...]      OPTIONAL. preprocessing | training | evaluation | inference.
#                   Default: preprocessing training evaluation — inference is NOT
#                   built by default (see below) and must be named explicitly.
#   --compute X     OPTIONAL. cpu | gpu. Overrides what compute.train.instance_type implies.
#   --dry-run       Print the composed builder command and exit; build nothing.
#   --no-push       Build only; skip ECR create/login/push.
#   ENV             environment file to read, dev | qa | prod (default: dev)
#
# THIS SCRIPT DERIVES, IT DOES NOT DECIDE. Every argument the builder needs is read
# out of the model's own model-training-config.yaml, because every one of them is
# already authored there:
#
#   identity.model_name          -> --repo-prefix (hyphenated, trailing '-')
#   algorithm.source_script      -> --src-training      + --entry-training
#   evaluation.source_script     -> --src-evaluation    + --entry-evaluation
#   compute.train.instance_type  -> --compute cpu|gpu
#   evaluation.run_bias_check    -> --entry-args-evaluation
#   + bias_config.facet_columns
#   compute.<step>.dockerfile    -> --dockerfile-<stage>, when model-owned
#   compute.<step>.local_requirements -> --requirements-<stage>
#
# INFERENCE IS THE EXCEPTION TO ALL OF THE ABOVE. By default every model is
# served by one of the two SHARED platform images (config.inference_image_ref
# in src/model_training_core/config.py) — this script never builds or pushes
# anything for the inference stage, and the register step keeps resolving one
# of those two images, same as if this stage did not exist. A model whose
# inference.py needs a dependency those shared images do not carry (e.g.
# xgboost) authors compute.inference.local_requirements, exactly like it would
# for train/evaluate; THAT is what switches the register step onto a per-model
# repo instead (see src/pipelines/model_training.py's register step). Build it
# by naming the stage explicitly: `build_and_push.sh inference`. It is never
# included in the "all three" default, so a model with no inference override
# never pays for a build it does not need.
#
# The repo names are per-MODEL: <model-slug>-{preprocessing,training,evaluation,inference}.
# src/model_training_core/naming.py composes them, and the pipeline resolves each
# step's image from that SAME function and the same field — so what is pushed here
# is what the run pulls. Never re-implement that derivation in bash: this script
# calls it.
#
# The tag is `latest` because naming.step_image_ref defaults to it. Per-model
# isolation comes from the repo NAME here, not from the tag, so two models can
# never collide the way one shared repo would.
#
# PATHS IN THE CONFIG resolve against three roots, tried in order, because all three
# are in use across the sample models and the platform carries these fields through
# without resolving them itself (see naming.step_image_ref):
#
#   1. the config's own directory      "algorithm/train.py"      — what the template documents
#   2. the models repo root            "<brand>/<model>/model_training/algorithm/train.py"
#   3. this repo's src/images/         "default_containers/training-cpu/Dockerfile"
#
# Root 3 applies to a dockerfile only, and means something different: the config is
# naming the PLATFORM's own default for that stage, which the builder already builds
# from. No --dockerfile-<stage> is passed in that case — an override pointing at the
# thing it would override is just the default.
#
# PREPROCESSING normally bakes the PLATFORM's prep driver: it is generic, driven
# entirely by the step arguments and the config's preprocessing[] list, so there is
# nothing per-model to author. A model overriding it ships
# local_transformations/preprocessing/ and a compute.preprocess.dockerfile; both are
# picked up automatically when present.
#
# The config's preprocessing[] list never changes an image — it is data, uploaded by
# the pipeline and read at run time. Rebuild when the model's CODE changes.
#
# Everything that varies by environment (account, region) comes from
# cdk_pipeline/environments/<env>.json, never from this script.
set -euo pipefail

INTAKE_DIR="${INTAKE_DIR:-}"
ENV_NAME="${ENV:-dev}"
COMPUTE_OVERRIDE=""
DRY_RUN=0
STAGES=()
PUSH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intake-dir) INTAKE_DIR="$2";        shift 2 ;;
    --config-dir) INTAKE_DIR="$2";        shift 2 ;;
    --compute)    COMPUTE_OVERRIDE="$2";  shift 2 ;;
    --env)        ENV_NAME="$2";          shift 2 ;;
    --dry-run)    DRY_RUN=1;              shift ;;
    --no-push)    PUSH_ARGS+=(--no-push); shift ;;
    -h|--help)    sed -n '2,78p' "$0"; exit 0 ;;
    preprocessing|training|evaluation|inference) STAGES+=("$1"); shift ;;
    *) echo "[fail] unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$INTAKE_DIR" ]]; then
  echo "[fail] no model-training directory. Export INTAKE_DIR (see the runbook)." >&2
  exit 1
fi
# Resolved BEFORE any cd below, because a relative path is relative to where the
# operator ran this from.
[[ -d "$INTAKE_DIR" ]] ||
  { echo "[fail] INTAKE_DIR is not a directory: $INTAKE_DIR" >&2; exit 1; }
INTAKE_DIR="$(cd "$INTAKE_DIR" && pwd)"

CONFIG="$INTAKE_DIR/model-training-config.yaml"
[[ -f "$CONFIG" ]] ||
  { echo "[fail] $INTAKE_DIR has no model-training-config.yaml." >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${REPO_ROOT}/cdk_pipeline/environments/${ENV_NAME}.json"
[[ -f "$ENV_FILE" ]] || { echo "[fail] no such environment file: $ENV_FILE" >&2; exit 1; }

eval "$(python3 - "$ENV_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
print(f'ACCOUNT="{cfg["account"]}"')
print(f'REGION="{cfg["region"]}"')
PY
)"

# One of the three roots a config path may be authored against. Not fatal when the
# directory is not a git clone — only a config using that form then fails.
MODELS_REPO="$(git -C "$INTAKE_DIR" rev-parse --show-toplevel 2>/dev/null || echo "")"

# ── read every builder argument out of the config ─────────────────────────────
# One python block rather than one per field: a second parse could disagree with
# the first if the file changed under us, and the repo prefix has to come from
# naming.step_image_ref rather than from a hyphen substitution done here.
#
# Captured and status-checked BEFORE eval: `eval "$(...)"` reports the exit status of
# eval, not of the substitution, so a block that failed half way through would be
# evalled anyway and leave later variables unset — which `set -u` then reports as a
# bash error rather than as the config problem it is.
if ! DERIVED="$(PYTHONPATH="$REPO_ROOT" python3 - \
    "$CONFIG" "$INTAKE_DIR" "$MODELS_REPO" "$REPO_ROOT" <<'PY'
import os
import pathlib
import shlex
import sys

import yaml

sys.path.insert(0, os.environ["PYTHONPATH"])
from src.model_training_core import naming

config_path = pathlib.Path(sys.argv[1])
intake_dir = pathlib.Path(sys.argv[2])
models_repo = pathlib.Path(sys.argv[3]) if sys.argv[3] else None
platform_images = pathlib.Path(sys.argv[4]) / "src" / "images"
cfg = yaml.safe_load(config_path.read_text()) or {}


def emit(name, value):
    print(f"{name}={shlex.quote(str(value))}")


def resolve_authored_path(rel, *, allow_platform=False):
    """Resolve a path a config authored, or None. See the header for the roots."""
    candidates = [intake_dir / rel]
    if models_repo is not None:
        candidates.append(models_repo / rel)
    if allow_platform:
        candidates.append(platform_images / rel)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    return None


model_name = str((cfg.get("identity") or {}).get("model_name") or "").strip()
if not model_name:
    sys.exit("identity.model_name is missing or empty")
naming.validate_model_name(model_name)

# step_image_ref names each stage's FULL repo independently now
# (novos-ar-base-<cpu|gpu>-job-<stage>-<model-slug>) — cpu vs gpu differ per
# stage, since each stage reads its OWN compute.<step>.instance_type, so there
# is no longer one shared prefix all three stages can share. Pass each stage's
# resolved repo through --repo-<stage> (the builder's per-stage override),
# never --repo-prefix, so the builder's own concatenation never runs.
def _stage_instance_type(step):
    instance = str(
        ((cfg.get("compute") or {}).get(step) or {}).get("instance_type") or ""
    ).strip()
    if not instance:
        sys.exit(f"compute.{step}.instance_type is missing or empty")
    return instance


emit("MODEL_NAME", model_name)
TAG = "latest"
emit("TAG", TAG)
for step, stage in (
    ("preprocess", "PREPROCESSING"),
    ("train", "TRAINING"),
    ("evaluate", "EVALUATION"),
):
    ref = naming.step_image_ref(model_name, step, _stage_instance_type(step), TAG)
    repo, _ = ref.split(":", 1)
    emit(f"REPO_{stage}", repo)

# inference's repo is resolved the SAME way, but only when the model actually
# authors compute.inference.local_requirements or .dockerfile — the same
# either-field condition src/pipelines/model_training.py's register step uses
# to switch off the two shared platform images onto this per-model repo, so a
# config with no override never needs this resolved (and
# compute.inference.instance_type, otherwise optional, is only required at
# this point because it IS set).
_inference_block = (cfg.get("compute") or {}).get("inference") or {}
inference_owned = bool(
    str(_inference_block.get("local_requirements") or "").strip()
    or str(_inference_block.get("dockerfile") or "").strip()
)
if inference_owned:
    ref = naming.step_image_ref(model_name, "inference", _stage_instance_type("inference"), TAG)
    repo, _ = ref.split(":", 1)
    emit("REPO_INFERENCE", repo)
else:
    emit("REPO_INFERENCE", "")


def stage_source(block, key, default_entry):
    """(source dir, entry filename) for a stage, from its source_script.

    The builder wants a directory; source_script names the entry FILE inside it.
    Empty means the stage has no model code to bake — for preprocessing that is
    normal (the platform driver is used); for training and evaluation the builder
    fails, because that code IS the model.
    """
    rel = str((cfg.get(block) or {}).get(key) or "").strip()
    if not rel:
        return "", default_entry
    path = resolve_authored_path(rel)
    if path is None:
        sys.exit(
            f"{block}.{key} is '{rel}' but no such file exists, either relative to "
            f"the config ({intake_dir}) or to the models repo root ({models_repo})."
        )
    return str(path.parent), path.name


src_train, entry_train = stage_source("algorithm", "source_script", "train.py")
src_eval, entry_eval = stage_source("evaluation", "source_script", "evaluate.py")
emit("SRC_TRAINING", src_train)
emit("ENTRY_TRAINING", entry_train)
emit("SRC_EVALUATION", src_eval)
emit("ENTRY_EVALUATION", entry_eval)

# GPU instance families. Everything else is CPU; the builder only uses this to pick
# which Dockerfile a stage builds FROM, never to name an image.
instance = str(
    ((cfg.get("compute") or {}).get("train") or {}).get("instance_type") or ""
).strip()
family = instance.split(".")[1] if instance.count(".") >= 2 else ""
emit("COMPUTE", "gpu" if family.startswith(("p", "g")) else "cpu")

# The evaluate container always emits predictions (the bias step reads
# predictions.csv rather than invoking the model). --bias-attr names the facet the
# Clarify step compares groups across; the builder takes ONE, so a config listing
# several bakes in the first and the rest are reported by the step itself.
evaluation = cfg.get("evaluation") or {}
args = ["--emit-predictions"]
facets = ((evaluation.get("bias_config") or {}).get("facet_columns")) or []
if evaluation.get("run_bias_check") and facets:
    args += ["--bias-attr", str(facets[0])]
emit("ENTRY_ARGS_EVALUATION", " ".join(args))
emit("FACET_COUNT", len(facets))

# Per-step overrides. A model authoring either of these owns that stage's build, so
# the builder must be told — and a path the config names but does not ship is a
# build that would fail confusingly later. inference is included here too: a
# model with no compute.inference.local_requirements never reaches this branch
# (REPO_INFERENCE above is already "" for it), but one that does also gets its
# dockerfile override picked up the same as any other stage.
platform_defaults = []
for step, stage in (
    ("preprocess", "PREPROCESSING"),
    ("train", "TRAINING"),
    ("evaluate", "EVALUATION"),
    ("inference", "INFERENCE"),
):
    block = (cfg.get("compute") or {}).get(step) or {}

    rel = str(block.get("dockerfile") or "").strip()
    if not rel:
        emit(f"DOCKERFILE_{stage}", "")
    else:
        path = resolve_authored_path(rel, allow_platform=True)
        if path is None:
            sys.exit(
                f"compute.{step}.dockerfile is '{rel}' but no such file exists, "
                f"relative to the config ({intake_dir}), the models repo root "
                f"({models_repo}), or this repo's src/images ({platform_images})."
            )
        # Naming the platform's own Dockerfile is not an override: it is what the
        # builder already builds that stage from. Passing it back would be a no-op
        # at best, and at worst pins a path that moves when the platform's tree does.
        if path.is_relative_to(platform_images):
            emit(f"DOCKERFILE_{stage}", "")
            platform_defaults.append(f"{step}={rel}")
        else:
            emit(f"DOCKERFILE_{stage}", str(path))

    rel = str(block.get("local_requirements") or "").strip()
    if not rel:
        emit(f"REQS_{stage}", "")
    else:
        path = resolve_authored_path(rel)
        if path is None:
            sys.exit(
                f"compute.{step}.local_requirements is '{rel}' but no such file "
                f"exists, either relative to the config ({intake_dir}) or to the "
                f"models repo root ({models_repo})."
            )
        emit(f"REQS_{stage}", str(path))

emit("PLATFORM_DEFAULTS", " ".join(platform_defaults))

# Only a model that overrides the platform prep driver ships this tree.
local_prep = intake_dir / "local_transformations" / "preprocessing"
emit("SRC_PREPROCESSING", str(local_prep) if local_prep.is_dir() else "")
PY
)"; then
  echo "[fail] could not read the builder arguments from" >&2
  echo "       $CONFIG" >&2
  exit 1
fi
eval "$DERIVED"

# ── compose the builder invocation ───────────────────────────────────────────
[[ -n "$COMPUTE_OVERRIDE" ]] && COMPUTE="$COMPUTE_OVERRIDE"

BUILDER_DIR="${REPO_ROOT}/src/images"
[[ -f "$BUILDER_DIR/build_and_push.sh" ]] ||
  { echo "[fail] no builder at $BUILDER_DIR/build_and_push.sh" >&2; exit 1; }

ARGS=(
  --repo-preprocessing "$REPO_PREPROCESSING"
  --repo-training "$REPO_TRAINING"
  --repo-evaluation "$REPO_EVALUATION"
  --tag "$TAG"
  --model-name "$MODEL_NAME"
  --compute "$COMPUTE"
  --region "$REGION"
  --account "$ACCOUNT"
  --entry-args-evaluation "$ENTRY_ARGS_EVALUATION"
)
# Only passed when the model owns compute.inference.local_requirements/.dockerfile
# (REPO_INFERENCE is "" otherwise) — building the inference stage with no repo
# name would fall back to the builder's bare "inference" default, too generic to
# push to the shared ECR.
[[ -n "$REPO_INFERENCE" ]] && ARGS+=(--repo-inference "$REPO_INFERENCE")

[[ -n "$SRC_TRAINING" ]]   && ARGS+=(--src-training "$SRC_TRAINING" --entry-training "$ENTRY_TRAINING")
[[ -n "$SRC_EVALUATION" ]] && ARGS+=(--src-evaluation "$SRC_EVALUATION" --entry-evaluation "$ENTRY_EVALUATION")
[[ -n "$SRC_PREPROCESSING" ]] && ARGS+=(--src-preprocessing "$SRC_PREPROCESSING")

for stage in PREPROCESSING TRAINING EVALUATION INFERENCE; do
  lower="$(echo "$stage" | tr '[:upper:]' '[:lower:]')"
  dockerfile="DOCKERFILE_${stage}"; reqs="REQS_${stage}"
  [[ -n "${!dockerfile}" ]] && ARGS+=("--dockerfile-${lower}" "${!dockerfile}")
  [[ -n "${!reqs}" ]]       && ARGS+=("--requirements-${lower}" "${!reqs}")
done

ARGS+=("${PUSH_ARGS[@]}")
# inference is never in the inner builder's default stage list for US specifically
# (it defaults to all of STAGES_ALL, which now includes inference) — pass the
# three-stage default explicitly here when the caller named nothing, so a plain
# `build_and_push.sh` with no stage args still builds exactly what it always did.
if [[ ${#STAGES[@]} -eq 0 ]]; then
  ARGS+=(preprocessing training evaluation)
else
  ARGS+=("${STAGES[@]}")
fi

echo "[info] model          : $MODEL_NAME"
echo "[info] config         : $CONFIG"
echo "[info] repos          : $REPO_PREPROCESSING / $REPO_TRAINING / $REPO_EVALUATION (tag ${TAG})"
echo "[info] registry       : ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
echo "[info] compute        : $COMPUTE${COMPUTE_OVERRIDE:+ (overridden)}"
if [[ ${#STAGES[@]} -gt 0 ]]; then
  echo "[info] stages         : ${STAGES[*]}"
else
  echo "[info] stages         : preprocessing training evaluation"
fi
if [[ -n "$SRC_PREPROCESSING" ]]; then
  echo "[info] prep driver    : $SRC_PREPROCESSING (model override)"
else
  echo "[info] prep driver    : platform (src/processing/training-prep)"
fi
[[ -n "$PLATFORM_DEFAULTS" ]] &&
  echo "[info] platform images: $PLATFORM_DEFAULTS (named in the config; not an override)"
[[ "$FACET_COUNT" -gt 1 ]] &&
  echo "[warn] bias_config.facet_columns lists $FACET_COUNT; the image bakes in the first."

# --build-network sagemaker is mandatory inside SageMaker Studio and harmless
# elsewhere when that network exists; drop it with BUILD_NETWORK="" on a plain host.
BUILD_NETWORK="${BUILD_NETWORK-sagemaker}"
[[ -n "$BUILD_NETWORK" ]] && ARGS+=(--build-network "$BUILD_NETWORK")

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "[info] --dry-run; the builder would be called as:"
  printf '       bash %s/build_and_push.sh' "$BUILDER_DIR"
  # Each flag on its own line WITH its value: one argument per line splits the two
  # apart, which is exactly what a reader is trying to check here. The value that
  # follows a flag is never itself read as one — --entry-args-evaluation's value
  # legitimately starts with '--'.
  expect_value=0
  for arg in "${ARGS[@]}"; do
    if [[ "$expect_value" -eq 1 ]]; then
      printf ' %q' "$arg"
      expect_value=0
      continue
    fi
    case "$arg" in
      --no-push) printf ' \\\n         %s' "$arg" ;;
      --*)       printf ' \\\n         %s' "$arg"; expect_value=1 ;;
      *)         printf ' %q' "$arg" ;;
    esac
  done
  printf '\n'
  exit 0
fi

echo
cd "$BUILDER_DIR"
bash ./build_and_push.sh "${ARGS[@]}"

echo
echo "[ok] pushed $REPO_PREPROCESSING / $REPO_TRAINING / $REPO_EVALUATION :${TAG}"
echo "[ok] the pipeline resolves these same refs from identity.model_name —"
echo "     run_pipeline.sh for ${MODEL_NAME} pulls them."
