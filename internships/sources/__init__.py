"""A Source is anything with a `.name: str` and a `.fetch() -> list[Listing]`.
No base class needed -- duck typing is enough for something this small."""
from typing import Protocol

from internships.models import Listing


class Source(Protocol):
    name: str

    def fetch(self) -> list[Listing]: ...
