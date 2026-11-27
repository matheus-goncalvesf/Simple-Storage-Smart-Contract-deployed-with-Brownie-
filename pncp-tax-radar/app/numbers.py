"""Leitura e formatação de números no padrão brasileiro (1.234,56 / 0,65%)."""

from __future__ import annotations

import re
import unicodedata

from .models import Cell

_BR_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$")
_BR_DECIMAL = re.compile(r"^(?:\d{1,3}(?:\.\d{3})+|\d+),\d+$")
_US_DECIMAL = re.compile(r"^\d+\.\d+$")
_DIGITS = re.compile(r"^\d+$")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


def parse_number(token: str) -> Cell | None:
    """Converte um token isolado ('R$ 1.234,56', '0,65%', '(12,00)') em Cell numérica."""
    t = (token or "").strip().replace(" ", " ")
    if not t:
        return None
    money = False
    percent = None
    negative = False
    if t.upper().startswith("R$"):
        money = True
        t = t[2:].strip()
    if t.endswith("%"):
        percent = "points"
        t = t[:-1].strip()
    if t.startswith("(") and t.endswith(")"):
        negative = True
        t = t[1:-1].strip()
    if t.startswith("-"):
        negative = True
        t = t[1:].strip()
    t = t.replace(" ", "")
    if _BR_DECIMAL.match(t):
        value = float(t.replace(".", "").replace(",", "."))
    elif _BR_THOUSANDS.match(t) and not t.startswith("0."):
        value = float(t.replace(".", ""))
    elif _US_DECIMAL.match(t):
        value = float(t)
    elif _DIGITS.match(t):
        value = float(t)
    else:
        return None
    if negative:
        value = -value
    return Cell(text=token.strip(), value=value, percent=percent, money=money)


def split_line(text: str) -> list[Cell]:
    """Separa uma linha de texto em rótulo + números finais.

    Só os números no fim da linha contam (ex.: 'C.1 PIS 0,65% R$ 25,32'); números no
    meio do rótulo, como 'Submódulo 2.2', ficam no rótulo.
    """
    tokens = text.split()
    start = len(tokens)
    while start > 0:
        tok = tokens[start - 1]
        if tok.upper() in ("R$", "%", "-", "–") or parse_number(tok) is not None:
            start -= 1
        else:
            break
    zone = tokens[start:]
    while zone and zone[0] in ("-", "–", "%"):
        start += 1
        zone = zone[1:]
    cells: list[Cell] = []
    label = " ".join(tokens[:start]).strip().rstrip("-–:").strip()
    if label:
        cells.append(Cell(text=label))
    pending_money = False
    for tok in zone:
        up = tok.upper()
        if up == "R$":
            pending_money = True
            continue
        if tok in ("-", "–"):
            if pending_money:
                cells.append(Cell(text="R$ -", value=0.0, money=True))
            pending_money = False
            continue
        if tok == "%":
            if cells and cells[-1].value is not None and cells[-1].percent is None:
                cells[-1].percent = "points"
            continue
        cell = parse_number(tok)
        if cell is None:
            continue
        if pending_money:
            cell.money = True
            cell.text = f"R$ {tok}"
        pending_money = False
        cells.append(cell)
    return cells


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    s = f"{abs(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-R$ {s}" if value < 0 else f"R$ {s}"


def fmt_pct(fraction: float | None, digits: int = 2) -> str:
    if fraction is None:
        return "—"
    return f"{fraction * 100:.{digits}f}%".replace(".", ",")


def fmt_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}".replace(".", ",")
