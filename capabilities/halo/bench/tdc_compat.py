"""Compatibility shim for TDC's learned oracles on modern scikit-learn.

The published DRD2/GSK3B/JNK3 checkpoints were pickled with sklearn <1.3,
whose tree node array lacks the ``missing_go_to_left`` field. sklearn >=1.3
hard-fails on load. The shim patches ``_tree._check_node_array`` to append
the missing zero field, which is exactly how the old format maps onto the
new semantics (no missing-value support in the old trees -> never route
left on missing). Import this BEFORE ``from tdc import Oracle``.
"""
import numpy as _np

try:
    from sklearn.tree import _tree as _sk_tree

    _orig_check = _sk_tree._check_node_ndarray

    _OLD_FIELDS = [
        ("left_child", "<i8"), ("right_child", "<i8"), ("feature", "<i8"),
        ("threshold", "<f8"), ("impurity", "<f8"), ("n_node_samples", "<i8"),
        ("weighted_n_node_samples", "<f8"),
    ]

    def _check_node_ndarray_compat(array, *args, **kwargs):
        # tolerate both positional-dtype and expected_dtype= call styles
        dtype = None
        if args:
            dtype = args[0]
        dtype = kwargs.get("expected_dtype", kwargs.get("dtype", dtype))
        names = list(array.dtype.names or [])
        expected = list(dtype.names or []) if dtype is not None else []
        if names == [f for f, _ in _OLD_FIELDS] and "missing_go_to_left" in expected:
            n = array.shape[0]
            new = _np.zeros(n, dtype=dtype)
            for f, _ in _OLD_FIELDS:
                new[f] = array[f]
            return new
        return _orig_check(array, *args, **kwargs)

    _sk_tree._check_node_ndarray = _check_node_ndarray_compat
except Exception:  # pragma: no cover - old sklearn already compatible
    pass
