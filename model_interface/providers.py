"""Providers and models.

A provider is a URL, a key and a format. A model is an alias the caller uses, pointing at a
provider and the provider's own name for the model. The caller never sees the key or the name.
Every call streams: the stream is where the gateway watches, and where it can stop.
"""
from dataclasses import dataclass

import httpx

from .formats import Format

TIMEOUT = httpx.Timeout(connect=10, read=600, write=60, pool=10)


@dataclass
class Provider:
    name: str
    format: Format
    url: str
    key: str

    def stream(self, request: dict):
        """One model call, as canonical events. Closing the generator closes the connection, which stops the model."""
        with httpx.stream("POST", self.url.rstrip("/") + self.format.endpoint, json=self.format.request_out(request),
                          headers=self.format.auth(self.key), timeout=TIMEOUT) as response:
            if response.status_code >= 400:
                response.read()
                raise httpx.HTTPStatusError(f"{self.name} answered {response.status_code}", request=response.request, response=response)
            yield from self.format.events_in(response.iter_lines())


@dataclass
class Model:
    alias: str
    provider: Provider
    name: str

    def stream(self, request: dict):
        return self.provider.stream({**request, "model": self.name})
