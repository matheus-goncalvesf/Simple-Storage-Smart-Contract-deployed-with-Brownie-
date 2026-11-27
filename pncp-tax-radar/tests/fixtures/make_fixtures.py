"""Gera planilhas sintéticas no formato da IN 5/2017 com erros tributários propositais.

Uso: python tests/fixtures/make_fixtures.py  (grava os arquivos nesta pasta)
"""

from __future__ import annotations

import sys
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent

ENCARGOS_PADRAO = [
    ("A", "INSS", 0.20),
    ("B", "Salário-Educação", 0.025),
    ("C", "SAT/RAT", 0.03),
    ("D", "SESC ou SESI", 0.015),
    ("E", "SENAI ou SENAC", 0.01),
    ("F", "SEBRAE", 0.006),
    ("G", "INCRA", 0.002),
    ("H", "FGTS", 0.08),
]
ENCARGOS_SIMPLES_SEM_CPP = [
    ("A", "INSS", 0.0),
    ("B", "Salário-Educação", 0.0),
    ("C", "SAT/RAT", 0.03),
    ("D", "SESC ou SESI", 0.0),
    ("E", "SENAI ou SENAC", 0.0),
    ("F", "SEBRAE", 0.0),
    ("G", "INCRA", 0.0),
    ("H", "FGTS", 0.08),
]


@dataclass
class PlanSpec:
    posto: str = "Servente de limpeza 44h"
    objeto: str = "Prestação de serviços continuados de limpeza e conservação predial com dedicação exclusiva de mão de obra"
    regime: str = "Regime de tributação: Lucro Presumido"
    licitante: str = "Empresa Exemplo Serviços Ltda"
    cnpj: str = "12.345.678/0001-90"
    pregao: str = "90012/2026"
    salario: float = 1800.00
    adicional: float = 282.40
    encargos: list = field(default_factory=lambda: list(ENCARGOS_PADRAO))
    beneficios: list = field(default_factory=lambda: [("A", "Vale-transporte", 180.0), ("B", "Auxílio-alimentação", 550.0)])
    insumos: list = field(default_factory=lambda: [("A", "Uniformes", 45.0), ("B", "Materiais de limpeza", 120.0)])
    ci: float = 0.03
    lucro: float = 0.05
    tributos: list = field(default_factory=lambda: [("C.1", "PIS", 0.0065), ("C.2", "COFINS", 0.03), ("C.3", "ISS", 0.05)])
    por_dentro: bool = True


def compute(s: PlanSpec) -> dict:
    rem = s.salario + s.adicional
    enc = [(c, n, r, rem * r) for c, n, r in s.encargos]
    enc_total = sum(v for *_, v in enc)
    ben_total = sum(v for *_, v in s.beneficios)
    ins_total = sum(v for *_, v in s.insumos)
    sub = rem + enc_total + ben_total + ins_total
    ci = sub * s.ci
    lucro = (sub + ci) * s.lucro
    base = sub + ci + lucro
    t = sum(r for *_, r in s.tributos)
    if s.por_dentro:
        price = base / (1 - t)
        taxes = [(c, n, r, price * r) for c, n, r in s.tributos]
    else:
        taxes = [(c, n, r, base * r) for c, n, r in s.tributos]
        price = base + sum(v for *_, v in taxes)
    return {
        "rem": rem, "enc": enc, "enc_total": enc_total, "enc_rate": sum(r for *_, r in s.encargos),
        "sub": sub, "ci": ci, "lucro": lucro, "base": base, "t": t, "taxes": taxes,
        "taxes_total": sum(v for *_, v in taxes), "price": price,
    }


def header_lines(s: PlanSpec) -> list[str]:
    return [
        "PLANILHA DE CUSTOS E FORMAÇÃO DE PREÇOS",
        f"Pregão Eletrônico nº {s.pregao}",
        f"Licitante: {s.licitante} — CNPJ {s.cnpj}",
        f"Objeto: {s.objeto}",
        s.regime,
        f"Posto: {s.posto}",
    ]


# ---------------------------------------------------------------- xlsx


def write_xlsx(s: PlanSpec, path: Path, formulas: bool = True) -> Path:
    from openpyxl import Workbook

    v = compute(s)
    wb = Workbook()
    ws = wb.active
    ws.title = "Posto 1"
    money, pct = '"R$" #,##0.00', "0.00%"
    r = 1

    def put(code=None, label=None, rate=None, value=None):
        nonlocal r
        if code is not None:
            ws.cell(r, 1, code)
        if label is not None:
            ws.cell(r, 2, label)
        if rate is not None:
            ws.cell(r, 3, rate).number_format = pct
        if value is not None:
            ws.cell(r, 4, value).number_format = money
        r += 1
        return r - 1

    for line in header_lines(s):
        put(label=line)
    put()
    put(label="MÓDULO 1 – COMPOSIÇÃO DA REMUNERAÇÃO")
    r_sal = put("A", "Salário-base", value=s.salario)
    r_adic = put("B", "Adicional de insalubridade", value=s.adicional)
    r_rem = put(None, "Total da Remuneração (Módulo 1)", value=f"=SUM(D{r_sal}:D{r_adic})" if formulas else v["rem"])
    put(label="SUBMÓDULO 2.2 – ENCARGOS PREVIDENCIÁRIOS, FGTS E OUTRAS CONTRIBUIÇÕES")
    enc_rows = []
    for (code, name, rate), (*_, val) in zip(s.encargos, v["enc"]):
        rr = r
        enc_rows.append(put(code, name, rate, f"=D{r_rem}*C{rr}" if formulas else val))
    r_enc = put(None, "Total do Submódulo 2.2",
                f"=SUM(C{enc_rows[0]}:C{enc_rows[-1]})" if formulas else v["enc_rate"],
                f"=SUM(D{enc_rows[0]}:D{enc_rows[-1]})" if formulas else v["enc_total"])
    put(label="MÓDULO 3 – BENEFÍCIOS MENSAIS E DIÁRIOS")
    first_extra = r
    for code, name, val in s.beneficios:
        put(code, name, value=val)
    put(label="MÓDULO 5 – INSUMOS DIVERSOS")
    for code, name, val in s.insumos:
        put(code, name, value=val)
    last_extra = r - 1
    r_sub = put(None, "Subtotal (Módulos 1 a 5)",
                value=f"=D{r_rem}+D{r_enc}+SUM(D{first_extra}:D{last_extra})" if formulas else v["sub"])
    put(label="MÓDULO 6 – CUSTOS INDIRETOS, TRIBUTOS E LUCRO")
    rr = r
    r_ci = put("A", "Custos Indiretos", s.ci, f"=D{r_sub}*C{rr}" if formulas else v["ci"])
    rr = r
    r_luc = put("B", "Lucro", s.lucro, f"=(D{r_sub}+D{r_ci})*C{rr}" if formulas else v["lucro"])
    r_base = put(None, "Base para tributos", value=f"=D{r_sub}+D{r_ci}+D{r_luc}" if formulas else v["base"])
    first_tax = r
    r_tt = first_tax + len(s.tributos)
    r_total = r_tt + 1
    for (code, name, rate), (*_, val) in zip(s.tributos, v["taxes"]):
        rr = r
        if formulas:
            f = f"=$D${r_total}*C{rr}" if s.por_dentro else f"=D{r_base}*C{rr}"
        put(code, name, rate, f if formulas else val)
    put(None, "Total de tributos",
        f"=SUM(C{first_tax}:C{r_tt - 1})" if formulas else v["t"],
        f"=SUM(D{first_tax}:D{r_tt - 1})" if formulas else v["taxes_total"])
    if formulas:
        total_f = f"=D{r_base}/(1-C{r_tt})" if s.por_dentro else f"=D{r_base}+D{r_tt}"
    put(None, "VALOR TOTAL POR EMPREGADO", value=total_f if formulas else v["price"])
    wb.save(path)
    return path


# ---------------------------------------------------------------- pdf / docx


def br(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def plan_lines(s: PlanSpec) -> list[tuple[str, str, str | None, str | None]]:
    v = compute(s)
    out: list[tuple[str, str, str | None, str | None]] = []
    out.append(("", "MÓDULO 1 - COMPOSIÇÃO DA REMUNERAÇÃO", None, None))
    out.append(("A", "Salário-base", None, br(s.salario)))
    out.append(("B", "Adicional de insalubridade", None, br(s.adicional)))
    out.append(("", "Total da Remuneração (Módulo 1)", None, br(v["rem"])))
    out.append(("", "SUBMÓDULO 2.2 - ENCARGOS PREVIDENCIÁRIOS, FGTS E OUTRAS CONTRIBUIÇÕES", None, None))
    for code, name, rate, val in v["enc"]:
        out.append((code, name, br(rate * 100) + "%", br(val)))
    out.append(("", "Total do Submódulo 2.2", br(v["enc_rate"] * 100) + "%", br(v["enc_total"])))
    out.append(("", "MÓDULO 3 - BENEFÍCIOS MENSAIS E DIÁRIOS", None, None))
    for code, name, val in s.beneficios:
        out.append((code, name, None, br(val)))
    out.append(("", "MÓDULO 5 - INSUMOS DIVERSOS", None, None))
    for code, name, val in s.insumos:
        out.append((code, name, None, br(val)))
    out.append(("", "Subtotal (Módulos 1 a 5)", None, br(v["sub"])))
    out.append(("", "MÓDULO 6 - CUSTOS INDIRETOS, TRIBUTOS E LUCRO", None, None))
    out.append(("A", "Custos Indiretos", br(s.ci * 100) + "%", br(v["ci"])))
    out.append(("B", "Lucro", br(s.lucro * 100) + "%", br(v["lucro"])))
    out.append(("", "Base para tributos", None, br(v["base"])))
    for code, name, rate, val in v["taxes"]:
        out.append((code, name, br(rate * 100) + "%", br(val)))
    out.append(("", "Total de tributos", br(v["t"] * 100) + "%", br(v["taxes_total"])))
    out.append(("", "VALOR TOTAL POR EMPREGADO", None, br(v["price"])))
    return out


def write_pdf(s: PlanSpec, path: Path) -> Path:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    for line in header_lines(s):
        pdf.cell(0, 5, line.replace("—", "-").replace("–", "-"), new_x="LMARGIN", new_y="NEXT")
    for code, label, rate, val in plan_lines(s):
        parts = [p for p in (code, label, rate, f"R$ {val}" if val else None) if p]
        pdf.cell(0, 5, " ".join(parts), new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


def write_docx(s: PlanSpec, path: Path) -> Path:
    import docx

    d = docx.Document()
    for line in header_lines(s):
        d.add_paragraph(line)
    rows = plan_lines(s)
    table = d.add_table(rows=len(rows), cols=4)
    for i, (code, label, rate, val) in enumerate(rows):
        cells = table.rows[i].cells
        cells[0].text = code
        cells[1].text = label
        cells[2].text = rate or ""
        cells[3].text = f"R$ {val}" if val else ""
    d.save(str(path))
    return path


def write_edital_docx(path: Path, objeto: str, pregao: str) -> Path:
    import docx

    d = docx.Document()
    d.add_paragraph(f"EDITAL DO PREGÃO ELETRÔNICO Nº {pregao}")
    d.add_paragraph("Órgão: Universidade Federal Exemplo")
    d.add_paragraph(f"Objeto: {objeto}")
    d.add_paragraph(
        "As licitantes tributadas pelo Lucro Real poderão cotar alíquotas efetivas de PIS e COFINS, "
        "desde que comprovadas. As optantes pelo Simples Nacional observarão a LC 123/2006."
    )
    d.save(str(path))
    return path


def write_scanned_pdf(path: Path) -> Path:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.rect(20, 20, 170, 250)
    pdf.output(str(path))
    return path


# ---------------------------------------------------------------- catálogo

SIMPLES = "Empresa optante pelo Simples Nacional"
SIMPLES_ANEXO_IV = [("C.1", "Simples Nacional - alíquota efetiva", 0.085)]


def specs() -> dict[str, PlanSpec]:
    base = PlanSpec()
    return {
        "limpeza_presumido_correta": base,
        "limpeza_simples_sem_cpp": replace(base, regime=SIMPLES, encargos=list(ENCARGOS_SIMPLES_SEM_CPP), tributos=SIMPLES_ANEXO_IV),
        "limpeza_grossup_por_fora": replace(base, por_dentro=False),
        "vigilancia_real_pis_cofins_misturado": replace(
            base, posto="Vigilante 12x36 diurno",
            objeto="Prestação de serviços de vigilância patrimonial armada com dedicação exclusiva de mão de obra",
            regime="Regime de tributação: Lucro Real",
            tributos=[("C.1", "PIS", 0.0165), ("C.2", "COFINS", 0.03), ("C.3", "ISS", 0.05)],
        ),
        "limpeza_iss_abaixo_minimo": replace(base, tributos=[("C.1", "PIS", 0.0065), ("C.2", "COFINS", 0.03), ("C.3", "ISS", 0.015)]),
        "recepcao_simples": replace(
            base, posto="Recepcionista 44h",
            objeto="Prestação de serviços de recepção com dedicação exclusiva de mão de obra",
            regime=SIMPLES, encargos=list(ENCARGOS_SIMPLES_SEM_CPP), tributos=SIMPLES_ANEXO_IV,
            insumos=[("A", "Uniformes", 45.0)],
        ),
        "limpeza_fgts_errado_por_fora": replace(
            base, lucro=0.02, por_dentro=False,
            encargos=[e if e[1] != "FGTS" else ("H", "FGTS", 0.02) for e in ENCARGOS_PADRAO],
        ),
        "copeira_simples": replace(
            base, posto="Copeira 44h", pregao="90077/2026",
            objeto="Prestação de serviços de copeiragem com dedicação exclusiva de mão de obra",
            regime=SIMPLES, encargos=list(ENCARGOS_SIMPLES_SEM_CPP), tributos=SIMPLES_ANEXO_IV,
        ),
    }


def build_all(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sp = specs()
    files: dict[str, Path] = {}
    for name in (
        "limpeza_presumido_correta", "limpeza_simples_sem_cpp", "limpeza_grossup_por_fora",
        "vigilancia_real_pis_cofins_misturado", "limpeza_iss_abaixo_minimo", "recepcao_simples",
    ):
        files[name] = write_xlsx(sp[name], out_dir / f"{name}.xlsx")
    files["limpeza_presumido_correta_valores"] = write_xlsx(
        sp["limpeza_presumido_correta"], out_dir / "limpeza_presumido_correta_valores.xlsx", formulas=False
    )
    files["limpeza_fgts_errado_por_fora_pdf"] = write_pdf(sp["limpeza_fgts_errado_por_fora"], out_dir / "limpeza_fgts_errado_por_fora.pdf")
    files["limpeza_simples_sem_cpp_docx"] = write_docx(sp["limpeza_simples_sem_cpp"], out_dir / "limpeza_simples_sem_cpp.docx")
    files["digitalizado_pdf"] = write_scanned_pdf(out_dir / "digitalizado.pdf")
    corrupted = out_dir / "corrompido.xlsx"
    corrupted.write_bytes(b"isto nao e um xlsx \x00\x01\x02" * 50)
    files["corrompido"] = corrupted

    tmp = out_dir / "_lote_tmp"
    tmp.mkdir(exist_ok=True)
    edital = write_edital_docx(tmp / "edital.docx", sp["copeira_simples"].objeto, "90077/2026")
    proposta = write_xlsx(sp["copeira_simples"], tmp / "proposta_vencedora.xlsx")
    lote = out_dir / "lote_copeiragem.zip"
    with zipfile.ZipFile(lote, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(edital, "edital.docx")
        zf.write(proposta, "proposta_vencedora.xlsx")
        zf.writestr("../fora_da_pasta.txt", "tentativa de path traversal")
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    files["lote_copeiragem"] = lote
    return files


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    for k, p in build_all(target).items():
        print(f"{k}: {p}")
