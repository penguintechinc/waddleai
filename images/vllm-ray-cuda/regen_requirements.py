#!/usr/bin/env python3
"""Regenerate requirements.txt for the vllm-ray-cuda image.

The image layers a hash-pinned upgrade set (vLLM + CVE-floored libraries) over the digest-pinned
rayproject/ray-llm base. This script freezes the base image, resolves requirements.in with uv
against that freeze (so nothing else drifts), and keeps only the packages whose pinned version
differs from the base; the torch/CUDA stack is always left to the base image.
Usage: regen_requirements.py <base-image-ref>
"""

import re
import shutil
import subprocess  # nosec B404 - fixed-argv docker/uv calls only, no shell
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCKER = shutil.which("docker") or "docker"
UV = shutil.which("uv") or "uv"
# Owned by the base image (PyTorch CUDA wheels carry a +cu130 local version PyPI cannot match).
# The CUTLASS DSL and cuDNN frontend are not torch-coupled and vLLM 0.32.0 needs newer ones.
BASE_OWNED = re.compile(
    r"^(torch|torchaudio|torchvision|torchcodec|triton|nvidia-(?!cutlass-dsl|cudnn-frontend).*)$"
)
COMPILE = [
    UV,
    "pip",
    "compile",
    "requirements.in",
    "--python-version",
    "3.12",
    "--python-platform",
    "x86_64-manylinux_2_28",
    "--generate-hashes",
    "--emit-index-url",
    "--index-strategy",
    "unsafe-best-match",
    "--allow-unsafe",
]


def norm(name: str) -> str:
    """Normalise a distribution name for comparison (PEP 503 style)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def freeze_base(image: str) -> dict:
    """Return {normalised name: version without local part} for the base image's pip freeze."""
    out = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, image ref from the Makefile
        [DOCKER, "run", "--rm", "--entrypoint", "pip", image, "freeze"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    base = {}
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)$", line.strip())
        if m:
            base[norm(m.group(1))] = m.group(2).split("+")[0]
    if not base:
        sys.exit("FAIL: pip freeze of the base image returned zero packages")
    return base


def resolve(base: dict, tmp: Path) -> str:
    """Resolve requirements.in against the base freeze, releasing conflicting pins."""
    declared = {
        norm(re.split(r"[<>=!~\[ ;]", ln.strip(), maxsplit=1)[0])
        for ln in (HERE / "requirements.in").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    }
    pins = {
        n: v
        for n, v in base.items()
        if n not in declared and n != "ray" and not BASE_OWNED.match(n)
    }
    for _ in range(60):
        cfile = tmp / "constraints.txt"
        cfile.write_text("".join(f"{n}=={v}\n" for n, v in sorted(pins.items())))
        r = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv
            COMPILE + ["-c", str(cfile), "-o", str(tmp / "out.txt")],
            cwd=HERE,
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return (tmp / "out.txt").read_text()
        blamed = [
            n
            for n in (
                norm(t) for t in re.findall(r"([A-Za-z0-9_.\-]+)(?:\{[^}]*\})?==\d", r.stderr)
            )
            if n in pins
        ]
        if not blamed:
            sys.exit("FAIL: unresolvable and no pinned package to release:\n" + r.stderr)
        print("releasing base pin:", blamed[0])
        del pins[blamed[0]]
    sys.exit("FAIL: too many conflicting pins")


def delta(text: str, base: dict) -> str:
    """Drop requirement blocks that equal the base image's version or are base-owned."""
    out, block, kept, total = [], [], 0, 0

    def flush():
        nonlocal block, kept, total
        if not block:
            return
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)", block[0])
        if m:
            total += 1
            n, v = norm(m.group(1)), m.group(2)
            if BASE_OWNED.match(n) or base.get(n) == v:
                block = []
                return
            kept += 1
        out.extend(block)
        block = []

    for line in text.splitlines():
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*==", line):
            flush()
        if block or re.match(r"^[A-Za-z0-9]", line):
            block.append(line)
        else:
            out.append(line)
    flush()
    if kept == 0:
        sys.exit("FAIL: zero upgrade packages kept")
    print(f"resolved {total} packages, kept {kept} that differ from the base")
    return "\n".join(out) + "\n"


def main() -> None:
    """Entry point: freeze base, resolve, filter, write requirements.txt."""
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    import tempfile

    base = freeze_base(sys.argv[1])
    with tempfile.TemporaryDirectory() as t:
        text = resolve(base, Path(t))
    header = (
        "# Regenerate: python3 regen_requirements.py <base image ref> (see Dockerfile header).\n"
        "# Only packages that differ from the rayproject/ray-llm base are listed "
        "(torch/CUDA stay with the base).\n"
    )
    (HERE / "requirements.txt").write_text(header + delta(text, base))


if __name__ == "__main__":
    main()
