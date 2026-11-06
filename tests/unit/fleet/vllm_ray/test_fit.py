"""Table-driven tests for the vLLM-on-Ray fit calculator (Plan B, task B4)."""

from dataclasses import dataclass

import pytest

from shared.fleet.enums import GpuVendor, InventoryState, LayoutStrategy, Quantization
from shared.fleet.vllm_ray.fit import (
    QUANT_MATRIX,
    Pool,
    build_pools,
    class_label,
    estimate_weight_bytes,
    fit_check,
    quant_allowed,
)

NV, AMD, INTEL = int(GpuVendor.NVIDIA), int(GpuVendor.AMD), int(GpuVendor.INTEL)
NONE, AWQ, GPTQ, FP8 = (
    int(q) for q in (Quantization.NONE, Quantization.AWQ, Quantization.GPTQ, Quantization.FP8)
)
EMPTY_REFUSAL = "No schedulable GPU pool. Check that GPUs are classified (see inventory)."


def pool(vendor: int, cls: str, *nodes: tuple[str, int, int]) -> Pool:
    """Build a Pool from (node_uid, gpu_count, free_mb_per_gpu) tuples."""
    return Pool(vendor=vendor, gpu_class=cls, nodes=list(nodes))


def n4090(uid: str, count: int) -> tuple[str, int, int]:
    """An empty RTX 4090 node."""
    return (uid, count, 24564)


def test_a_12b_two_4090() -> None:
    """Check: a 12b two 4090."""
    res = fit_check(12, NONE, "bfloat16", 4096, [pool(NV, "nvidia-rtx4090", n4090("a", 2))], False)
    assert res.fits and len(res.layouts) == 1
    lay = res.layouts[0]
    assert (lay.tensor_parallel, lay.pipeline_parallel, lay.replicas, lay.nodes_used) == (
        2,
        1,
        1,
        1,
    )
    assert lay.recommended and lay.vram_needed_mb == 30540 and lay.vram_available_mb == 49128
    assert lay.explanation == "Fits on 1 node using 2× RTX 4090 (30 GB of 48 GB). Recommended."


def test_b_12b_one_4090_refused_with_awq_hint() -> None:
    """Check: b 12b one 4090 refused with awq hint."""
    res = fit_check(12, NONE, "bfloat16", 4096, [pool(NV, "nvidia-rtx4090", n4090("a", 1))], False)
    assert not res.fits and res.layouts == []
    assert res.refusal == "Does not fit in any pool. Options: use 4-bit quantization (needs 8 GB)."


def test_c_70b_awq_four_4090() -> None:
    """Check: c 70b awq four 4090."""
    res = fit_check(70, AWQ, "float16", 4096, [pool(NV, "nvidia-rtx4090", n4090("a", 4))], False)
    assert res.fits and len(res.layouts) == 2
    first, second = res.layouts
    assert (first.tensor_parallel, first.replicas, first.nodes_used, first.recommended) == (
        2,
        2,
        1,
        True,
    )
    assert (first.vram_needed_mb, first.vram_available_mb) == (94268, 98256)
    assert (
        first.explanation
        == "Fits on 1 node using 2× RTX 4090 per replica, 2 replicas (92 GB of 96 GB). Recommended."
    )
    assert (second.tensor_parallel, second.replicas, second.recommended, second.vram_needed_mb) == (
        4,
        1,
        False,
        50208,
    )
    assert second.explanation == "Fits on 1 node using 4× RTX 4090 (49 GB of 96 GB)."


def test_d_120b_bf16_two_nodes_refused() -> None:
    """Check: d 120b bf16 two nodes refused."""
    pools = [pool(NV, "nvidia-rtx4090", n4090("a", 4), n4090("b", 4))]
    res = fit_check(120, NONE, "bfloat16", 4096, pools, False)
    assert not res.fits
    assert res.refusal == "Does not fit in any pool. Options: use 4-bit quantization (needs 74 GB)."


def test_e_120b_awq_two_nodes_two_gpus() -> None:
    """Check: e 120b awq two nodes two gpus."""
    pools = [pool(NV, "nvidia-rtx4090", n4090("a", 2), n4090("b", 2))]
    res = fit_check(120, AWQ, "float16", 4096, pools, False)
    assert res.fits and len(res.layouts) == 1
    lay = res.layouts[0]
    assert (lay.pipeline_parallel, lay.tensor_parallel, lay.replicas, lay.nodes_used) == (
        2,
        2,
        1,
        2,
    )
    assert lay.strategy == LayoutStrategy.STRICT_SPREAD and lay.recommended
    assert (lay.vram_needed_mb, lay.vram_available_mb) == (81676, 98256)
    assert (
        lay.explanation
        == "Fits across 2 nodes using 2× RTX 4090 per stage (80 GB of 96 GB). Recommended."
    )


def test_f_120b_awq_three_nodes() -> None:
    """Check: f 120b awq three nodes."""
    pools = [pool(NV, "nvidia-rtx4090", n4090("a", 4), n4090("b", 4), n4090("c", 4))]
    res = fit_check(120, AWQ, "float16", 4096, pools, False)
    assert res.fits
    rec = res.layouts[0]
    assert rec.recommended and (rec.tensor_parallel, rec.pipeline_parallel, rec.nodes_used) == (
        4,
        1,
        1,
    )
    assert (
        rec.vram_needed_mb == 20419 * 4
    )  # 20419 MB per GPU x 4 GPUs (plan quotes the per-GPU figure)
    assert any(
        (x.pipeline_parallel, x.tensor_parallel, x.replicas, x.nodes_used) == (3, 2, 1, 3)
        for x in res.layouts
    )
    assert any(x.pipeline_parallel == 2 for x in res.layouts)
    two = [i for i, x in enumerate(res.layouts) if x.nodes_used == 2]
    three = [i for i, x in enumerate(res.layouts) if x.nodes_used == 3]
    assert two and three and max(two) < min(three)
    assert sum(1 for x in res.layouts if x.nodes_used == 1) == 1  # per-node duplicates collapsed


def test_g_mixed_classes_off() -> None:
    """Check: g mixed classes off."""
    pools = [
        pool(NV, "nvidia-rtx4090", n4090("a", 2)),
        pool(NV, "nvidia-rtx4070", ("b", 2, 12282)),
    ]
    res = fit_check(120, AWQ, "float16", 4096, pools, False)
    assert not res.fits
    assert (
        res.refusal == "Does not fit in any pool. Options: add GPUs with at least 39 GB VRAM each."
    )


def test_h_mixed_classes_on() -> None:
    """Check: h mixed classes on."""
    pools = [
        pool(NV, "nvidia-rtx4090", n4090("a", 4)),
        pool(NV, "nvidia-rtx4070", ("b", 4, 12282)),
    ]
    res = fit_check(120, AWQ, "float16", 4096, pools, True)
    assert res.fits
    rec = res.layouts[0]
    assert rec.recommended and (rec.tensor_parallel, rec.pipeline_parallel) == (4, 1)
    assert (
        rec.vram_needed_mb == 20419 * 4
    )  # 20419 MB per GPU x 4 GPUs (plan quotes the per-GPU figure)
    mixed = [x for x in res.layouts if x.pipeline_parallel == 2]
    assert len(mixed) == 1
    assert (mixed[0].tensor_parallel, mixed[0].replicas, mixed[0].nodes_used) == (4, 1, 2)
    assert (mixed[0].vram_needed_mb, mixed[0].vram_available_mb) == (87824, 147384)
    assert (
        mixed[0].explanation
        == "Fits across 2 nodes, mixing RTX 4090 and RTX 4070 stages (86 GB of 144 GB)."
    )
    assert not any(
        x.gpu_class == "nvidia-rtx4070" and x.pipeline_parallel == 1 for x in res.layouts
    )


def test_i_intel_single_gpu_replicas() -> None:
    """Check: i intel single gpu replicas."""
    res = fit_check(
        12, AWQ, "float16", 4096, [pool(INTEL, "intel-max1100", ("a", 2, 16384))], False
    )
    # AWQ is not allowed on Intel (matrix = NONE only) -> quantization gate
    assert not res.fits and "AWQ is not supported on MAX1100" in (res.refusal or "")


def test_i_intel_bf16_single_gpu_replicas() -> None:
    """Check: i intel bf16 single gpu replicas."""
    # Plan row I used AWQ, contradicting spec 4.4 (Intel = bf16/fp16 only); 4B fp16 complies.
    res = fit_check(
        4, NONE, "float16", 4096, [pool(INTEL, "intel-max1100", ("a", 2, 16384))], False
    )
    assert res.fits and len(res.layouts) == 1
    lay = res.layouts[0]
    assert (lay.tensor_parallel, lay.pipeline_parallel, lay.replicas, lay.nodes_used) == (
        1,
        1,
        2,
        1,
    )
    assert lay.recommended and lay.vram_needed_mb == 21384 and lay.vram_available_mb == 32768


def test_j_fp8_on_pre_ada_refused() -> None:
    """Check: j fp8 on pre ada refused."""
    res = fit_check(12, FP8, "bfloat16", 4096, [pool(NV, "nvidia-rtx3090", n4090("a", 2))], False)
    assert not res.fits and res.layouts == []
    assert res.refusal == "FP8 is not supported on RTX 3090. Choose AWQ, GPTQ, or bf16/fp16."


def test_k_fp8_on_ada() -> None:
    """Check: k fp8 on ada."""
    res = fit_check(12, FP8, "bfloat16", 4096, [pool(NV, "nvidia-rtx4090", n4090("a", 2))], False)
    assert res.fits and len(res.layouts) == 2
    a, b = res.layouts
    assert (a.tensor_parallel, a.replicas, a.nodes_used, a.recommended, a.vram_needed_mb) == (
        1,
        2,
        1,
        True,
        30540,
    )
    assert (b.tensor_parallel, b.replicas, b.nodes_used, b.recommended, b.vram_needed_mb) == (
        2,
        1,
        1,
        False,
        16806,
    )


@dataclass(slots=True)
class _Rec:
    node_uid: str
    gpu_index: int
    vendor: int
    gpu_class: str | None
    vram_total_mb: int
    vram_used_mb: int
    schedulable: bool
    state: int


def test_l_unclassified_excluded() -> None:
    """Check: l unclassified excluded."""
    rec = _Rec("u", 0, NV, None, 24564, 0, False, int(InventoryState.UNCLASSIFIED))
    assert build_pools([rec]) == []
    res = fit_check(12, NONE, "bfloat16", 4096, [], False)
    assert not res.fits and res.refusal == EMPTY_REFUSAL


def test_m_mixed_amd_excluded() -> None:
    """Check: m mixed amd excluded."""
    recs = [
        _Rec("u", 0, AMD, "amd-mi300x", 196608, 0, False, int(InventoryState.MIXED_UNSUPPORTED)),
        _Rec("u", 1, AMD, "amd-mi250x", 131072, 0, False, int(InventoryState.MIXED_UNSUPPORTED)),
    ]
    assert build_pools(recs) == []
    assert fit_check(12, NONE, "bfloat16", 4096, build_pools(recs), False).refusal == EMPTY_REFUSAL


def test_build_pools_groups_by_class_with_min_free() -> None:
    """Check: build pools groups by class with min free."""
    warm = int(InventoryState.WARM)
    recs = [
        _Rec("u1", 0, NV, "nvidia-rtx4090", 24564, 512, True, warm),
        _Rec("u1", 1, NV, "nvidia-rtx4090", 24564, 0, True, warm),
        _Rec("u2", 0, NV, "nvidia-rtx4090", 24564, 0, True, warm),
        _Rec("u3", 0, NV, "nvidia-rtx4070", 12282, 0, True, warm),
        _Rec("u4", 0, NV, "nvidia-rtx4090", 24564, 0, False, warm),  # not schedulable
        _Rec("u5", 0, NV, "nvidia-rtx4090", 0, 0, True, int(InventoryState.COLD)),  # cold
    ]
    pools = build_pools(recs)
    assert [(p.vendor, p.gpu_class) for p in pools] == [
        (NV, "nvidia-rtx4090"),
        (NV, "nvidia-rtx4070"),
    ]
    assert pools[0].nodes == [("u1", 2, 24052), ("u2", 1, 24564)]
    assert pools[1].nodes == [("u3", 1, 12282)]


def test_estimate_weight_bytes_table() -> None:
    """Check: estimate weight bytes table."""
    assert estimate_weight_bytes(12, NONE, "bfloat16") == 24_000_000_000
    assert estimate_weight_bytes(12, NONE, "float16") == 24_000_000_000
    assert estimate_weight_bytes(70, AWQ, "float16") == int(70e9 * 0.5 * 1.1)
    assert estimate_weight_bytes(70, GPTQ, "float16") == int(70e9 * 0.5 * 1.1)
    assert estimate_weight_bytes(10, int(Quantization.BNB), "float16") == int(10e9 * 0.5 * 1.15)
    assert estimate_weight_bytes(12, FP8, "bfloat16") == int(12e9)


def test_estimate_weight_bytes_rejects_unknown_dtype() -> None:
    """Check: estimate weight bytes rejects unknown dtype."""
    with pytest.raises(ValueError, match="dtype"):
        estimate_weight_bytes(12, NONE, "fp8")


def test_quant_matrix_matches_spec() -> None:
    """Check: quant matrix matches spec."""
    assert QUANT_MATRIX[GpuVendor.INTEL] == {Quantization.NONE}
    assert QUANT_MATRIX[GpuVendor.NVIDIA] == {NONE, AWQ, GPTQ, int(Quantization.BNB), FP8}
    assert QUANT_MATRIX[GpuVendor.AMD] == {NONE, AWQ, GPTQ, FP8}


@pytest.mark.parametrize(
    ("vendor", "cls", "allowed"),
    [
        (NV, "nvidia-rtx4090", True),
        (NV, "nvidia-rtx3090", False),
        (AMD, "amd-mi300x", True),
        (AMD, "amd-mi250", False),
        (AMD, "amd-instinct-mi300x", True),  # reference slug form
        (AMD, "amd-instinct-mi250x", False),
        (NV, "nvidia-rtx-6000-ada", True),
        (NV, "nvidia-rtx-a6000", False),
        (NV, "nvidia-rtx-pro-6000-blackwell", True),
        (NV, "nvidia-b300", True),
    ],
)
def test_quant_allowed_fp8_by_class(vendor: int, cls: str, allowed: bool) -> None:
    """Check: quant allowed fp8 by class."""
    assert quant_allowed(vendor, cls, FP8) is allowed


def test_quant_allowed_unknown_vendor_false() -> None:
    """Check: quant allowed unknown vendor false."""
    assert quant_allowed(99, "x", NONE) is False


@pytest.mark.parametrize(
    ("slug", "label"),
    [
        ("nvidia-rtx4090", "RTX 4090"),
        ("amd-mi300x", "MI300X"),
        ("intel-max1100", "MAX1100"),
        ("nvidia-h100", "H100"),
        ("amd-instinct-mi300x", "MI300X"),
    ],
)
def test_class_label(slug: str, label: str) -> None:
    """Check: class label."""
    assert class_label(slug) == label


def test_non_recommended_single_replica_and_multi_replica_explanations() -> None:
    """Check: non recommended single replica and multi replica explanations."""
    res = fit_check(70, AWQ, "float16", 4096, [pool(NV, "nvidia-rtx4090", n4090("a", 4))], False)
    assert not res.layouts[1].explanation.endswith("Recommended.")


def test_gptq_gate_message_lists_alternatives() -> None:
    """Check: gptq gate message lists alternatives."""
    res = fit_check(
        12,
        int(Quantization.BNB),
        "float16",
        4096,
        [pool(AMD, "amd-mi250", ("a", 1, 100000))],
        False,
    )
    assert res.refusal == "BNB is not supported on MI250. Choose AWQ, GPTQ, or bf16/fp16."


def test_two_alternatives_use_or_without_comma() -> None:
    """Check: two alternatives use or without comma."""
    res = fit_check(
        12, AWQ, "float16", 4096, [pool(INTEL, "intel-max1100", ("a", 1, 16384))], False
    )
    assert res.refusal == "AWQ is not supported on MAX1100. Choose bf16/fp16."


def test_fp8_alternatives_exclude_bnb_and_include_fp8_when_allowed() -> None:
    """Check: fp8 alternatives exclude bnb and include fp8 when allowed."""
    # BNB requested on Intel -> only bf16 offered; never lists BNB
    res = fit_check(
        12,
        int(Quantization.BNB),
        "float16",
        4096,
        [pool(INTEL, "intel-max1100", ("a", 1, 16384))],
        False,
    )
    assert res.refusal == "BNB is not supported on MAX1100. Choose bf16/fp16."


def test_gate_passes_when_any_pool_allows_and_filters_the_rest() -> None:
    """Check: gate passes when any pool allows and filters the rest."""
    pools = [pool(NV, "nvidia-rtx3090", n4090("a", 2)), pool(NV, "nvidia-rtx4090", n4090("b", 2))]
    res = fit_check(12, FP8, "bfloat16", 4096, pools, False)
    assert res.fits and all(x.gpu_class == "nvidia-rtx4090" for x in res.layouts)
