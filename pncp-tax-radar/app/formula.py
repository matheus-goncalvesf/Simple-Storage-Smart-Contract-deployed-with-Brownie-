"""Avaliador seguro e determinístico para o subconjunto de fórmulas do Excel usado em planilhas de custos.

Suporta: números, referências (A1, $A$1, Planilha!A1, 'Nome da aba'!A1), intervalos em funções,
+ - * / ^ &, comparações, % pós-fixo e as funções SUM, MIN, MAX, AVERAGE, ROUND, ROUNDUP,
ROUNDDOWN, TRUNC, ABS e IF. Qualquer outra coisa levanta UnsupportedFormula.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable

from openpyxl.utils.cell import column_index_from_string, get_column_letter


class UnsupportedFormula(Exception):
    pass


class CircularReference(Exception):
    pass


_TOKEN = re.compile(
    r"""\s*(?:
    (?P<str>"(?:[^"]|"")*")
  | (?P<func>(?:_xlfn\.)?[A-Za-z][A-Za-z0-9_.]*)\s*\(
  | (?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
  | (?P<bool>TRUE|FALSE)\b
  | (?P<op><>|<=|>=|[-+*/^&=<>%(),;])
  )""",
    re.X | re.I,
)


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class Ref:
    sheet: str | None
    coord: str


@dataclass(frozen=True)
class Range:
    sheet: str | None
    start: str
    end: str


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Func:
    name: str
    args: tuple


def _tokenize(src: str) -> list[tuple[str, str]]:
    pos = 0
    out: list[tuple[str, str]] = []
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise UnsupportedFormula(f"trecho não reconhecido: {src[pos:pos + 20]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
        while pos < len(src) and src[pos].isspace():
            pos += 1
    return out


def _split_sheet(text: str) -> tuple[str | None, str]:
    if "!" in text:
        sheet, ref = text.rsplit("!", 1)
        if sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        return sheet, ref.replace("$", "").upper()
    return None, text.replace("$", "").upper()


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise UnsupportedFormula("fim inesperado da fórmula")
        self.i += 1
        return tok

    def expect(self, value: str) -> None:
        kind, val = self.take()
        if val != value:
            raise UnsupportedFormula(f"esperado {value!r}, veio {val!r}")

    def parse(self):
        node = self.comparison()
        if self.peek() is not None:
            raise UnsupportedFormula(f"sobrou {self.peek()[1]!r}")
        return node

    def comparison(self):
        node = self.concat()
        while (tok := self.peek()) and tok[1] in ("=", "<>", "<", ">", "<=", ">="):
            self.take()
            node = Binary(tok[1], node, self.concat())
        return node

    def concat(self):
        node = self.additive()
        while (tok := self.peek()) and tok[1] == "&":
            self.take()
            node = Binary("&", node, self.additive())
        return node

    def additive(self):
        node = self.term()
        while (tok := self.peek()) and tok[1] in ("+", "-"):
            self.take()
            node = Binary(tok[1], node, self.term())
        return node

    def term(self):
        node = self.power()
        while (tok := self.peek()) and tok[1] in ("*", "/"):
            self.take()
            node = Binary(tok[1], node, self.power())
        return node

    def power(self):
        node = self.unary()
        while (tok := self.peek()) and tok[1] == "^":
            self.take()
            node = Binary("^", node, self.unary())
        return node

    def unary(self):
        tok = self.peek()
        if tok and tok[1] in ("+", "-"):
            self.take()
            return Unary(tok[1], self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while (tok := self.peek()) and tok[1] == "%":
            self.take()
            node = Binary("/", node, Num(100.0))
        return node

    def primary(self):
        kind, val = self.take()
        if kind == "num":
            return Num(float(val))
        if kind == "str":
            return Str(val[1:-1].replace('""', '"'))
        if kind == "bool":
            return Num(1.0 if val.upper() == "TRUE" else 0.0)
        if kind == "ref":
            sheet, ref = _split_sheet(val)
            if ":" in ref:
                a, b = ref.split(":")
                return Range(sheet, a, b)
            return Ref(sheet, ref)
        if kind == "func":
            name = val.upper().removeprefix("_XLFN.")
            args = []
            if self.peek() and self.peek()[1] == ")":
                self.take()
                return Func(name, tuple())
            while True:
                args.append(self.comparison())
                kind2, sep = self.take()
                if sep == ")":
                    break
                if sep not in (",", ";"):
                    raise UnsupportedFormula(f"separador inesperado {sep!r}")
            return Func(name, tuple(args))
        if val == "(":
            node = self.comparison()
            self.expect(")")
            return node
        raise UnsupportedFormula(f"token inesperado {val!r}")


def parse_formula(formula: str):
    src = formula[1:] if formula.startswith("=") else formula
    return _Parser(_tokenize(src)).parse()


def iter_range(start: str, end: str):
    m1 = re.match(r"([A-Z]+)(\d+)", start)
    m2 = re.match(r"([A-Z]+)(\d+)", end)
    c1, r1 = column_index_from_string(m1.group(1)), int(m1.group(2))
    c2, r2 = column_index_from_string(m2.group(1)), int(m2.group(2))
    if (c2 - c1 + 1) * (r2 - r1 + 1) > 20000:
        raise UnsupportedFormula("intervalo grande demais")
    for r in range(min(r1, r2), max(r1, r2) + 1):
        for c in range(min(c1, c2), max(c1, c2) + 1):
            yield f"{get_column_letter(c)}{r}"


Resolver = Callable[[str, str], object]


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    raise UnsupportedFormula(f"valor não numérico: {v!r}")


def _round(x: float, n: int, mode: str) -> float:
    factor = 10 ** n
    if mode == "up":
        return math.copysign(math.ceil(abs(x) * factor - 1e-9) / factor, x)
    if mode == "down":
        return math.copysign(math.floor(abs(x) * factor + 1e-9) / factor, x)
    return math.copysign(math.floor(abs(x) * factor + 0.5 + 1e-9) / factor, x)


def evaluate(node, sheet: str, resolve: Resolver):
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Ref):
        return resolve(node.sheet or sheet, node.coord)
    if isinstance(node, Range):
        raise UnsupportedFormula("intervalo fora de função")
    if isinstance(node, Unary):
        v = _num(evaluate(node.operand, sheet, resolve))
        return -v if node.op == "-" else v
    if isinstance(node, Binary):
        a = evaluate(node.left, sheet, resolve)
        b = evaluate(node.right, sheet, resolve)
        op = node.op
        if op == "&":
            return f"{'' if a is None else a}{'' if b is None else b}"
        if op in ("=", "<>", "<", ">", "<=", ">="):
            x, y = _num(a), _num(b)
            return float({"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op])
        x, y = _num(a), _num(b)
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            if y == 0:
                raise UnsupportedFormula("divisão por zero")
            return x / y
        if op == "^":
            return x**y
    if isinstance(node, Func):
        return _call(node, sheet, resolve)
    raise UnsupportedFormula(f"nó desconhecido {node!r}")


def _flatten(args, sheet: str, resolve: Resolver) -> list[float]:
    vals: list[float] = []
    for arg in args:
        if isinstance(arg, Range):
            target = arg.sheet or sheet
            for coord in iter_range(arg.start, arg.end):
                v = resolve(target, coord)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(float(v))
        else:
            vals.append(_num(evaluate(arg, sheet, resolve)))
    return vals


def _call(node: Func, sheet: str, resolve: Resolver):
    name, args = node.name, node.args
    if name == "SUM":
        return sum(_flatten(args, sheet, resolve))
    if name in ("MIN", "MAX", "AVERAGE"):
        vals = _flatten(args, sheet, resolve)
        if not vals:
            return 0.0
        return {"MIN": min, "MAX": max, "AVERAGE": lambda v: sum(v) / len(v)}[name](vals)
    if name in ("ROUND", "ROUNDUP", "ROUNDDOWN", "TRUNC"):
        x = _num(evaluate(args[0], sheet, resolve))
        n = int(_num(evaluate(args[1], sheet, resolve))) if len(args) > 1 else 0
        mode = {"ROUND": "half", "ROUNDUP": "up", "ROUNDDOWN": "down", "TRUNC": "down"}[name]
        return _round(x, n, mode)
    if name == "ABS":
        return abs(_num(evaluate(args[0], sheet, resolve)))
    if name == "IF":
        cond = _num(evaluate(args[0], sheet, resolve))
        if cond:
            return evaluate(args[1], sheet, resolve) if len(args) > 1 else 1.0
        return evaluate(args[2], sheet, resolve) if len(args) > 2 else 0.0
    raise UnsupportedFormula(f"função não suportada: {name}")


def references(node) -> list[Ref | Range]:
    found: list[Ref | Range] = []

    def walk(n):
        if isinstance(n, (Ref, Range)):
            found.append(n)
        elif isinstance(n, Unary):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)
        elif isinstance(n, Func):
            for a in n.args:
                walk(a)

    walk(node)
    return found


def has_gross_up_divisor(node) -> bool:
    """True se a fórmula contém uma divisão por (1 - x), o divisor do cálculo 'por dentro'."""

    def is_one_minus(n) -> bool:
        return isinstance(n, Binary) and n.op == "-" and isinstance(n.left, Num) and n.left.value == 1.0

    def walk(n) -> bool:
        if isinstance(n, Binary):
            if n.op == "/" and is_one_minus(n.right):
                return True
            return walk(n.left) or walk(n.right)
        if isinstance(n, Unary):
            return walk(n.operand)
        if isinstance(n, Func):
            return any(walk(a) for a in n.args)
        return False

    return walk(node)


class WorkbookEvaluator:
    """Resolve o valor de cada célula: valor salvo pelo Excel quando existe; senão recalcula a fórmula."""

    def __init__(self, formulas_wb, values_wb=None):
        self.fwb = formulas_wb
        self.vwb = values_wb
        self._memo: dict[tuple[str, str], object] = {}
        self._stack: set[tuple[str, str]] = set()
        self._names = {ws.title.lower(): ws.title for ws in formulas_wb.worksheets}

    def _sheet(self, name: str):
        real = self._names.get(name.lower())
        if real is None:
            raise UnsupportedFormula(f"aba inexistente: {name}")
        return real

    def raw(self, sheet: str, coord: str):
        return self.fwb[self._sheet(sheet)][coord].value

    def value(self, sheet: str, coord: str):
        sheet = self._sheet(sheet)
        key = (sheet, coord)
        if key in self._memo:
            return self._memo[key]
        raw = self.fwb[sheet][coord].value
        if isinstance(raw, str) and raw.startswith("="):
            cached = self.vwb[sheet][coord].value if self.vwb is not None else None
            if isinstance(cached, (int, float)) and not isinstance(cached, bool):
                result = float(cached)
            else:
                if key in self._stack:
                    raise CircularReference(f"referência circular em {sheet}!{coord}")
                self._stack.add(key)
                try:
                    result = evaluate(parse_formula(raw), sheet, self.value)
                finally:
                    self._stack.discard(key)
        elif isinstance(raw, bool):
            result = 1.0 if raw else 0.0
        elif isinstance(raw, (int, float)):
            result = float(raw)
        elif raw is None:
            result = None
        elif isinstance(raw, str):
            result = raw
        else:
            raise UnsupportedFormula(f"tipo de célula não suportado em {coord}")
        self._memo[key] = result
        return result

    def try_value(self, sheet: str, coord: str) -> float | None:
        try:
            v = self.value(sheet, coord)
        except (UnsupportedFormula, CircularReference, RecursionError, KeyError, ValueError):
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return None
