from __future__ import annotations

from curl_cffi import requests as curl_requests


class ChromeImpersonatingSession:
    """Requests-like session using a real Chrome TLS/HTTP fingerprint.

    Some issuer IR CDNs accept ordinary browsers but stall or reset generic
    Python/CI clients even when the URL is public. curl_cffi keeps the request
    direct to the issuer's official host while matching Chrome's transport
    fingerprint; no proxy, browser process, login, or third-party data source
    is involved.
    """

    def __init__(self) -> None:
        self._session = curl_requests.Session()

    def get(self, url: str, **kwargs):
        kwargs.setdefault("impersonate", "chrome")
        return self._session.get(url, **kwargs)
