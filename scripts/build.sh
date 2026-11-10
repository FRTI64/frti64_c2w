#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build.sh — convert a user-provided Docker image into a container2wasm
# "container on web" artifact (out.js + QEMU wasm + .data, or WASI out.wasm).
#
# Designed for GitHub Actions (ubuntu-latest, rootful Docker + buildx) and for
# any Linux machine that has:
#   * docker >= 25 with the buildx plugin
#   * git, curl, tar, python3
# Everything else (container2wasm CLI, the patched/upstream Dockerfile) is
# fetched by this script:
#   * c2w v${C2W_VERSION} release binary  -> tools/bin/c2w
#   * container2wasm v${UPSTREAM_TAG} repo -> .build/container2wasm
#
# Guest kernels (--kernel):
#   builtin                 upstream container2wasm v0.8.5 pin (mainline 6.1,
#                           unmodified upstream Dockerfile)
#   mainline[:VERSION]      mainline stable kernel (default v7.3.3), uses
#                           patches/0001-dockerfile-mainline-kernel.patch
#   lts[:VERSION]           mainline LTS kernel (default v6.18.62)
#   stable[:VERSION]        alias of mainline
#   torvalds[:VERSION]      tag from github.com/torvalds/linux (e.g. v6.1)
#
# Guest instruction sets (--arch): amd64 | riscv64 | aarch64
#   (riscv64 gets the qemu-wasm Zbb workaround by default; disable with
#    --qemu-cpu-opts "")
#
# Source image (--image): the Docker image you want to run on the web, given
# as any registry reference, e.g.:
#   ghcr.io/owner/my-app:latest, docker.io/owner/app:1.2, alpine:3.20,
#   riscv64/alpine:3.20, python:3.12-slim, archlinux:latest
# c2w pulls it with `docker pull --platform=linux/<arch>`.
# If you already have the image locally (docker load / docker build), just pass
# its tag. For a plain rootfs tarball use --image-rootfs FILE [--image-cmd CMD]
# (converted into a local docker image via scripts/prepare-image.sh).
#
# Examples:
#   ./scripts/build.sh -a amd64   -i alpine:3.20         -k mainline:v7.3.3 -o out/x86
#   ./scripts/build.sh -a riscv64 -i riscv64/alpine:3.20 -k mainline         -o out/rv
#   ./scripts/build.sh -a amd64   -i alpine:3.20         -k builtin          -o out/x86-61
#   ./scripts/build.sh -a aarch64 -i arm64v8/alpine:3.20 -k mainline         -o out/arm64
#   ./scripts/build.sh -a amd64   -i alpine:3.20 -k mainline --wasi -o out/wasi
#
# Extra environment variables:
#   C2W_VERSION       container2wasm release tag (default v0.8.5)
#   UPSTREAM_TAG      container2wasm git tag for the Dockerfile (default v0.8.5)
#   C2W_REPO          container2wasm git URL
#   C2W_DOCKER_VERSION  recorded in build-manifest.json (CI sets it)
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export C2W_VERSION=${C2W_VERSION:-v0.8.5}
export UPSTREAM_TAG=${UPSTREAM_TAG:-v0.8.5}
C2W_REPO=${C2W_REPO:-https://github.com/container2wasm/container2wasm.git}
STABLE_URL=${STABLE_URL:-https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git}
TORVALDS_URL=${TORVALDS_URL:-https://github.com/torvalds/linux}
MAINLINE_DEFAULT=${MAINLINE_DEFAULT:-v7.3.3}
LTS_DEFAULT=${LTS_DEFAULT:-v6.18.62}

TOOLS=$ROOT/tools
C2W=$TOOLS/bin/c2w
BUILD=$ROOT/.build
UPSTREAM=$BUILD/container2wasm
PRISTINE=$UPSTREAM/.c2w-upstream-Dockerfile

ARCH=""
IMAGE=""
IMAGE_ROOTFS=""
IMAGE_CMD="/bin/bash"
KERNEL="mainline"
KERNEL_VERSION=""
OUT=""
TO_JS=1
BUILDER="docker"
DRY_RUN=0
FORCE_UPSTREAM=0
GUEST_RAM=""
CORES=""
QEMU_CPU_OPTS=""
QEMU_CPU_OPTS_SET=0
EXTRA_BUILD_ARGS=()

log()  { printf '[build] %s\n' "$*" >&2; }
die()  { printf '[build] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  sed -n '2,400p' "${BASH_SOURCE[0]}" \
    | awk '/^set -euo pipefail/{exit} {print}' \
    | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -a|--arch)        ARCH=${2:?}; shift 2 ;;
    -i|--image)       IMAGE=${2:?}; shift 2 ;;
    -k|--kernel)      KERNEL=${2:?}; shift 2 ;;
    --kernel-version) KERNEL_VERSION=${2:?}; shift 2 ;;
    -o|--out|--output) OUT=${2:?}; shift 2 ;;
    --image-rootfs)   IMAGE_ROOTFS=${2:?}; shift 2 ;;
    --image-cmd|--set-cmd) IMAGE_CMD=${2:?}; shift 2 ;;
    --to-js)          TO_JS=1; shift ;;
    --wasi)           TO_JS=0; shift ;;
    --builder)        BUILDER=${2:?}; shift 2 ;;
    --guest-ram)      GUEST_RAM=${2:?}; shift 2 ;;
    --cores)          CORES=${2:?}; shift 2 ;;
    --qemu-cpu-opts)  QEMU_CPU_OPTS="${2-}"; QEMU_CPU_OPTS_SET=1; shift 2 ;;
    --build-arg)      EXTRA_BUILD_ARGS+=("${2:?}"); shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --force-upstream) FORCE_UPSTREAM=1; shift ;;
    -h|--help)        usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# architecture matrix
# ---------------------------------------------------------------------------
[[ -n "$ARCH" ]] || die "--arch is required (amd64 | riscv64 | aarch64)"
case "$ARCH" in
  amd64|x86_64|x64) ARCH=amd64;   PLATFORM=linux/amd64 ;;
  riscv64|rv64)     ARCH=riscv64; PLATFORM=linux/riscv64 ;;
  aarch64|arm64)    ARCH=aarch64; PLATFORM=linux/arm64 ;;
  *) die "unsupported --arch '$ARCH' (amd64 | riscv64 | aarch64)" ;;
esac

# ---------------------------------------------------------------------------
# kernel matrix
# ---------------------------------------------------------------------------
KERNEL_MODE=builtin
LINUX_VERSION=""
LINUX_GIT_URL="$STABLE_URL"
k_kind="${KERNEL%%:*}"
k_tag=""
[[ "$KERNEL" == *:* ]] && k_tag="${KERNEL#*:}"
case "$k_kind" in
  builtin|upstream)
    KERNEL_MODE=builtin; KERNEL_LABEL="builtin-6.1" ;;
  mainline|stable)
    KERNEL_MODE=mainline
    LINUX_VERSION="${k_tag:-${KERNEL_VERSION:-$MAINLINE_DEFAULT}}"
    KERNEL_LABEL="mainline-${LINUX_VERSION}" ;;
  lts)
    KERNEL_MODE=mainline
    LINUX_VERSION="${k_tag:-${KERNEL_VERSION:-$LTS_DEFAULT}}"
    KERNEL_LABEL="lts-${LINUX_VERSION}" ;;
  torvalds)
    KERNEL_MODE=mainline
    LINUX_VERSION="${k_tag:-${KERNEL_VERSION:-v6.1}}"
    LINUX_GIT_URL="$TORVALDS_URL"
    KERNEL_LABEL="torvalds-${LINUX_VERSION}" ;;
  v[0-9]*)
    KERNEL_MODE=mainline; LINUX_VERSION="$KERNEL"
    KERNEL_LABEL="mainline-${LINUX_VERSION}" ;;
  config)
    die "custom .config is not supported by the slim CI script; supported: builtin, mainline[:tag], lts[:tag], stable[:tag], torvalds[:tag]" ;;
  *)
    die "unsupported --kernel '$KERNEL' (builtin | mainline[:tag] | lts[:tag] | stable[:tag] | torvalds[:tag])" ;;
esac
if [[ "$KERNEL_MODE" == mainline ]]; then
  [[ "$LINUX_VERSION" =~ ^v[0-9]+\.[0-9]+(\.[0-9]+)?(-rc[0-9]+)?$ ]] \
    || die "invalid kernel version '$LINUX_VERSION' (expected e.g. v7.3.3)"
  KERNEL_DESC="${LINUX_VERSION} from ${LINUX_GIT_URL}"
else
  KERNEL_DESC="upstream container2wasm pin (mainline 6.1)"
fi

# ---------------------------------------------------------------------------
# source image
# ---------------------------------------------------------------------------
if [[ -n "$IMAGE_ROOTFS" ]]; then
  [[ -z "$IMAGE" ]] || die "use either --image or --image-rootfs, not both"
  [[ -f "$IMAGE_ROOTFS" ]] || die "rootfs tarball not found: $IMAGE_ROOTFS"
  IMAGE="c2w-rootfs-${ARCH}:latest"
  PREPARE_IMAGE=1
else
  [[ -n "$IMAGE" ]] || die "--image is required (or --image-rootfs FILE)"
  PREPARE_IMAGE=0
fi

# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
if [[ -z "$OUT" ]]; then
  img_slug=$(printf '%s' "$IMAGE" | tr '/:@ ' '----' | tr -cd 'A-Za-z0-9._-')
  if [[ "$img_slug" == "${ARCH}"* || "$img_slug" == x86_64* || "$img_slug" == arm64* ]]; then
    OUT="$ROOT/out/${img_slug}-${KERNEL_LABEL}"
  else
    OUT="$ROOT/out/${ARCH}-${img_slug}-${KERNEL_LABEL}"
  fi
fi
if [[ "$TO_JS" == 1 ]]; then
  OUT_ARG="${OUT}/"
else
  OUT_ARG="${OUT}/out.wasm"
fi

build_args_string() {
  local out=()
  if [[ "$KERNEL_MODE" == mainline ]]; then
    out+=("LINUX_VERSION=${LINUX_VERSION}")
    out+=("LINUX_GIT_URL=${LINUX_GIT_URL}")
  fi
  if [[ "$ARCH" == riscv64 && "$KERNEL_MODE" == mainline ]]; then
    # qemu-wasm (QEMU 8.2 wasm JIT) mis-emulates RISC-V Zbb and Linux >= 6.4
    # uses Zbb-optimized string routines; hide Zbb from the emulated CPU.
    # Set QEMU_RISCV_CPU_OPTS="" (--qemu-cpu-opts "") to disable.
    local cpu_opts="rv64,zbb=false"
    if [[ "$QEMU_CPU_OPTS_SET" == 1 ]]; then
      cpu_opts="$QEMU_CPU_OPTS"
    fi
    out+=("QEMU_RISCV_CPU_OPTS=${cpu_opts}")
  fi
  [[ -n "$GUEST_RAM" ]] && out+=("VM_MEMORY_SIZE_MB=${GUEST_RAM}")
  [[ -n "$CORES" ]]     && out+=("VM_CORE_NUMS=${CORES}")
  if [[ ${#EXTRA_BUILD_ARGS[@]} -gt 0 ]]; then
    out+=("${EXTRA_BUILD_ARGS[@]}")
  fi
  printf '%s\n' "${out[@]+"${out[@]}"}"
}

ensure_c2w() {
  if [[ -x "$C2W" ]]; then return 0; fi
  command -v curl >/dev/null || die "curl is required to download c2w"
  command -v tar  >/dev/null || die "tar is required to extract c2w"
  local host_arch url tmp
  case "$(uname -m)" in
    x86_64)        host_arch=amd64 ;;
    aarch64|arm64) host_arch=arm64 ;;
    *) die "unsupported host arch for c2w release binary: $(uname -m)" ;;
  esac
  mkdir -p "$TOOLS/bin" "$TOOLS/tmp"
  url="https://github.com/container2wasm/container2wasm/releases/download/${C2W_VERSION}/container2wasm-${C2W_VERSION}-linux-${host_arch}.tar.gz"
  log "downloading c2w ${C2W_VERSION}: ${url}"
  tmp="$TOOLS/tmp/c2w.tar.gz"
  curl -fsSL -o "$tmp" "$url"
  tar xzf "$tmp" -C "$TOOLS/bin"
  rm -f "$tmp"
  chmod +x "$C2W" 2>/dev/null || true
  [[ -x "$C2W" ]] || die "c2w binary not found after download: $C2W"
}

ensure_upstream() {
  command -v git >/dev/null || die "git is required to fetch container2wasm upstream"
  if [[ "$FORCE_UPSTREAM" == 1 && -d "$UPSTREAM" ]]; then
    log "removing cached upstream checkout (--force-upstream)"
    rm -rf "$UPSTREAM"
  fi
  if [[ ! -d "$UPSTREAM/.git" ]]; then
    mkdir -p "$BUILD"
    log "cloning container2wasm ${UPSTREAM_TAG}"
    rm -rf "$UPSTREAM"
    git -c advice.detachedHead=false clone --depth 1 --branch "$UPSTREAM_TAG" "$C2W_REPO" "$UPSTREAM"
  fi
  [[ -f "$PRISTINE" ]] || cp "$UPSTREAM/Dockerfile" "$PRISTINE"
}

make_dockerfile() {
  if [[ "$KERNEL_MODE" == builtin ]]; then
    DOCKERFILE="$PRISTINE"
    return 0
  fi
  local patch="$ROOT/patches/0001-dockerfile-mainline-kernel.patch"
  [[ -f "$patch" ]] || die "missing patch: $patch"
  cp "$PRISTINE" "$UPSTREAM/Dockerfile"
  log "applying $(basename "$patch")"
  if command -v git >/dev/null; then
    ( cd "$UPSTREAM" && git apply -p1 "$patch" )
  else
    ( cd "$UPSTREAM" && patch -p1 < "$patch" )
  fi
  DOCKERFILE="$UPSTREAM/Dockerfile"
}

# ---------------------------------------------------------------------------
# summary + dry-run
# ---------------------------------------------------------------------------
log "arch        : ${ARCH} (${PLATFORM})"
log "kernel      : ${KERNEL_LABEL} (${KERNEL_DESC})"
log "image       : ${IMAGE}${IMAGE_ROOTFS:+ (from rootfs ${IMAGE_ROOTFS})}"
log "output      : ${OUT}"
log "mode        : $([[ "$TO_JS" == 1 ]] && echo "--to-js (browser)" || echo "--wasi")"
log "builder     : ${BUILDER}"

if [[ "$DRY_RUN" == 1 ]]; then
  ensure_c2w
  ensure_upstream
  make_dockerfile
  BUILD_ARGS=$(build_args_string)
  CMD=( "$C2W" --dockerfile "$DOCKERFILE" --target-arch "$ARCH" )
  [[ "$TO_JS" == 1 ]] && CMD+=(--to-js)
  CMD+=(--builder "$BUILDER")
  while IFS= read -r ba; do [[ -n "$ba" ]] && CMD+=(--build-arg "$ba"); done <<< "$BUILD_ARGS"
  CMD+=( "$IMAGE" "$OUT_ARG" )
  log "dockerfile  : ${DOCKERFILE}"
  log "build args  : ${BUILD_ARGS:-<none>}"
  log "command     :"
  printf '  %q' "${CMD[@]}" >&2; printf '\n' >&2
  [[ "$PREPARE_IMAGE" == 1 ]] && log "would import rootfs: ${IMAGE_ROOTFS} -> ${IMAGE}"
  exit 0
fi

# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------
ensure_c2w
ensure_upstream
make_dockerfile

if [[ "$PREPARE_IMAGE" == 1 ]]; then
  log "converting rootfs tarball to a local docker image"
  "$ROOT/scripts/prepare-image.sh" --rootfs "$IMAGE_ROOTFS" --arch "$ARCH" \
      --cmd "$IMAGE_CMD" --tag "$IMAGE" >/dev/null
fi

command -v docker >/dev/null || die "docker is required"
docker buildx version >/dev/null 2>&1 || die "docker buildx plugin is required"
docker info >/dev/null 2>&1 || die "docker daemon is not reachable"

if [[ -e "$OUT" ]]; then
  log "removing existing output directory ${OUT}"
  rm -rf "$OUT"
fi
mkdir -p "$OUT"

# ---------------------------------------------------------------------------
# run container2wasm
# ---------------------------------------------------------------------------
CMD=( "$C2W" --dockerfile "$DOCKERFILE" --target-arch "$ARCH" )
[[ "$TO_JS" == 1 ]] && CMD+=(--to-js)
CMD+=(--builder "$BUILDER")
BUILD_ARGS=$(build_args_string)
while IFS= read -r ba; do
  [[ -n "$ba" ]] && CMD+=(--build-arg "$ba")
done <<< "$BUILD_ARGS"
CMD+=( "$IMAGE" "$OUT_ARG" )

log "running container2wasm (this pulls the c2w build toolchain images and can take 20-45 min):"
printf '  %q' "${CMD[@]}" >&2; printf '\n' >&2
start=$(date +%s)
"${CMD[@]}"
elapsed=$(( $(date +%s) - start ))
log "c2w finished in ${elapsed}s"

# ---------------------------------------------------------------------------
# verify artifacts + manifest
# ---------------------------------------------------------------------------
if [[ "$TO_JS" == 1 ]]; then
  for f in out.js load.js arg-module.js; do
    [[ -f "$OUT/$f" ]] || die "expected artifact missing: $OUT/$f"
  done
  compgen -G "$OUT/qemu-system-*.wasm" >/dev/null || die "no qemu-system-*.wasm artifact in $OUT"
else
  [[ -f "$OUT/out.wasm" ]] || die "expected artifact missing: $OUT/out.wasm"
fi

( cd "$OUT" && sha256sum ./* > SHA256SUMS )

python3 - "$OUT" "$ARCH" "$PLATFORM" "$IMAGE" "$KERNEL_LABEL" "$KERNEL_MODE" \
         "$LINUX_VERSION" "$LINUX_GIT_URL" "$DOCKERFILE" "$elapsed" <<'PY'
import hashlib, json, os, sys, time
(out, arch, platform, image, klabel, kmode,
 kver, kurl, df, elapsed) = sys.argv[1:12]
artifacts = []
for name in sorted(os.listdir(out)):
    if name in ("build-manifest.json", "SHA256SUMS"):
        continue
    p = os.path.join(out, name)
    if not os.path.isfile(p):
        continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    artifacts.append({"name": name, "size": os.path.getsize(p), "sha256": h.hexdigest()})
manifest = {
    "schema": "c2w-github-actions-build/v1",
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "arch": arch,
    "platform": platform,
    "image": image,
    "kernel": {
        "label": klabel,
        "mode": kmode,
        "version": kver if kmode == "mainline" else "6.1 (upstream container2wasm pin)",
        "git_url": kurl if kmode == "mainline" else "https://github.com/torvalds/linux",
    },
    "container2wasm": {
        "version": os.environ.get("C2W_VERSION", "v0.8.5"),
        "dockerfile": df,
        "dockerfile_sha256": hashlib.sha256(open(df, "rb").read()).hexdigest(),
    },
    "docker": os.environ.get("C2W_DOCKER_VERSION", ""),
    "github": {
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "job": os.environ.get("GITHUB_JOB", ""),
    },
    "build_seconds": int(elapsed),
    "artifacts": artifacts,
}
p = os.path.join(out, "build-manifest.json")
with open(p, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
print("[build] wrote", p)
PY

log "artifacts:"
( cd "$OUT" && ls -la )
log "done: ${OUT}"
