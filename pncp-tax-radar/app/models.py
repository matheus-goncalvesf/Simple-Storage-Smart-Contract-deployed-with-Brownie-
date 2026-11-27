from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    ALTA = "ALTA"
    MEDIA = "MEDIA"
    INFO = "INFO"


class Verdict(str, Enum):
    FORTE = "FORTE"  # correção excede lucro + custos indiretos declarados
    MODERADO = "MODERADO"  # excede o lucro, cabe nos custos indiretos
    SANAVEL = "SANAVEL"  # cabe no lucro: a vencedora pode ajustar sem subir o preço
    INDETERMINADO = "INDETERMINADO"
    A_FAVOR = "A_FAVOR"  # erro não reduz o custo da vencedora (sobrepreço ou inconsistência)


SEVERITY_RANK = {"ALTA": 3, "MEDIA": 2, "INFO": 1}
VERDICT_RANK = {"FORTE": 5, "MODERADO": 4, "INDETERMINADO": 3, "SANAVEL": 2, "A_FAVOR": 1}


@dataclass
class Cell:
    text: str
    value: float | None = None
    formula: str | None = None
    coord: str | None = None
    percent: str | None = None  # "fraction" = formato % do Excel; "points" = texto com "%"
    money: bool = False

    @property
    def is_label(self) -> bool:
        return self.value is None and self.formula is None and bool(self.text)


@dataclass
class Row:
    doc: str
    sheet: str
    index: int
    cells: list[Cell]

    @property
    def label(self) -> str:
        return " ".join(c.text for c in self.cells if c.is_label).strip()

    @property
    def numbers(self) -> list[Cell]:
        return [c for c in self.cells if c.value is not None]

    @property
    def location(self) -> str:
        return f"{self.doc} › {self.sheet} › linha {self.index}"

    def raw(self) -> str:
        return " | ".join(c.text for c in self.cells if c.text)


@dataclass
class Document:
    name: str
    kind: str
    text: str = ""
    rows: list[Row] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Alert:
    rule_id: str
    severity: str
    verdict: str
    title: str
    evidence: str
    location: str
    found: str
    expected: str
    technical_explanation: str
    legal_basis: str
    actionable_solution: str
    doc: str = ""
    block: str = ""
    impact: float | None = None
    lucro: float | None = None
    lucro_estimado: bool = False
    custos_indiretos: float | None = None
    preco: float | None = None
    scenarios: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
