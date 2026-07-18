"""Validates the real fastembed model assumption: BAAI/bge-small-en-v1.5
produces 384-dim float32 vectors. Env-gated (downloads ~130 MB on first run) so
CI stays hermetic — run locally with JC_RUN_EMBEDDING_MODEL_TEST=1."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("JC_RUN_EMBEDDING_MODEL_TEST"),
    reason="set JC_RUN_EMBEDDING_MODEL_TEST=1 to run the real-model test (downloads the model)",
)


def test_real_model_produces_384_dim_float32():
    import numpy as np

    from jobcannon.host.embeddings import EMBEDDING_DIM, _get_model

    model = _get_model()
    vectors = list(model.embed(["a short job description for a data analyst role"]))
    assert len(vectors) == 1
    vec = vectors[0]
    assert isinstance(vec, np.ndarray)
    assert vec.dtype == np.float32
    assert vec.shape == (EMBEDDING_DIM,)
