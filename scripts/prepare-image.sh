#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# prepare-image.sh — convert a plain root filesystem tarball into a local
# docker image that container2wasm can use as --image.
#
# No RUN steps are executed, so this works for any guest architecture
# (the rootfs is imported as a single layer and the image config carries the
# requested platform + CMD).
#
# Usage:
#   ./scripts/prepare-image.sh --rootfs archriscv.tar.zst --arch riscv64 \
#       --cmd /bin/bash --tag c2w-rootfs-riscv64:latest
#
# Options:
#   --rootfs FILE     rootfs tarball (.tar, .tar.gz, .tar.zst; zstd required for .zst)
#   --arch ARCH       amd64 | riscv64 | aarch64 (maps to --platform linux/<arch>)
#   --cmd CMD         default command of the image (default: /bin/bash)
#   --tag TAG         docker image tag (default: c2w-rootfs-<arch>:latest)
#   --dry-run         print the docker import command without executing it
#
# Requires: docker (>= 25), tar, and zstd for .zst input.
# Prints the image tag on stdout (logs go to stderr).
# ---------------------------------------------------------------------------
set -euo pipefail

log() { printf '[prepare-image] %s\n' "$*" >&2; }
die() { printf '[prepare-image] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  sed -n '2,400p' "${BASH_SOURCE[0]}" \
    | awk '/^set -euo pipefail/{exit} {print}' \
    | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

ROOTFS=""
ARCH=""
CMD="/bin/bash"
TAG=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rootfs)  ROOTFS=${2:?}; shift 2 ;;
    --arch)    ARCH=${2:?}; shift 2 ;;
    --cmd)     CMD=${2:?}; shift 2 ;;
    --tag)     TAG=${2:?}; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -n "$ARCH"   ]] || die "--arch is required (amd64 | riscv64 | aarch64)"
[[ -n "$ROOTFS" ]] || die "--rootfs is required"
[[ -f "$ROOTFS" ]] || die "rootfs tarball not found: $ROOTFS"

case "$ARCH" in
  amd64|x86_64|x64) ARCH=amd64;   PLATFORM=linux/amd64 ;;
  riscv64|rv64)     ARCH=riscv64; PLATFORM=linux/riscv64 ;;
  aarch64|arm64)    ARCH=aarch64; PLATFORM=linux/arm64 ;;
  *) die "unsupported --arch '$ARCH' (amd64 | riscv64 | aarch64)" ;;
esac

TAG=${TAG:-c2w-rootfs-${ARCH}:latest}
TAG=$(printf '%s' "$TAG" | tr '[:upper:]' '[:lower:]')

TMPDIR_BASE=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
TMP=$(mktemp -d "$TMPDIR_BASE/c2w-rootfs.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

INPUT="$ROOTFS"
case "$ROOTFS" in
  *.zst|*.zstd)
    command -v zstd >/dev/null || die "zstd is required to decompress ${ROOTFS} (e.g. apt-get install -y zstd)"
    log "decompressing $(basename "$ROOTFS")"
    zstd -d -f -q -o "$TMP/rootfs.tar" "$ROOTFS"
    INPUT="$TMP/rootfs.tar"
    ;;
esac

tar -tf "$INPUT" >/dev/null 2>&1 || die "$INPUT does not look like a tar archive"
log "rootfs: $INPUT ($(du -h "$INPUT" | cut -f1))"
log "platform: ${PLATFORM}, cmd: ${CMD}"

docker_cmd=(docker import --platform "$PLATFORM"
            --change "CMD [\"${CMD}\"]" "$INPUT" "$TAG")

if [[ "$DRY_RUN" == 1 ]]; then
  printf 'DRY-RUN:'
  printf ' %q' "${docker_cmd[@]}"
  printf '\n'
  echo "$TAG"
  exit 0
fi

command -v docker >/dev/null || die "docker is required"
docker info >/dev/null 2>&1 || die "docker daemon is not reachable"
log "importing rootfs as docker image ${TAG}"
"${docker_cmd[@]}" >/dev/null

info=$(docker image inspect --format '{{.Os}}/{{.Architecture}} {{json .Config.Cmd}}' "$TAG")
log "image imported: ${TAG} (${info})"
echo "$TAG"
