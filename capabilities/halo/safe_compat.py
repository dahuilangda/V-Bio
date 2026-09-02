"""Runtime compatibility shim: `safe` (datamol-io) on transformers >= 5.x.

The safe package imports symbols that transformers 5.x removed
(DisjunctiveConstraint / PhrasalConstraint / several transformers.utils
helpers), so a plain `import safe` fails on modern stacks. The shim restores
those symbols in place before safe loads. Idempotent: every patch is guarded
by hasattr, so running it on a transformers version that still ships the
symbols is a no-op.

Applied from halo/__init__ so any `halo.*` import patches transformers before
the first lazy `import safe` inside the module bodies.
"""


def apply() -> None:
    try:
        import transformers.generation as _tg
    except ImportError:
        return

    if not hasattr(_tg, "DisjunctiveConstraint"):
        class DisjunctiveConstraint:  # noqa: D101
            pass

        class PhrasalConstraint:  # noqa: D101
            pass

        _tg.DisjunctiveConstraint = DisjunctiveConstraint
        _tg.PhrasalConstraint = PhrasalConstraint

    try:
        import transformers.utils as _tu
    except ImportError:
        return

    if not hasattr(_tu, "download_url"):
        import requests

        def download_url(url, *args, **kwargs):
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            return response.content

        _tu.download_url = download_url

    if not hasattr(_tu, "is_offline_mode"):
        import os

        _tu.is_offline_mode = lambda: os.environ.get("HF_HUB_OFFLINE", "0") == "1"

    if not hasattr(_tu, "is_remote_url"):
        def _is_remote_url(url):
            return isinstance(url, str) and url.startswith(("http://", "https://", "hf://"))

        _tu.is_remote_url = _is_remote_url

    if not hasattr(_tu, "working_or_temp_dir"):
        import contextlib
        import os
        import tempfile

        @contextlib.contextmanager
        def _working_or_temp_dir(working_dir, **kwargs):
            if working_dir is not None:
                yield working_dir
            else:
                with tempfile.TemporaryDirectory(**kwargs) as tmp:
                    yield tmp

        _tu.working_or_temp_dir = _working_or_temp_dir

    if not hasattr(_tu, "extract_commit_hash"):
        import re

        def _extract_commit_hash(response, *args, **kwargs):
            if response is None or isinstance(response, str):
                return None
            header = response.headers.get("X-Repo-Commit")
            return header if header and re.fullmatch("[0-9a-f]{40}", header or "") else None

        _tu.extract_commit_hash = _extract_commit_hash


apply()
