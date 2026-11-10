#!/usr/bin/env python3
"""compute-matrix.py -- turn workflow_dispatch inputs into the build matrix.

This is the input layer of .github/workflows/build.yml.  It reads the
``INPUT_*`` environment variables that the workflow passes from `inputs.*`,
validates them, and writes two GitHub Actions outputs:

    matrix=<json>            # {"include": [<build entry>, ...]}
    retention_days=<1..90>

When GITHUB_STEP_SUMMARY is set it also appends a human-readable table so a
dispatch shows exactly what was resolved (and why) before the 20-45 minute
builds start.

Design rules:
  * every invalid combination fails fast, before a runner starts a build,
    with a `::error::` annotation pointing at the workflow file;
  * all capabilities of scripts/build.sh stay reachable from the dispatch UI;
  * unknown matrix-entry fields are rejected so typos (e.g. "images") do not
    silently fall back to defaults;
  * nothing here needs network access, so it is unit-testable locally:

        python3 scripts/compute-matrix.py --self-test
        INPUT_IMAGE=alpine:3.20 python3 scripts/compute-matrix.py

Environment inputs (all optional; empty means "use default"):
    INPUT_SOURCE           auto | registry | image-tar | rootfs-tar | matrix | demo
    INPUT_IMAGE            one or more registry refs, comma/whitespace separated
    INPUT_ARCH             amd64 | riscv64 | aarch64 (aliases: x86_64, arm64, ...)
    INPUT_OUTPUT_KIND      to-js | wasi
    INPUT_KERNEL           kernel family: mainline | lts | stable | torvalds |
                           builtin  (legacy "mainline:v6.12.50" also accepted)
    INPUT_KERNEL_VERSION   kernel tag for the family, e.g. v6.12.50
    INPUT_NAME             artifact name (single-image modes only)
    INPUT_GUEST_RAM        guest RAM in MB
    INPUT_CORES            guest CPU cores
    INPUT_IMAGE_TAR_URL    URL of a `docker save` tarball
    INPUT_IMAGE_TAR_NAME   tag to use after `docker load`
    INPUT_ROOTFS_TAR_URL   URL of a rootfs tarball
    INPUT_ROOTFS_CMD       default command for rootfs images
    INPUT_MATRIX           JSON array of matrix entries (advanced batch mode)
    INPUT_RETENTION_DAYS   artifact retention, 1-90
    INPUT_EXPERIMENTAL     true/false: keep the run green if a build fails

Matrix entry schema (all fields optional except exactly one image source):
    arch, kernel, kernel_version, kernel_family, mode, output_kind, name,
    experimental, guest_ram, cores, qemu_cpu_opts,
    image | image_tar_url | image_rootfs_url, image_tar_name, image_cmd,
    rootfs_tar_url, rootfs_cmd (last two are aliases).

Exit codes: 0 = ok, 1 = invalid input or self-test failure.
"""

import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
ARCH_ALIASES = {
    "amd64": "amd64", "x86_64": "amd64", "x64": "amd64",
    "riscv64": "riscv64", "rv64": "riscv64",
    "aarch64": "aarch64", "arm64": "aarch64",
}

MODE_ALIASES = {
    "to-js": "to-js", "to_js": "to-js", "tojs": "to-js", "browser": "to-js",
    "wasi": "wasi",
}

KERNEL_FAMILIES = ("mainline", "lts", "stable", "torvalds", "builtin")
KERNEL_VERSION_RE = re.compile(r"^v\d+\.\d+(?:\.\d+)?(?:-rc\d+)?$")

IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9\[][A-Za-z0-9._/:@+\[\]-]*$")  # cheap gate
REPO_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$")
REGISTRY_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:]+\])"
    r"(?::[0-9]+)?$"
)
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
DIGEST_RE = re.compile(r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[A-Fa-f0-9]{32,}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
CMD_RE = re.compile(r"^[A-Za-z0-9._/+=:, -]{1,256}$")

SOURCES = ("auto", "registry", "image-tar", "rootfs-tar", "matrix", "demo")

ENTRY_KEYS = (
    "arch", "kernel", "mode", "name", "experimental",
    "image", "image_tar_url", "image_tar_name",
    "image_rootfs_url", "image_cmd",
    "guest_ram", "cores", "qemu_cpu_opts",
)

ENTRY_ALIASES = {
    "output_kind": "mode",
    "rootfs_tar_url": "image_rootfs_url",
    "rootfs_cmd": "image_cmd",
}

ALLOWED_ENTRY_KEYS = set(ENTRY_KEYS) | set(ENTRY_ALIASES) | {"kernel_version", "kernel_family"}

# Built-in demo: one small image per guest arch.  Only the image and arch are
# pinned; kernel/output/tuning come from the dispatch defaults.
DEFAULT_MATRIX = [
    {"arch": "amd64", "image": "alpine:3.20", "name": "amd64-alpine"},
    {"arch": "riscv64", "image": "riscv64/alpine:3.20", "name": "riscv64-alpine"},
    {"arch": "aarch64", "image": "arm64v8/alpine:3.20", "name": "aarch64-alpine",
     "experimental": True},
]

DEFAULT_RETENTION_DAYS = 14
DEFAULT_ROOTFS_CMD = "/bin/bash"
DEFAULT_TAR_NAME = "c2w-user-image:latest"


class MatrixError(Exception):
    """Invalid workflow input; message is shown to the user."""


def fail(message):
    raise MatrixError(message)


def log(message):
    print(f"[matrix] {message}", file=sys.stderr)


def warn(message):
    print(f"[matrix] warning: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# scalar validators
# ---------------------------------------------------------------------------
def _text(value, default=""):
    """Coerce an env/JSON value to a stripped string (booleans -> 'true')."""
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def as_bool(value, where, default=False):
    if isinstance(value, bool):
        return value
    raw = _text(value).lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    fail(f"{where}: expected a boolean (true/false), got {_text(value)!r}")


def as_count(value, where, low, high, default=None):
    """Parse an optional non-negative integer with bounds; '' -> default."""
    raw = _text(value)
    if raw == "":
        return default
    if raw.endswith(".0"):  # JSON numbers such as 14.0
        raw = raw[:-2]
    if not raw.isdigit():
        fail(f"{where}: expected a whole number, got {_text(value)!r}")
    number = int(raw)
    if not low <= number <= high:
        fail(f"{where}: must be between {low} and {high}, got {number}")
    return number


def validate_image_ref(value, where):
    """Validate a docker image reference: [registry[:port]/]repo[/repo][:tag][@algo:hex]."""
    if len(value) > 512:
        fail(f"{where}: image reference is too long (max 512 chars)")
    if not IMAGE_REF_RE.fullmatch(value):
        fail(f"{where}: invalid image reference {value!r} "
             "(expected e.g. ghcr.io/owner/app:latest)")
    reference = value
    if "@" in reference:
        reference, digest = reference.rsplit("@", 1)
        if not DIGEST_RE.fullmatch(digest):
            fail(f"{where}: invalid image digest {digest!r} "
                 "(expected <algorithm>:<32+ hex chars>, e.g. sha256:<64 hex>)")
    tail = reference.rsplit("/", 1)[-1]
    if ":" in tail:
        reference, tag = reference.rsplit(":", 1)
        if not TAG_RE.fullmatch(tag):
            fail(f"{where}: invalid image tag {tag!r} "
                 "(letters, digits, '_', '.', '-'; max 128 chars)")
    components = reference.split("/")
    if len(components) > 1 and ("." in components[0] or ":" in components[0]
                                or components[0] == "localhost"):
        registry = components.pop(0)
        if not REGISTRY_RE.fullmatch(registry):
            fail(f"{where}: invalid registry host {registry!r}")
    if not components or not all(REPO_COMPONENT_RE.fullmatch(part) for part in components):
        fail(f"{where}: invalid image repository {reference!r} "
             "(lowercase letters, digits, '.', '_', '-'); "
             "expected e.g. ghcr.io/owner/app:latest")


def validate_tag(value, where):
    validate_image_ref(value, where)


def validate_url(value, where):
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        fail(f"{where}: expected an http(s) URL, got {value!r}")


def validate_cmd(value, where):
    if not CMD_RE.fullmatch(value):
        fail(f"{where}: unsupported command {value!r} "
             "(use a plain command without quotes/backslashes, e.g. /bin/bash)")


def validate_name(value, where):
    if not NAME_RE.fullmatch(value):
        fail(f"{where}: invalid artifact name {value!r} "
             "(letters, digits, '.', '_', '-'; must start alphanumeric; max 100 chars)")
    return value


def slugify(value):
    """Filesystem/artifact-safe slug, stable and collision-resistant on truncation."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._").lower()
    if not slug:
        slug = "image"
    if len(slug) > 64:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        slug = slug[:48].rstrip("-._") + "-" + digest
    return slug


def compose_kernel(family, version, where, version_is_explicit=True):
    """Return the kernel spec string understood by scripts/build.sh."""
    fam = _text(family).lower() or "mainline"
    if fam not in KERNEL_FAMILIES:
        fail(f"{where}: unsupported kernel family {fam!r} "
             f"(expected {' | '.join(KERNEL_FAMILIES)})")
    ver = _text(version)
    if fam == "builtin":
        if ver and version_is_explicit:
            fail(f"{where}: the 'builtin' kernel takes no kernel_version "
                 "(it is upstream's pinned mainline 6.1)")
        if ver:
            warn(f"{where}: 'builtin' kernel ignores the inherited kernel_version {ver!r}")
        return "builtin"
    if not ver:
        # Leave the tag out: scripts/build.sh applies its per-family default.
        return fam
    normalized = ver if ver.startswith("v") else "v" + ver
    if not KERNEL_VERSION_RE.fullmatch(normalized):
        fail(f"{where}: invalid kernel version {ver!r} (expected e.g. v6.12.50)")
    return f"{fam}:{normalized}"


# ---------------------------------------------------------------------------
# matrix entry normalization
# ---------------------------------------------------------------------------
def normalize_entry(raw, where, defaults):
    """Validate one build entry and give it every key the build job consumes."""
    if not isinstance(raw, dict):
        fail(f"{where}: matrix entry must be a JSON object")

    unknown = sorted(set(raw) - ALLOWED_ENTRY_KEYS)
    if unknown:
        fail(f"{where}: unknown field(s) {', '.join(unknown)} "
             f"(allowed: {', '.join(sorted(ALLOWED_ENTRY_KEYS))})")

    entry = dict(raw)
    for alias, canonical in ENTRY_ALIASES.items():
        if alias not in entry:
            continue
        if canonical in entry:
            fail(f"{where}: use either {alias} or {canonical}, not both")
        entry[canonical] = entry.pop(alias)

    def value(key, default=""):
        # An explicit "" (or null) in a matrix entry means "not set": fall
        # back to the dispatch-level default, mirroring resolve().get().
        text = _text(entry.get(key))
        return text if text else _text(default)

    # -- arch ---------------------------------------------------------------
    arch_raw = value("arch", defaults.get("arch", "amd64")).lower()
    if arch_raw not in ARCH_ALIASES:
        fail(f"{where}: unsupported arch {arch_raw!r} (amd64 | riscv64 | aarch64)")
    arch = ARCH_ALIASES[arch_raw]

    # -- kernel -------------------------------------------------------------
    # Conflict detection uses the raw entry values, not the dispatch defaults.
    kernel_field = _text(entry.get("kernel"))
    family_field = _text(entry.get("kernel_family"))
    if kernel_field and family_field:
        fail(f"{where}: use either kernel or kernel_family, not both")
    family = family_field or kernel_field or defaults.get("kernel") or "mainline"
    raw_version = _text(entry.get("kernel_version"))
    explicit_version = ""
    if ":" in family:
        if raw_version:
            fail(f"{where}: kernel already contains a version; "
                 "leave kernel_version empty")
        family, explicit_version = family.split(":", 1)
        family = family.strip().lower()
        explicit_version = explicit_version.strip()
    explicit_version = explicit_version or raw_version
    version = explicit_version or defaults.get("kernel_version", "")
    kernel = compose_kernel(family, version, where, version_is_explicit=bool(explicit_version))

    # -- output mode --------------------------------------------------------
    mode_field = _text(entry.get("mode"))
    output_kind_field = _text(entry.get("output_kind"))
    if mode_field and output_kind_field:
        fail(f"{where}: use either mode or output_kind, not both")
    mode_raw = (mode_field or output_kind_field or defaults.get("mode", "to-js")).lower()
    if mode_raw not in MODE_ALIASES:
        fail(f"{where}: unsupported output kind {mode_raw!r} (to-js | wasi)")
    mode = MODE_ALIASES[mode_raw]
    if arch == "aarch64" and mode == "wasi":
        fail(f"{where}: aarch64 supports to-js only (WASI output exists for amd64/riscv64)")

    # -- exactly one image source ------------------------------------------
    image = value("image")
    tar_url = value("image_tar_url")
    rootfs_url = value("image_rootfs_url")
    provided = [name for name, field in
                (("image", image), ("image_tar_url", tar_url),
                 ("image_rootfs_url", rootfs_url)) if field]
    if len(provided) != 1:
        fail(f"{where}: exactly one of image / image_tar_url / image_rootfs_url "
             f"is required (got {', '.join(provided) or 'none'})")

    configured_tar_name = value("image_tar_name")
    configured_cmd = value("image_cmd")
    image_tar_name = ""
    image_cmd = ""
    if image:
        validate_image_ref(image, f"{where}.image")
    if tar_url:
        validate_url(tar_url, f"{where}.image_tar_url")
        image_tar_name = configured_tar_name or DEFAULT_TAR_NAME
        validate_tag(image_tar_name, f"{where}.image_tar_name")
    if rootfs_url:
        validate_url(rootfs_url, f"{where}.image_rootfs_url")
        image_cmd = configured_cmd or DEFAULT_ROOTFS_CMD
        validate_cmd(image_cmd, f"{where}.image_cmd")
    if configured_tar_name and not tar_url:
        fail(f"{where}: image_tar_name only applies to image_tar_url entries")
    if configured_cmd and not rootfs_url:
        fail(f"{where}: image_cmd/rootfs_cmd only applies to image_rootfs_url entries")

    # -- runtime tuning -----------------------------------------------------
    guest_ram = as_count(value("guest_ram", defaults.get("guest_ram", "")),
                         f"{where}.guest_ram", 64, 65536)
    cores = as_count(value("cores", defaults.get("cores", "")),
                     f"{where}.cores", 1, 64)
    qemu_cpu_opts = value("qemu_cpu_opts", defaults.get("qemu_cpu_opts", ""))
    if "\n" in qemu_cpu_opts:
        fail(f"{where}.qemu_cpu_opts must be a single line")
    if qemu_cpu_opts and arch != "riscv64":
        warn(f"{where}: qemu_cpu_opts is only used for riscv64 builds; ignored")
    elif qemu_cpu_opts and kernel == "builtin":
        warn(f"{where}: qemu_cpu_opts is only used with the mainline kernel; "
             "the builtin kernel ignores it")

    # -- name ---------------------------------------------------------------
    name = value("name")
    if not name:
        name = f"{arch}-{slugify(image or tar_url or rootfs_url)}"
    name = validate_name(name, f"{where}.name")

    # -- experimental -------------------------------------------------------
    if _text(entry.get("experimental")):
        experimental = as_bool(entry["experimental"], f"{where}.experimental")
    else:
        experimental = bool(defaults.get("experimental", False))

    return {
        "arch": arch,
        "kernel": kernel,
        "mode": mode,
        "name": name,
        "experimental": experimental,
        "image": image,
        "image_tar_url": tar_url,
        "image_tar_name": image_tar_name,
        "image_rootfs_url": rootfs_url,
        "image_cmd": image_cmd,
        "guest_ram": str(guest_ram) if guest_ram is not None else "",
        "cores": str(cores) if cores is not None else "",
        "qemu_cpu_opts": qemu_cpu_opts,
    }


# ---------------------------------------------------------------------------
# dispatch input resolution
# ---------------------------------------------------------------------------
def resolve(environ=None):
    """Return (matrix_entries, retention_days, source) for the given env."""
    env = os.environ if environ is None else environ

    def get(name, default=""):
        # `inputs.*` render as empty strings (not missing) on push/pull_request
        # runs and for omitted dispatch inputs, so empty means "use default".
        text = _text(env.get(name))
        return text if text else default

    # -- common defaults (used when a matrix entry omits a field) ----------
    arch_raw = get("INPUT_ARCH", "amd64").lower()
    if arch_raw not in ARCH_ALIASES:
        fail(f"input arch: unsupported arch {arch_raw!r} (amd64 | riscv64 | aarch64)")
    mode_raw = get("INPUT_OUTPUT_KIND", "to-js").lower()
    if mode_raw not in MODE_ALIASES:
        fail(f"input output_kind: unsupported output kind {mode_raw!r} (to-js | wasi)")

    kernel_raw = get("INPUT_KERNEL", "mainline")
    kernel_version_raw = get("INPUT_KERNEL_VERSION")
    kernel_family = kernel_raw
    kernel_version = kernel_version_raw
    if ":" in kernel_raw:  # legacy "mainline:v6.12.50" dispatch (API callers)
        if kernel_version_raw:
            fail("input kernel already contains a version; leave kernel_version empty")
        kernel_family, kernel_version = kernel_raw.split(":", 1)
    if not kernel_family:
        kernel_family = "mainline"

    defaults = {
        "arch": ARCH_ALIASES[arch_raw],
        "mode": MODE_ALIASES[mode_raw],
        "kernel": kernel_family.strip().lower(),
        "kernel_version": kernel_version.strip(),
        "guest_ram": get("INPUT_GUEST_RAM"),
        "cores": get("INPUT_CORES"),
        "qemu_cpu_opts": "",
        "experimental": as_bool(get("INPUT_EXPERIMENTAL"), "input experimental"),
    }
    compose_kernel(defaults["kernel"], defaults["kernel_version"],
                   "input kernel", version_is_explicit=True)  # fail fast on bad family/tag

    # -- source selection ---------------------------------------------------
    source = get("INPUT_SOURCE", "auto").lower()
    if source not in SOURCES:
        fail(f"input source: unsupported source {source!r} ({' | '.join(SOURCES)})")

    images = [part for part in re.split(r"[\s,]+", get("INPUT_IMAGE")) if part]
    tar_url = get("INPUT_IMAGE_TAR_URL")
    rootfs_url = get("INPUT_ROOTFS_TAR_URL")
    matrix_raw = get("INPUT_MATRIX")
    name = get("INPUT_NAME")

    if source == "auto":
        given = []
        if images:
            given.append("registry")
        if tar_url:
            given.append("image-tar")
        if rootfs_url:
            given.append("rootfs-tar")
        if matrix_raw:
            given.append("matrix")
        if not given:
            if name:
                fail("input name: name requires an image source (image / image_tar_url / "
                     "rootfs_tar_url / matrix); set one or clear name")
            source = "demo"
        elif len(given) > 1:
            fail("multiple image sources given (" + ", ".join(given) +
                 "); keep exactly one, or choose source explicitly")
        else:
            source = given[0]

    def reject_others(allowed):
        present = []
        if "registry" not in allowed and images:
            present.append("image")
        if "image-tar" not in allowed and tar_url:
            present.append("image_tar_url")
        if "rootfs-tar" not in allowed and rootfs_url:
            present.append("rootfs_tar_url")
        if "matrix" not in allowed and matrix_raw:
            present.append("matrix")
        if present:
            fail(f"source={source} conflicts with: {', '.join(present)}; "
                 "clear those inputs or use source=auto")

    matrix = []

    if source == "registry":
        reject_others({"registry"})
        if not images:
            fail("source=registry requires the image input")
        if name and len(images) > 1:
            fail("input name: a single name cannot be used for multiple images; "
                 "clear name (auto names: <arch>-<image-slug>) or use matrix mode")
        for index, ref in enumerate(images):
            raw = {"image": ref}
            if name:
                raw["name"] = name
            matrix.append(normalize_entry(raw, f"images[{index}]", defaults))

    elif source == "image-tar":
        reject_others({"image-tar"})
        if not tar_url:
            fail("source=image-tar requires image_tar_url")
        raw = {"image_tar_url": tar_url}
        tar_name = get("INPUT_IMAGE_TAR_NAME")
        if tar_name:
            raw["image_tar_name"] = tar_name
        if name:
            raw["name"] = name
        matrix = [normalize_entry(raw, "image_tar_url", defaults)]

    elif source == "rootfs-tar":
        reject_others({"rootfs-tar"})
        if not rootfs_url:
            fail("source=rootfs-tar requires rootfs_tar_url")
        raw = {"image_rootfs_url": rootfs_url}
        rootfs_cmd = get("INPUT_ROOTFS_CMD")
        if rootfs_cmd:
            raw["image_cmd"] = rootfs_cmd
        if name:
            raw["name"] = name
        matrix = [normalize_entry(raw, "rootfs_tar_url", defaults)]

    elif source == "matrix":
        reject_others({"matrix"})
        if not matrix_raw:
            fail("source=matrix requires the matrix input (JSON array)")
        if name:
            fail("input name is not used in matrix mode; set name per matrix entry instead")
        try:
            data = json.loads(matrix_raw)
        except json.JSONDecodeError as exc:
            fail(f"input matrix: invalid JSON: {exc}")
        if not isinstance(data, list) or not data:
            fail("input matrix: expected a non-empty JSON array of objects")
        matrix = [normalize_entry(item, f"matrix[{index}]", defaults)
                  for index, item in enumerate(data)]

    elif source == "demo":
        reject_others({"demo"})
        if name:
            fail("input name is not used for the demo matrix (3 architectures); "
                 "clear name or use source=registry")
        if defaults["mode"] == "wasi":
            fail("source=demo includes aarch64, which supports to-js only; "
                 "set output_kind=to-js (or use source=matrix for a custom set)")
        matrix = [normalize_entry(item, f"demo[{index}]", defaults)
                  for index, item in enumerate(DEFAULT_MATRIX)]

    # -- warnings for dispatch inputs the chosen source ignores -------------
    if source == "demo" and defaults["arch"] != "amd64":
        warn(f"source=demo pins the guest arches (amd64/riscv64/aarch64); "
             f"input arch={defaults['arch']!r} is ignored")
    if len(matrix) > 256:
        fail(f"{len(matrix)} matrix entries requested; GitHub Actions supports "
             "at most 256 jobs per workflow run")

    dispatch_tar_name = get("INPUT_IMAGE_TAR_NAME", DEFAULT_TAR_NAME)
    if source != "image-tar" and dispatch_tar_name not in ("", DEFAULT_TAR_NAME):
        warn(f"input image_tar_name={dispatch_tar_name!r} is ignored by source={source}")
    dispatch_rootfs_cmd = get("INPUT_ROOTFS_CMD", DEFAULT_ROOTFS_CMD)
    if source != "rootfs-tar" and dispatch_rootfs_cmd not in ("", DEFAULT_ROOTFS_CMD):
        warn(f"input rootfs_cmd={dispatch_rootfs_cmd!r} is ignored by source={source}")

    # -- duplicates, retention ---------------------------------------------
    seen = {}
    for index, entry in enumerate(matrix, 1):
        if entry["name"] in seen:
            fail(f"duplicate build name {entry['name']!r} "
                 f"(entries #{seen[entry['name']]} and #{index}); "
                 "set unique names")
        seen[entry["name"]] = index

    retention = as_count(get("INPUT_RETENTION_DAYS"), "input retention_days",
                         1, 90, default=DEFAULT_RETENTION_DAYS)
    return matrix, retention, source


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def md_cell(value):
    # Table cells are rendered inside `code spans`, so also neutralize
    # backticks (which would otherwise break the span) and newlines.
    return (str(value).replace("|", "\\|").replace("`", "'")
            .replace("\n", " "))


def entry_source_label(entry):
    if entry["image"]:
        return f"`{md_cell(entry['image'])}`"
    if entry["image_tar_url"]:
        return f"docker-save tar `{md_cell(entry['image_tar_url'])}` → `{entry['image_tar_name']}`"
    return f"rootfs tar `{md_cell(entry['image_rootfs_url'])}` (cmd `{entry['image_cmd']}`)"


def kernel_label(kernel):
    return kernel if ":" in kernel else f"{kernel} (default tag)"


def render_summary(matrix, retention, source):
    lines = [
        "## container2wasm build matrix",
        "",
        f"**source:** `{source}` · **entries:** {len(matrix)} · "
        f"**artifact retention:** {retention} day(s)",
        "",
        "| # | name | arch | kernel | output | image source |",
        "|---|---|---|---|---|---|",
    ]
    for index, entry in enumerate(matrix, 1):
        lines.append(
            f"| {index} | `{md_cell(entry['name'])}` | {entry['arch']} | "
            f"`{md_cell(kernel_label(entry['kernel']))}` | {entry['mode']} | "
            f"{entry_source_label(entry)} |"
        )
    tuning = []
    for entry in matrix:
        extras = []
        if entry["guest_ram"]:
            extras.append(f"ram={entry['guest_ram']}MB")
        if entry["cores"]:
            extras.append(f"cores={entry['cores']}")
        if entry["qemu_cpu_opts"]:
            extras.append(f"qemu_cpu_opts={md_cell(entry['qemu_cpu_opts'])}")
        if entry["experimental"]:
            extras.append("experimental")
        if extras:
            tuning.append(f"`{entry['name']}`: " + ", ".join(extras))
    if tuning:
        lines += ["", "**overrides:** " + " · ".join(tuning)]
    lines.append("")
    return "\n".join(lines) + "\n"


def emit(matrix, retention, source, environ=None):
    env = os.environ if environ is None else environ
    matrix_json = json.dumps({"include": matrix}, separators=(",", ":"))

    gh_output = env.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={matrix_json}\n")
            handle.write(f"retention_days={retention}\n")

    log(f"source={source}: {len(matrix)} build(s), retention={retention} day(s)")
    for entry in matrix:
        log(f"  {entry['name']}: arch={entry['arch']} kernel={entry['kernel']} "
            f"mode={entry['mode']} image={entry['image'] or entry['image_tar_url'] or entry['image_rootfs_url']}")

    summary = render_summary(matrix, retention, source)
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary)

    if not gh_output:  # local / debugging run: show the matrix on stdout
        print(matrix_json)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _ienv(**kwargs):
    return {"INPUT_" + key.upper(): value for key, value in kwargs.items()}


def _expect(condition, message):
    if not condition:
        raise AssertionError(message)


def _self_test_cases():
    cases = []

    def case(description, env, ok=True, check=None):
        cases.append((description, env, ok, check))

    def checked(expected_source, expected_count, asserts):
        def check(matrix, retention, source):
            _expect(source == expected_source, f"source {source!r} != {expected_source!r}")
            _expect(len(matrix) == expected_count,
                    f"{len(matrix)} entries != {expected_count}")
            asserts(matrix, retention, source)
        return check

    # 1. empty dispatch -> built-in demo
    def demo_asserts(matrix, retention, source):
        _expect([e["name"] for e in matrix] ==
                ["amd64-alpine", "riscv64-alpine", "aarch64-alpine"],
                "unexpected demo names")
        _expect([e["arch"] for e in matrix] == ["amd64", "riscv64", "aarch64"],
                "unexpected demo arches")
        _expect(all(e["kernel"] == "mainline" for e in matrix), "demo kernel")
        _expect(matrix[2]["experimental"] is True, "aarch64 demo should be experimental")
        _expect(retention == 14, f"retention {retention} != 14")
    case("empty inputs run the demo", {}, True,
         checked("demo", 3, demo_asserts))

    # 1b. push/pull_request: `inputs.*` render as empty strings, not missing
    push_env = {f"INPUT_{key}": "" for key in (
        "SOURCE", "IMAGE", "ARCH", "OUTPUT_KIND", "KERNEL", "KERNEL_VERSION",
        "NAME", "GUEST_RAM", "CORES", "IMAGE_TAR_URL", "IMAGE_TAR_NAME",
        "ROOTFS_TAR_URL", "ROOTFS_CMD", "MATRIX", "RETENTION_DAYS",
        "EXPERIMENTAL")}
    case("empty-string inputs (push/pull_request)", dict(push_env), True,
         checked("demo", 3, demo_asserts))

    # 1c. dispatch with all defaults explicitly supplied (API callers)
    default_dispatch = {
        "INPUT_SOURCE": "auto", "INPUT_IMAGE": "", "INPUT_ARCH": "amd64",
        "INPUT_OUTPUT_KIND": "to-js", "INPUT_KERNEL": "mainline",
        "INPUT_KERNEL_VERSION": "", "INPUT_NAME": "", "INPUT_GUEST_RAM": "",
        "INPUT_CORES": "", "INPUT_IMAGE_TAR_URL": "", "INPUT_IMAGE_TAR_NAME":
        "c2w-user-image:latest", "INPUT_ROOTFS_TAR_URL": "",
        "INPUT_ROOTFS_CMD": "/bin/bash", "INPUT_MATRIX": "",
        "INPUT_RETENTION_DAYS": "14", "INPUT_EXPERIMENTAL": "false",
    }
    case("all dispatch defaults supplied", dict(default_dispatch), True,
         checked("demo", 3, demo_asserts))

    # 2. single registry image
    def single_image(matrix, retention, source):
        e = matrix[0]
        _expect(e["arch"] == "amd64", e["arch"])
        _expect(e["kernel"] == "mainline", e["kernel"])
        _expect(e["mode"] == "to-js", e["mode"])
        _expect(e["name"] == "amd64-alpine-3.20", e["name"])
        _expect(e["image"] == "alpine:3.20", e["image"])
    case("single image", _ienv(image="alpine:3.20"), True,
         checked("registry", 1, single_image))

    # 3. comma separated image list
    def image_list(matrix, retention, source):
        _expect([e["name"] for e in matrix] ==
                ["amd64-alpine-3.20", "amd64-python-3.12-slim"], "names")
    case("comma separated images", _ienv(image="alpine:3.20, python:3.12-slim"), True,
         checked("registry", 2, image_list))

    # 4. newline separated image list
    case("newline separated images",
         _ienv(image="alpine:3.20\nnginx:1.27"), True,
         checked("registry", 2, lambda m, r, s: None))

    # 5. conflicting sources in auto mode
    case("conflicting auto sources",
         _ienv(image="alpine:3.20", image_tar_url="https://example.test/a.tar"),
         False)

    # 6. docker-save tarball
    def tar_asserts(matrix, retention, source):
        e = matrix[0]
        _expect(e["image_tar_url"] == "https://example.test/app.tar", "tar url")
        _expect(e["image_tar_name"] == "c2w-user-image:latest", e["image_tar_name"])
        _expect(e["name"].startswith("amd64-https-example.test"), e["name"])
    case("image tar source", _ienv(source="image-tar",
                                   image_tar_url="https://example.test/app.tar"),
         True, checked("image-tar", 1, tar_asserts))

    # 7. rootfs tarball gets the default cmd
    def rootfs_asserts(matrix, retention, source):
        e = matrix[0]
        _expect(e["image_rootfs_url"] == "https://example.test/rootfs.tar.zst", "url")
        _expect(e["image_cmd"] == "/bin/bash", e["image_cmd"])
    case("rootfs source", _ienv(source="rootfs-tar",
                                rootfs_tar_url="https://example.test/rootfs.tar.zst"),
         True, checked("rootfs-tar", 1, rootfs_asserts))

    # 8. explicit registry without image
    case("registry source without image", _ienv(source="registry"), False)

    # 9. kernel family + version split
    def lts_asserts(matrix, retention, source):
        _expect(matrix[0]["kernel"] == "lts:v6.12.50", matrix[0]["kernel"])
    case("lts kernel with version",
         _ienv(image="alpine:3.20", kernel="lts", kernel_version="6.12.50"),
         True, checked("registry", 1, lts_asserts))

    # 10-11. bad kernel inputs
    case("invalid kernel version",
         _ienv(image="alpine:3.20", kernel="lts", kernel_version="six"), False)
    case("builtin kernel rejects a version",
         _ienv(image="alpine:3.20", kernel="builtin", kernel_version="v6.12.50"), False)

    # 12. legacy "family:tag" dispatch value (API callers)
    def legacy_kernel(matrix, retention, source):
        _expect(matrix[0]["kernel"] == "mainline:v6.12.50", matrix[0]["kernel"])
    case("legacy kernel:tag input",
         _ienv(image="alpine:3.20", kernel="mainline:v6.12.50"), True,
         checked("registry", 1, legacy_kernel))

    # 13. aarch64 has no WASI target
    case("aarch64 + wasi",
         _ienv(image="arm64v8/alpine:3.20", arch="aarch64", output_kind="wasi"),
         False)

    # 14. matrix mode with per-entry overrides
    def matrix_asserts(matrix, retention, source):
        e = matrix[0]
        _expect(e["arch"] == "riscv64", e["arch"])
        _expect(e["kernel"] == "lts:v6.12.50", e["kernel"])
        _expect(e["mode"] == "wasi", e["mode"])
        _expect(e["name"] == "rv-alpine", e["name"])
    matrix_json = json.dumps([{
        "arch": "riscv64", "kernel": "lts:v6.12.50", "mode": "wasi",
        "name": "rv-alpine", "image": "riscv64/alpine:3.20"}])
    case("matrix entry", _ienv(source="matrix", matrix=matrix_json), True,
         checked("matrix", 1, matrix_asserts))

    # 15. matrix aliases (output_kind/rootfs_tar_url/rootfs_cmd/kernel_family)
    def alias_asserts(matrix, retention, source):
        e = matrix[0]
        _expect(e["arch"] == "aarch64", e["arch"])
        _expect(e["kernel"] == "builtin", e["kernel"])
        _expect(e["mode"] == "to-js", e["mode"])
        _expect(e["image_cmd"] == "/bin/sh", e["image_cmd"])
    case("matrix aliases", _ienv(source="matrix", matrix=json.dumps([{
        "arch": "arm64",
        "kernel_family": "builtin",
        "output_kind": "to-js",
        "rootfs_tar_url": "https://example.test/rootfs.tar.zst",
        "rootfs_cmd": "/bin/sh",
    }])), True, checked("matrix", 1, alias_asserts))

    # 16-17. malformed matrix input
    case("invalid matrix JSON", _ienv(source="matrix", matrix="{oops"), False)
    case("empty matrix array", _ienv(source="matrix", matrix="[]"), False)

    # 18. duplicate names
    case("duplicate names", _ienv(source="matrix", matrix=json.dumps([
        {"image": "alpine:3.20", "name": "same"},
        {"image": "python:3.12-slim", "name": "same"},
    ])), False)

    # 19. invalid custom name
    case("invalid name", _ienv(image="alpine:3.20", name="bad name!"), False)

    # 20-22. retention bounds
    case("retention 0", _ienv(image="alpine:3.20", retention_days="0"), False)
    case("retention 91", _ienv(image="alpine:3.20", retention_days="91"), False)

    def retention_asserts(matrix, retention, source):
        _expect(retention == 30, retention)
    case("retention 30", _ienv(image="alpine:3.20", retention_days="30"), True,
         checked("registry", 1, retention_asserts))

    # 23. runtime tuning
    def tuning_asserts(matrix, retention, source):
        _expect(matrix[0]["guest_ram"] == "1024", matrix[0]["guest_ram"])
        _expect(matrix[0]["cores"] == "2", matrix[0]["cores"])
    case("guest ram and cores",
         _ienv(image="alpine:3.20", guest_ram="1024", cores="2"), True,
         checked("registry", 1, tuning_asserts))
    case("invalid guest ram", _ienv(image="alpine:3.20", guest_ram="1GB"), False)
    case("invalid cores", _ienv(image="alpine:3.20", cores="0"), False)

    # 24. URL scheme validation
    case("ftp rootfs url", _ienv(source="rootfs-tar",
                                 rootfs_tar_url="ftp://example.test/rootfs.tar"), False)

    # 25. demo cannot be mixed with image inputs
    case("demo with image", _ienv(source="demo", image="alpine:3.20"), False)

    # 26. matrix entry with two image sources
    case("matrix entry with two sources", _ienv(source="matrix", matrix=json.dumps([
        {"image": "alpine:3.20", "image_tar_url": "https://example.test/a.tar"}])), False)

    # 27. matrix boolean strings
    def experimental_asserts(matrix, retention, source):
        _expect(matrix[0]["experimental"] is True, matrix[0]["experimental"])
    case("matrix experimental string", _ienv(source="matrix", matrix=json.dumps([
        {"image": "alpine:3.20", "experimental": "true"}])), True,
         checked("matrix", 1, experimental_asserts))

    # 28. dispatch-level experimental applies to registry builds
    def dispatch_experimental(matrix, retention, source):
        _expect(matrix[0]["experimental"] is True, matrix[0]["experimental"])
    case("dispatch experimental", _ienv(image="alpine:3.20", experimental="true"), True,
         checked("registry", 1, dispatch_experimental))

    # 29. a name for a multi-image batch is rejected
    case("name with image list",
         _ienv(image="alpine:3.20, python:3.12-slim", name="batch"), False)

    # 30. unknown matrix entry field (typo protection)
    case("unknown matrix field", _ienv(source="matrix", matrix=json.dumps([
        {"images": "alpine:3.20"}])), False)

    # 30b. per-source-only fields are rejected on the wrong matrix source
    case("matrix image_tar_name on registry image", _ienv(source="matrix", matrix=json.dumps([
        {"image": "alpine:3.20", "image_tar_name": "app:1"}])), False)
    case("matrix image_cmd on registry image", _ienv(source="matrix", matrix=json.dumps([
        {"image": "alpine:3.20", "image_cmd": "/bin/sh"}])), False)

    # 31. matrix kernel_version + inherited filter
    def matrix_kernel_version(matrix, retention, source):
        _expect(matrix[0]["kernel"] == "mainline:v6.12.50", matrix[0]["kernel"])
        _expect(matrix[0]["guest_ram"] == "1024", matrix[0]["guest_ram"])

    case("matrix inherits dispatch kernel/ram",
         _ienv(source="matrix", kernel_version="v6.12.50", guest_ram="1024",
               matrix=json.dumps([{"image": "alpine:3.20"}])), True,
         checked("matrix", 1, matrix_kernel_version))

    # 32. demo picks up kernel_version across all arches
    def demo_kernel(matrix, retention, source):
        _expect(all(e["kernel"] == "mainline:v6.12.50" for e in matrix),
                [e["kernel"] for e in matrix])
    case("demo with kernel version",
         _ienv(kernel_version="v6.12.50"), True,
         checked("demo", 3, demo_kernel))

    # 33. matrix with wasi mode via alias defaults through dispatch input
    case("matrix aarch64 + wasi from dispatch", _ienv(
        source="matrix", arch="aarch64", output_kind="wasi",
        matrix=json.dumps([{"image": "arm64v8/alpine:3.20"}])), False)

    # 34. unsupported source value
    case("unsupported source", _ienv(source="gitlab"), False)

    # 35. rootfs command with shell metacharacters is rejected
    case("unsafe rootfs cmd", _ienv(source="rootfs-tar",
                                    rootfs_tar_url="https://example.test/r.tar",
                                    rootfs_cmd='bash -c "id"'), False)

    # 36. matrix name auto-generated per entry when omitted
    def matrix_names(matrix, retention, source):
        _expect(matrix[0]["name"] == "amd64-ghcr.io-owner-app-latest", matrix[0]["name"])
    case("matrix auto name", _ienv(source="matrix", matrix=json.dumps([
        {"image": "ghcr.io/owner/app:latest"}])), True,
         checked("matrix", 1, matrix_names))

    # 37. matrix entries: explicit "" behaves like an omitted field
    def matrix_empty_inherits(matrix, retention, source):
        entry = matrix[0]
        _expect(entry["arch"] == "riscv64", entry["arch"])
        _expect(entry["mode"] == "wasi", entry["mode"])
        _expect(entry["guest_ram"] == "1000", entry["guest_ram"])
        _expect(entry["experimental"] is True, entry["experimental"])
    case("matrix empty strings inherit dispatch defaults", _ienv(
        arch="riscv64", output_kind="wasi", guest_ram="1000", experimental="true",
        source="matrix", matrix=json.dumps([{
            "image": "riscv64/alpine:3.20", "arch": "", "mode": "",
            "guest_ram": "", "experimental": ""}])), True,
         checked("matrix", 1, matrix_empty_inherits))

    # 37b. alias fields work when the canonical field is only inherited/defaulted
    def alias_only(matrix, retention, source):
        _expect(matrix[0]["kernel"] == "lts", matrix[0]["kernel"])
        _expect(matrix[0]["mode"] == "wasi", matrix[0]["mode"])
    case("matrix kernel_family/output_kind without conflict", _ienv(
        source="matrix", matrix=json.dumps([{
            "image": "alpine:3.20", "kernel_family": "lts",
            "output_kind": "wasi"}])), True,
         checked("matrix", 1, alias_only))
    case("matrix kernel + kernel_family conflict", _ienv(
        source="matrix", matrix=json.dumps([{
            "image": "alpine:3.20", "kernel": "lts",
            "kernel_family": "mainline"}])), False)
    case("matrix mode + output_kind conflict", _ienv(
        source="matrix", matrix=json.dumps([{
            "image": "alpine:3.20", "mode": "to-js",
            "output_kind": "wasi"}])), False)

    # 38. inline kernel tag conflicts with an explicit kernel_version
    case("matrix kernel tag conflicts with kernel_version", _ienv(
        source="matrix", matrix=json.dumps([{
            "image": "alpine:3.20", "kernel": "lts:v6.12.50",
            "kernel_version": "v6.13.0"}])), False)

    # 39. ...but an inline tag beats an inherited dispatch kernel_version
    def matrix_inline_tag(matrix, retention, source):
        _expect(matrix[0]["kernel"] == "lts:v6.12.50", matrix[0]["kernel"])
    case("matrix inline tag beats inherited version", _ienv(
        kernel_version="v6.13.0", source="matrix", matrix=json.dumps([
            {"image": "alpine:3.20", "kernel": "lts:v6.12.50"}])), True,
         checked("matrix", 1, matrix_inline_tag))

    # 40. docker image-reference validation
    for ref in ("alpine:3.20", "ghcr.io/owner/app:latest", "localhost:5000/app:1",
                "riscv64/alpine@sha256:" + "a" * 64, "python:3.12-slim",
                "c2w-user-image:latest"):
        case(f"valid image ref {ref[:28]}", _ienv(image=ref), True,
             checked("registry", 1, lambda m, r, s: None))
    for ref in ("Alpine:3.20", "app:bad tag", "app@sha256:short",
                "ghcr.io/-bad/app:1", "app:UPPER?"):
        case(f"invalid image ref {ref[:28]}", _ienv(
            source="matrix", matrix=json.dumps([{"image": ref}])), False)

    # 40b. registry hostname grammar
    for ref in ("registry.example.com:8443/ns/app:v1.2.3",
                "docker.io/library/alpine:3.20", "[::1]:5000/app"):
        case(f"valid registry ref {ref[:30]}", _ienv(image=ref), True,
             checked("registry", 1, lambda m, r, s: None))
    for ref in ("a..b/app", "a-.b/app", "registry..example.com/app:1",
                "ghcr.io/-bad/app:1", "ghcr.io/app-/x:1"):
        case(f"invalid registry ref {ref[:30]}", _ienv(
            source="matrix", matrix=json.dumps([{"image": ref}])), False)

    # 40c. digest grammar accepts sha256/sha512, any hex case
    for ref in ("alpine@sha256:" + "A" * 64,
                "alpine@sha512:" + "a" * 128,
                "alpine:3.20@sha256:" + "d" * 64):
        case(f"valid digest ref {ref[:26]}", _ienv(image=ref), True,
             checked("registry", 1, lambda m, r, s: None))
    for ref in ("alpine@sha256:" + "g" * 64,
                "alpine@SHA256:" + "a" * 64,
                "alpine@:" + "a" * 64,
                "alpine@sha256:0123456789abcdef"):
        case(f"invalid digest ref {ref[:26]}", _ienv(
            source="matrix", matrix=json.dumps([{"image": ref}])), False)

    # 40d. summary overrides must not leak raw backticks
    def summary_escape(matrix, retention, source):
        rendered = render_summary(matrix, retention, source)
        _expect("rv`64" not in rendered, "qemu_cpu_opts backtick leaked into summary")
    case("summary escapes qemu_cpu_opts", _ienv(
        source="matrix", matrix=json.dumps([{
            "arch": "riscv64", "image": "riscv64/alpine:3.20",
            "qemu_cpu_opts": "rv`64,zbb=false"}])), True,
         checked("matrix", 1, summary_escape))

    # 41. github caps a run at 256 jobs
    case("more than 256 matrix entries", _ienv(source="matrix", matrix=json.dumps(
        [{"image": "alpine:3.20", "name": f"e{index}"} for index in range(257)])),
         False)

    return cases


def run_self_test():
    cases = _self_test_cases()
    failures = []
    for description, env, ok, check in cases:
        try:
            matrix, retention, source = resolve(env)
        except MatrixError as exc:
            if ok:
                failures.append(f"{description}: unexpected rejection: {exc}")
            continue
        if not ok:
            failures.append(f"{description}: expected a rejection, got {len(matrix)} entry(ies)")
            continue
        if check is not None:
            try:
                check(matrix, retention, source)
            except AssertionError as exc:
                failures.append(f"{description}: {exc}")
    passed = len(cases) - len(failures)
    print(f"[self-test] {passed}/{len(cases)} cases passed", file=sys.stderr)
    for message in failures:
        print(f"[self-test] FAIL: {message}", file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv):
    if "--self-test" in argv[1:]:
        return run_self_test()
    if any(arg in ("-h", "--help") for arg in argv[1:]):
        print(__doc__)
        return 0
    try:
        matrix, retention, source = resolve()
    except MatrixError as exc:
        message = str(exc).replace("\n", " ")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Invalid workflow inputs::{message}", file=sys.stderr)
        print(f"[matrix] ERROR: {message}", file=sys.stderr)
        return 1
    emit(matrix, retention, source)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
