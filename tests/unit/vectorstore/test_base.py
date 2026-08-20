"""Tests for shared.vectorstore.base value types and cosine_similarity edge cases."""

from __future__ import annotations

import pytest

from shared.vectorstore.base import (
    CollectionSpec,
    VectorCollectionMismatchError,
    cosine_similarity,
)


def test_cosine_similarity_identical_vectors_is_one() -> None:
    """Cosine similarity of a vector with itself is 1.0."""
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    """Cosine similarity of orthogonal vectors is 0.0."""
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors_is_negative_one() -> None:
    """Cosine similarity of opposite vectors is -1.0."""
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_mismatched_length_returns_zero() -> None:
    """Different-length vectors return 0.0 rather than raising."""
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_cosine_similarity_empty_vectors_returns_zero() -> None:
    """Empty vectors return 0.0 rather than a ZeroDivisionError."""
    assert cosine_similarity([], []) == 0.0


def test_cosine_similarity_zero_vector_returns_zero() -> None:
    """A zero-norm vector returns 0.0 rather than a ZeroDivisionError."""
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_collection_spec_defaults_to_cosine_distance() -> None:
    """CollectionSpec's distance metric defaults to cosine."""
    spec = CollectionSpec(name="c", dimensions=8, embedder_id="e")
    assert spec.distance == "cosine"


def test_vector_collection_mismatch_error_is_a_value_error() -> None:
    """VectorCollectionMismatchError is catchable as a plain ValueError."""
    assert issubclass(VectorCollectionMismatchError, ValueError)
