from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter


def create_pooled_session(*, pool_connections: int, pool_maxsize: int) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max(1, int(pool_connections)),
        pool_maxsize=max(1, int(pool_maxsize)),
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

