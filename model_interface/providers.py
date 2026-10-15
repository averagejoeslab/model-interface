"""Providers and models.

A provider is a URL, a key and a format. A model is an alias the caller uses, pointing at a
provider and the provider's own name for the model. The caller never sees the key or the name.
"""
from dataclasses import dataclass

import httpx

from .formats import Format


@dataclass
class Provider:
    name: str
    format: Format
    url: str
    key: str

    def complete(self, request: dict) -> dict:
        """One model call. Canonical in, canonical out; the format does the translating."""
        response = httpx.post(self.url.rstrip("/") + self.format.endpoint, json=self.format.request_out(request),
                              headers=self.format.auth(self.key), timeout=600)
        response.raise_for_status()
        return self.format.response_in(response.json())


@dataclass
class Model:
    alias: str
    provider: Provider
    name: str

    def complete(self, request: dict) -> dict:
        return self.provider.complete({**request, "model": self.name})
