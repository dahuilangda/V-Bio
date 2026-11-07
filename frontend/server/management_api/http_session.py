from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter


def create_pooled_session(*, pool_connections: int, pool_maxsize: int) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max(1, int(pool_connections)),
        pool_maxsize=max(1, int(pool_maxsize)),
        # 池满时阻塞等待，避免突发流量下丢弃连接、慢源挤占内部调用。
        pool_block=True,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

