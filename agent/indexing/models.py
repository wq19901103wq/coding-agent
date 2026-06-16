from dataclasses import dataclass


@dataclass
class Symbol:
    path: str
    name: str
    kind: str
    line: int
    column: int
    scope: str | None = None
    signature: str | None = None


@dataclass
class Reference:
    path: str
    name: str
    line: int
    column: int
    is_definition: bool = False
