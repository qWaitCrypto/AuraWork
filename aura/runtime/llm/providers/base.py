from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..types import CanonicalRequest, ModelProfile


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    json: dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> "PreparedRequest":
        headers = dict(self.headers)
        if "Authorization" in headers:
            headers["Authorization"] = "REDACTED"
        if "x-api-key" in headers:
            headers["x-api-key"] = "REDACTED"
        return PreparedRequest(method=self.method, url=self.url, headers=headers, json=self.json)


class ProviderAdapter(Protocol):
    def prepare_request(self, profile: ModelProfile, request: CanonicalRequest) -> PreparedRequest: ...

