"""Providers and models.

A provider is a URL, a key and a format, reached through that format's own SDK. A model is an
alias the caller uses, pointing at a provider and the provider's own name for the model.
"""
from dataclasses import dataclass, field

from .formats import Format


@dataclass
class Provider:
    name: str
    format: Format
    url: str
    key: str
    client: object = field(init=False, repr=False)

    def __post_init__(self):
        self.client = self.format.client(self.url, self.key)


@dataclass
class Model:
    alias: str
    provider: Provider
    name: str
