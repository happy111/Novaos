#!/bin/bash
#####################################################################
# NOVOS SL — Build a Docker image by name (from config.yaml)
# Usage: ./scripts/build-image.sh <image-name>
#####################################################################
set -e
source ./cdk_pipeline/scripts/common.sh

P="http://proxy-euie.aws.novartis.net:3128"

_should_build() {  # $1 = directory, $2 = image name, $3 = tag
  local dir=$1 img_name=$2 tag=$3
  local force="${FORCE_BUILD:-}"

  case "$force" in
    all|true)
      echo "FORCE_BUILD=$force — bypassing change detection for $img_name."
      return 0 ;;
    *) ;;
  esac

  local entries=()
  IFS=',' read -ra entries <<< "$force"
  for entry in "${entries[@]}"; do
    entry="${entry// /}"
    if [[ "$entry" == "$img_name" ]]; then
      echo "FORCE_BUILD includes '$img_name' — bypassing change detection."
      return 0
    fi
  done

  # Build if the image we would push is not already in ECR — regardless of the folder
  # diff. This covers an image changed in an EARLIER commit of the same push (change
  # detection only spans GIT_PREVIOUS_SUCCESSFUL_COMMIT..HEAD, defaulting to HEAD~1), a
  # cleared/expired tag, or a base-image rebuild. The tag is the folder's last-commit
  # hash, so an UNCHANGED folder keeps the SAME tag and stays in ECR — this branch then
  # does NOT fire, so it never causes perpetual rebuilds.
  if [[ -n "$tag" ]] && ! aws ecr describe-images \
        --repository-name "$ECR_REPO_NAME" \
        --image-ids imageTag="$tag" \
        --region "$REGION" >/dev/null 2>&1; then
    echo "Image tag ${tag} not found in ECR — building ${img_name} regardless of change detection."
    return 0
  fi

  local base="${GIT_PREVIOUS_SUCCESSFUL_COMMIT:-HEAD~1}"
  if ! git rev-parse --verify --quiet "$base" >/dev/null; then
    return 0
  fi
  [[ -n "$(git diff --name-only "$base" HEAD -- "$dir")" ]]
}

_build() {  # $1=tag $2=dockerfile $3=dir ; rest=extra docker build args
  local tag=$1 df=$2 dir=$3; shift 3

  local proxy_args=()
  if [[ -n "${CI}" ]]; then
    proxy_args=(
      --build-arg HTTP_PROXY="$P"  --build-arg HTTPS_PROXY="$P"
      --build-arg http_proxy="$P"  --build-arg https_proxy="$P"
      --build-arg NO_PROXY="169.254.169.254,localhost,novartis.net,*.novartis.net"  # NOSONAR
      --build-arg no_proxy="169.254.169.254,localhost,novartis.net,*.novartis.net"  # NOSONAR
    )
  fi

  docker build \
    -t "${ECR_REPO}:${tag}" \
    -f "$df" \
    --build-arg CACHEBUST="$(git log -1 --format=%h -- "$dir")" \
    "${proxy_args[@]}" \
    "$@" \
    .
  return 0
}

_verify() {  # $1=tag $2=name
  local tag=$1 name=$2
  docker image inspect "${ECR_REPO}:${tag}" >/dev/null 2>&1 \
    || { echo "❌ ${name} image ${ECR_REPO}:${tag} not found locally" >&2; exit 1; }
  echo "✓ ${name} image built: ${ECR_REPO}:${tag}"
  return 0
}

name="${1:-}"
IMAGES_FILE="${IMAGES_FILE:-./cdk_pipeline/config.yaml}"
img=$(yq ".images[] | select(.name == \"$name\")" "$IMAGES_FILE")
if [[ -z "$img" || "$img" == "null" ]]; then
  echo "Usage: $0 <$(yq '.images[].name' "$IMAGES_FILE" | paste -sd '|')>" >&2
  exit 1
fi

dir=$(       yq '.dir'                 <<<"$img")
dockerfile=$(yq '.dockerfile'          <<<"$img")
tag_var=$(   yq '.tag_var'             <<<"$img")
platform=$(  yq '.platform // ""'      <<<"$img")
alias_var=$( yq '.alias_tag_var // ""' <<<"$img")
tag=${!tag_var}

# ── Per-Lambda BASE_IMAGE: resolved from lambdas.<name>.base_image_tag in env JSON ──
if yq -e '.build_args[]?' <<<"$img" | grep -q '^BASE_IMAGE$'; then
  env_file="./cdk_pipeline/environments/${ENVIRONMENT}.json"
  base_tag=$(jq -r ".lambdas[\"${name}\"].base_image_tag // empty" "${env_file}" 2>/dev/null)
  : "${base_tag:?ERROR: lambdas.${name}.base_image_tag is missing in ${env_file} — every image that uses BASE_IMAGE must declare it}"
  export BASE_IMAGE="${ECR_REPO}:${base_tag}"
fi

if ! _should_build "$dir" "$name" "$tag"; then
  echo "⏭  No changes under ${dir}/ since last successful build. Skipping ${name} image build."
  exit 0
fi

build_args=()
for n in $(yq '.build_args[]?' <<<"$img"); do
  build_args+=(--build-arg "$n=${!n}")
done
[[ -n "$platform" ]] && build_args+=(--platform "$platform")

echo "Building ${name} image: ${ECR_REPO}:${tag}"
_build "$tag" "$dockerfile" "$dir" "${build_args[@]}"

if [[ -n "$alias_var" ]]; then
  alias_tag=${!alias_var}
  docker tag "${ECR_REPO}:${tag}" "${ECR_REPO}:${alias_tag}"
  _verify "$tag"       "${name}"
  _verify "$alias_tag" "${name} (alias)"
else
  _verify "$tag" "${name}"
fi
