import pytest
from openpyxl import Workbook

from app.formula import WorkbookEvaluator, has_gross_up_divisor, parse_formula
from app.numbers import parse_number, split_line


@pytest.mark.parametrize(
    "token,value,percent,money",
    [
        ("1.234,56", 1234.56, None, False),
        ("R$ 1.234,56", 1234.56, None, True),
        ("R$1.800,00", 1800.0, None, True),
        ("0,65%", 0.65, "points", False),
        ("20%", 20.0, "points", False),
        ("1.234", 1234.0, None, False),
        ("0.65", 0.65, None, False),
        ("(12,50)", -12.5, None, False),
        ("2.500.000,00", 2500000.0, None, False),
    ],
)
def test_parse_number(token, value, percent, money):
    cell = parse_number(token)
    assert cell.value == pytest.approx(value)
    assert cell.percent == percent
    assert cell.money == money


@pytest.mark.parametrize("token", ["abc", "12/2026", "00.000.000/0001-00", "44h", ""])
def test_parse_number_rejects_text(token):
    assert parse_number(token) is None


def test_split_line_keeps_codes_in_label():
    cells = split_line("Submódulo 2.2 - Encargos")
    assert len(cells) == 1 and cells[0].text == "Submódulo 2.2 - Encargos"


def test_split_line_rate_and_money():
    cells = split_line("C.1 PIS 0,65% R$ 28,81")
    assert cells[0].text == "C.1 PIS"
    assert cells[1].value == 0.65 and cells[1].percent == "points"
    assert cells[2].value == 28.81 and cells[2].money


def test_split_line_dash_separator_and_accounting_zero():
    cells = split_line("A INSS - 0,00% R$ -")
    assert cells[0].text == "A INSS"
    assert cells[1].value == 0.0 and cells[1].percent == "points"
    assert cells[2].value == 0.0 and cells[2].money


def _wb(values: dict, sheet2: dict | None = None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan1"
    for coord, v in values.items():
        ws[coord] = v
    if sheet2:
        ws2 = wb.create_sheet("Outra aba")
        for coord, v in sheet2.items():
            ws2[coord] = v
    return WorkbookEvaluator(wb)


def test_evaluator_arithmetic_sum_and_percent():
    ev = _wb({"A1": 100, "A2": 50, "A3": "=SUM(A1:A2)*10%", "A4": "=ROUND(A3/3,2)", "A5": "=-2^2"})
    assert ev.try_value("Plan1", "A3") == pytest.approx(15.0)
    assert ev.try_value("Plan1", "A4") == pytest.approx(5.0)
    assert ev.try_value("Plan1", "A5") == pytest.approx(4.0)  # Excel: negação antes da potência


def test_evaluator_cross_sheet_and_if():
    ev = _wb({"A1": "='Outra aba'!B2*2", "A2": "=IF(A1>10,1,0)"}, {"B2": 7})
    assert ev.try_value("Plan1", "A1") == 14.0
    assert ev.try_value("Plan1", "A2") == 1.0


def test_evaluator_circular_and_unsupported_return_none():
    ev = _wb({"A1": "=A2+1", "A2": "=A1+1", "B1": "=VLOOKUP(1,C1:D2,2,0)"})
    assert ev.try_value("Plan1", "A1") is None
    assert ev.try_value("Plan1", "B1") is None


def test_gross_up_divisor_detection():
    assert has_gross_up_divisor(parse_formula("=D31/(1-C35)"))
    assert has_gross_up_divisor(parse_formula("=D31/(1-SUM(C32:C34))*C32"))
    assert not has_gross_up_divisor(parse_formula("=D31*C32"))
