"""Tests for the static GPU price reference generator and generated module (Plan B, B4c)."""

import json
import re
from pathlib import Path

import pytest

from scripts.gen_gpu_price_reference import VENDOR_CODES, generate, main
from shared.fleet.enums import GpuVendor
from shared.fleet.vllm_ray import gpu_price_reference as ref

REF_DIR = Path("docs/reference")
SLUG = re.compile(r"^(nvidia|amd|intel)-[a-z0-9-]+$")
NVIDIA_JSON = "gpu-price-reference-nvidia.json"


def copy_inputs(dst: Path, skip: str | None = None) -> None:
    """Copy the three real JSON inputs into ``dst``, optionally skipping one file name."""
    for f in REF_DIR.glob("gpu-price-reference-*.json"):
        if f.name != skip:
            (dst / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")


def mutate_nvidia(dst: Path, fn) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
    """Copy inputs then apply ``fn(data)`` to the parsed NVIDIA file and write it back."""
    copy_inputs(dst)
    path = dst / NVIDIA_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_as_of_is_iso_date() -> None:
    """AS_OF is an ISO date."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref.AS_OF)


def test_slugs_match_convention_and_key_equals_class() -> None:
    """Every key is a valid slug equal to its record's gpu_class."""
    assert ref.GPU_PRICE_REFERENCE
    for key, value in ref.GPU_PRICE_REFERENCE.items():
        assert SLUG.match(key)
        assert key == value.gpu_class


def test_row_counts_cover_all_three_vendors() -> None:
    """85 NVIDIA + 53 AMD + 19 Intel rows are all present."""
    by_vendor: dict[int, int] = {}
    for value in ref.GPU_PRICE_REFERENCE.values():
        by_vendor[value.vendor] = by_vendor.get(value.vendor, 0) + 1
    assert by_vendor == {1: 85, 2: 53, 3: 19}


def test_nvidia_rtx4090_has_24gb() -> None:
    """The RTX 4090 reference is the 24 GB card."""
    assert ref.GPU_PRICE_REFERENCE["nvidia-rtx4090"].vram_gb == 24


def test_references_are_frozen() -> None:
    """GpuRef is immutable."""
    with pytest.raises(AttributeError):
        ref.GPU_PRICE_REFERENCE["nvidia-rtx4090"].avg_street_usd = 1  # type: ignore[misc]


def test_duplicate_slug_rejected(tmp_path: Path) -> None:
    """A duplicate slug across rows fails generation."""
    mutate_nvidia(tmp_path, lambda d: d["gpus"].append(dict(d["gpus"][0])))
    with pytest.raises(ValueError, match="duplicate"):
        generate(tmp_path)


def test_vendor_codes_match_gpu_vendor() -> None:
    """Generator vendor codes equal the GpuVendor enum."""
    assert VENDOR_CODES == {"nvidia": 1, "amd": 2, "intel": 3}
    assert VENDOR_CODES == {v.name.lower(): int(v) for v in GpuVendor}


def test_missing_file_fails_loudly(tmp_path: Path) -> None:
    """A missing input file names the file in the error."""
    copy_inputs(tmp_path, skip="gpu-price-reference-amd.json")
    with pytest.raises(ValueError, match="gpu-price-reference-amd.json"):
        generate(tmp_path)


def test_unparsable_file_fails_loudly(tmp_path: Path) -> None:
    """Invalid JSON fails with the file name."""
    copy_inputs(tmp_path)
    (tmp_path / NVIDIA_JSON).write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match=NVIDIA_JSON):
        generate(tmp_path)


def test_as_of_mismatch_rejected(tmp_path: Path) -> None:
    """Files disagreeing on as_of fail generation."""
    mutate_nvidia(tmp_path, lambda d: d.update(as_of="2020-01-01"))
    with pytest.raises(ValueError, match="as_of"):
        generate(tmp_path)


def test_row_vendor_mismatch_rejected(tmp_path: Path) -> None:
    """A row whose vendor differs from its file's vendor fails."""
    mutate_nvidia(tmp_path, lambda d: d["gpus"][0].update(vendor="amd"))
    with pytest.raises(ValueError, match="vendor"):
        generate(tmp_path)


@pytest.mark.parametrize("slug", ["NVIDIA-RTX", "amd-rtx4090", "nvidia_rtx", "nvidia-"])
def test_bad_slug_rejected(tmp_path: Path, slug: str) -> None:
    """Slugs not matching the convention or the file vendor prefix fail."""
    mutate_nvidia(tmp_path, lambda d: d["gpus"][0].update({"class": slug}))
    with pytest.raises(ValueError, match="slug"):
        generate(tmp_path)


@pytest.mark.parametrize("field", ["vram_gb", "tdp_watts", "avg_street_usd"])
@pytest.mark.parametrize("bad", [0, -5, "7", 1.5, True])
def test_non_positive_int_fields_rejected(tmp_path: Path, field: str, bad: object) -> None:
    """Numeric fields must be positive ints (bool and float are refused)."""
    mutate_nvidia(tmp_path, lambda d: d["gpus"][0].update({field: bad}))
    with pytest.raises(ValueError, match=field):
        generate(tmp_path)


@pytest.mark.parametrize(("field", "bad"), [("sources", []), ("availability", "")])
def test_empty_sources_or_availability_rejected(tmp_path: Path, field: str, bad: object) -> None:
    """Rows need sources and an availability string."""
    mutate_nvidia(tmp_path, lambda d: d["gpus"][0].update({field: bad}))
    with pytest.raises(ValueError, match=field):
        generate(tmp_path)


def test_generation_is_deterministic_and_sorted() -> None:
    """Two generations are byte-identical and keys are sorted."""
    first = generate(REF_DIR)
    assert first == generate(REF_DIR)
    keys = list(ref.GPU_PRICE_REFERENCE)
    assert keys == sorted(keys)
    assert first.startswith('"""generated by scripts/gen_gpu_price_reference.py')


def test_main_writes_module_and_reports_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default mode writes the module and prints the GPU count."""
    out = tmp_path / "gpu_price_reference.py"
    assert main(["--input-dir", str(REF_DIR), "--output", str(out)]) == 0
    assert capsys.readouterr().out.strip() == "wrote 157 GPUs"
    assert out.read_text(encoding="utf-8") == generate(REF_DIR)


def test_check_mode_passes_when_current(tmp_path: Path) -> None:
    """--check exits 0 when the module matches a fresh generation."""
    out = tmp_path / "gpu_price_reference.py"
    out.write_text(generate(REF_DIR), encoding="utf-8")
    assert main(["--check", "--input-dir", str(REF_DIR), "--output", str(out)]) == 0


def test_check_mode_fails_when_module_is_stale(tmp_path: Path) -> None:
    """--check exits 1 against a stale module."""
    stale = tmp_path / "gpu_price_reference.py"
    stale.write_text("# stale\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--check", "--input-dir", str(REF_DIR), "--output", str(stale)])
    assert exc.value.code == 1


def test_check_mode_fails_when_module_missing(tmp_path: Path) -> None:
    """--check exits 1 when the output file does not exist."""
    with pytest.raises(SystemExit) as exc:
        main(["--check", "--input-dir", str(REF_DIR), "--output", str(tmp_path / "nope.py")])
    assert exc.value.code == 1


def test_main_exits_one_on_invalid_input(tmp_path: Path) -> None:
    """Invalid inputs exit non-zero, never fall back."""
    with pytest.raises(SystemExit) as exc:
        main(["--input-dir", str(tmp_path), "--output", str(tmp_path / "o.py")])
    assert exc.value.code == 1


def test_committed_module_matches_fresh_generation() -> None:
    """The committed module equals a fresh generation (run make gen-gpu-price-reference)."""
    committed = Path("shared/fleet/vllm_ray/gpu_price_reference.py").read_text(encoding="utf-8")
    assert generate(REF_DIR) == committed
