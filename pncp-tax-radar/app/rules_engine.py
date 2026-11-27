"""Motor determinístico de regras tributárias para planilhas de custos de propostas.

Cada regra compara o que está na planilha com o que a lei exige, calcula o impacto da correção mantendo o
preço ofertado (por unidade da planilha, normalmente posto/mês) e compara com o lucro declarado:

- FORTE: correção maior que lucro + custos indiretos → a planilha não fecha sem aumentar o preço.
- MODERADO: maior que o lucro, cabe nos custos indiretos.
- SANAVEL: cabe no lucro → em regra a vencedora ajusta a planilha e continua (IN 5/2017, Anexo VII-A, 7.9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .legal import RULES
from .models import Alert, Severity, Verdict
from .numbers import fmt_money, fmt_pct
from .sheet_parser import TAX_FIELDS, CaseContext, CostBlock, FieldHit, block_category, doc_role

EPS = 0.0005  # tolerância de alíquota: 0,05 ponto percentual
PC_RATIO = 0.2169  # 0,65/3,00 = 0,2167 e 1,65/7,60 = 0,2171
PC_RATIO_TOL = 0.04
CUMULATIVO = 0.0365
NAO_CUMULATIVO = 0.0925

VERDICT_TEXT = {
    Verdict.FORTE: "A correção supera o lucro e os custos indiretos somados: a planilha não fecha sem aumentar o preço.",
    Verdict.MODERADO: "A correção supera o lucro declarado e só caberia consumindo os custos indiretos.",
    Verdict.SANAVEL: "A correção cabe no lucro: em regra a vencedora pode ajustar a planilha sem subir o preço.",
    Verdict.INDETERMINADO: "Não foi possível calcular o impacto com segurança: confirme manualmente.",
    Verdict.A_FAVOR: "O erro não reduz o custo da vencedora; serve só como apontamento de inconsistência.",
}


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.05, 0.0015 * abs(b))


@dataclass
class GrossUp:
    status: str  # correto | por_fora | misto | indeterminado | sem_tributos
    price: float | None = None
    price_estimated: bool = False
    base: float | None = None
    taxes: float = 0.0
    rate: float = 0.0
    impact: float | None = None
    hits: list[FieldHit] = field(default_factory=list)


@dataclass
class Margins:
    price: float | None
    lucro: float | None
    lucro_estimado: bool
    ci: float | None


def tax_hits(block: CostBlock) -> list[FieldHit]:
    seen: set[int] = set()
    out = []
    for h in block.all_hits(TAX_FIELDS):
        if id(h.row) in seen:
            continue
        if h.rate and h.value and h.rate > 0 and h.value > 0:
            seen.add(id(h.row))
            out.append(h)
    return out


def _values(rows) -> list[float]:
    return [c.value for r in rows for c in r.numbers if c.value is not None and not c.percent]


def _first_total_after(block: CostBlock, pos: int) -> float | None:
    for h in block.hits.get("total_geral", []):
        if block.row_pos(h.row) > pos and h.value:
            return h.value
    h = block.first("total_geral")
    return h.value if h and h.value else None


LEGAL_NOMINAL = {
    "pis": (0.0065, 0.0165),
    "cofins": (0.03, 0.076),
    "pis_cofins": (CUMULATIVO, NAO_CUMULATIVO),
    "iss": tuple(x / 1000 for x in range(20, 51, 5)),
}


def _rates_shown_over_base(hits: list[FieldHit]) -> list[float] | None:
    """Algumas planilhas exibem a alíquota como valor ÷ custo antes dos tributos, ou seja, nominal ÷ (1 − t).

    Se as alíquotas exibidas convertidas assim batem com as alíquotas legais, o cálculo foi feito por dentro
    (correto) e as alíquotas nominais são as convertidas.
    """
    s = sum(h.rate for h in hits)
    nominal = [h.rate / (1 + s) for h in hits]
    for h, n in zip(hits, nominal):
        legal = LEGAL_NOMINAL.get(h.field)
        if legal is None or not any(abs(n - x) <= 0.0002 for x in legal):
            return None
    return nominal


def analyze_gross_up(block: CostBlock) -> GrossUp:
    hits = tax_hits(block)
    if not hits:
        return GrossUp(status="sem_tributos", price=_first_total_after(block, -1))
    nominal = _rates_shown_over_base(hits)
    if nominal:
        for h, n in zip(hits, nominal):
            h.rate = n  # as regras seguintes passam a ver a alíquota legal
        T = sum(h.value for h in hits)
        price = median(h.value / h.rate for h in hits)
        return GrossUp(status="correto", price=price, base=price - T, taxes=T, rate=sum(nominal), impact=0.0, hits=hits)
    t = sum(h.rate for h in hits)
    T = sum(h.value for h in hits)
    bases = [h.value / h.rate for h in hits]
    first = min(block.row_pos(h.row) for h in hits)
    last = max(block.row_pos(h.row) for h in hits)
    before = _values(block.rows[:first])
    after = _values(block.rows[last + 1:])

    def has(vals, target):
        return any(close(v, target) for v in vals)

    g = GrossUp(status="indeterminado", taxes=T, rate=t, hits=hits)
    consistent = max(bases) - min(bases) <= max(0.05, 0.003 * max(bases))
    if consistent:
        X = median(bases)
        a_plus, a_x = has(after, X + T), has(after, X)
        if a_plus and not a_x:
            g.status = "por_fora"
        elif a_x and not a_plus:
            g.status = "correto"
        else:
            b_x, b_minus = has(before, X), has(before, X - T)
            if b_x and not b_minus:
                g.status = "por_fora"
            elif b_minus and not b_x:
                g.status = "correto"
        divides = [h.formula for h in hits if h.formula and "/" in h.formula]
        if g.status == "por_fora" and divides:
            # a fórmula do tributo divide por algo (provável 1 − t): sem prova pelos valores, não acusa
            g.status = "indeterminado"
        if g.status == "por_fora":
            g.price, g.base = X + T, X
        elif g.status == "correto":
            g.price, g.base = X, X - T
        else:
            g.price = _first_total_after(block, last)
            if g.price is None:
                g.price, g.price_estimated = X, True
    else:
        p_max = max(bases)
        if has(after, p_max):
            g.status, g.price, g.base = "misto", p_max, p_max - T
        else:
            g.price = _first_total_after(block, last) or p_max
            g.price_estimated = _first_total_after(block, last) is None
    if g.status in ("por_fora", "misto"):
        g.impact = t * g.price - T
    elif g.status == "correto":
        g.impact = 0.0
    return g


def margins(block: CostBlock, g: GrossUp) -> Margins:
    price = g.price
    lucro_h, ci_h = block.first("lucro"), block.first("ci")
    lucro = lucro_h.value if lucro_h and lucro_h.value is not None else None
    est = False
    if lucro is None and lucro_h and lucro_h.rate is not None and price:
        lucro, est = lucro_h.rate * price, True  # limite superior: lucro real incide sobre base menor que o preço
    ci = ci_h.value if ci_h and ci_h.value is not None else None
    if ci is None and ci_h and ci_h.rate is not None and price:
        ci = ci_h.rate * price
    return Margins(price=price, lucro=lucro, lucro_estimado=est, ci=ci)


def judge(impact: float | None, m: Margins) -> tuple[Verdict, Severity]:
    if impact is None:
        return Verdict.INDETERMINADO, Severity.MEDIA
    if impact <= 0.01:
        return Verdict.A_FAVOR, Severity.INFO
    if m.lucro is None:
        return Verdict.INDETERMINADO, Severity.MEDIA
    if impact > m.lucro + (m.ci or 0.0) + 0.01:
        return Verdict.FORTE, Severity.ALTA
    if impact > m.lucro + 0.01:
        return Verdict.MODERADO, Severity.ALTA
    return Verdict.SANAVEL, Severity.MEDIA


def _impact_sentence(impact: float | None, m: Margins) -> str:
    if impact is None:
        return ""
    parts = [f"Impacto da correção mantendo o preço: {fmt_money(impact)} por unidade da planilha (em geral posto/mês)"]
    if m.price:
        parts.append(f" ({fmt_pct(impact / m.price)} do preço de {fmt_money(m.price)})")
    if m.lucro is not None:
        est = " (estimado pela alíquota)" if m.lucro_estimado else ""
        parts.append(f"; lucro declarado: {fmt_money(m.lucro)}{est}")
        if m.ci is not None:
            parts.append(f"; custos indiretos: {fmt_money(m.ci)}")
    return "".join(parts) + "."


def make_alert(
    rule_id: str,
    block: CostBlock,
    m: Margins,
    *,
    found: str,
    expected: str,
    hits: list[FieldHit | None],
    impact: float | None,
    extra: str = "",
    title: str | None = None,
    scenarios: list[dict] | None = None,
    verdict: Verdict | None = None,
    severity: Severity | None = None,
) -> Alert:
    rule = RULES[rule_id]
    v, s = judge(impact, m)
    v, s = verdict or v, severity or s
    rows = [h.row for h in hits if h is not None]
    evidence_lines = []
    for h in hits:
        if h is None:
            continue
        line = h.row.raw()
        if h.formula:
            line += f"   [fórmula {h.value_cell.coord}: {h.formula}]"
        evidence_lines.append(f"{h.row.location}: {line}")
    explanation = " ".join(x for x in (rule["explanation"], extra, _impact_sentence(impact, m), VERDICT_TEXT[v]) if x)
    role = doc_role(block.doc)
    block_label = block.title + (" (documento do órgão, não da vencedora)" if role == "orgao" else "")
    return Alert(
        rule_id=rule_id,
        severity=s.value,
        verdict=v.value,
        title=title or rule["title"],
        evidence="\n".join(evidence_lines) or "(linha correspondente não encontrada na planilha)",
        location=rows[0].location if rows else block.doc,
        found=found,
        expected=expected,
        technical_explanation=explanation,
        legal_basis=rule["legal_basis"],
        actionable_solution=rule["solution"],
        doc=block.doc,
        block=block_label,
        impact=None if impact is None else round(impact, 2),
        lucro=None if m.lucro is None else round(m.lucro, 2),
        lucro_estimado=m.lucro_estimado,
        custos_indiretos=None if m.ci is None else round(m.ci, 2),
        preco=None if m.price is None else round(m.price, 2),
        scenarios=scenarios or [],
    )


# ---------------------------------------------------------------- regras

REGIME_NAME = {"simples": "Simples Nacional", "presumido": "Lucro Presumido", "real": "Lucro Real"}


def _regime_note(ctx: CaseContext) -> str:
    if not ctx.regime:
        return ""
    if ctx.regime_evidence == "informado manualmente":
        origin = "informado manualmente"
    elif ctx.regime_inferred:
        origin = "inferido da estrutura da planilha"
    else:
        origin = "declarado na proposta"
    return f"Regime da vencedora: {REGIME_NAME.get(ctx.regime, ctx.regime)} ({origin})."


def rule_pis_cofins(b: CostBlock, ctx: CaseContext, m: Margins) -> list[Alert]:
    if ctx.regime == "simples" or b.first("simples"):
        return []
    pis, cof, pc = b.first("pis"), b.first("cofins"), b.first("pis_cofins")
    P = m.price
    pis_r = pis.rate if pis else None
    cof_r = cof.rate if cof else None

    if pis_r is None and cof_r is None and (pc is None or pc.rate is None):
        if b.first("iss") is not None:
            impact = CUMULATIVO * P if P else None
            return [make_alert(
                "R02", b, m, title="PIS e COFINS ausentes da planilha",
                found="Nenhuma linha de PIS/COFINS na planilha (há linha de ISS)",
                expected=f"Fora do Simples, no mínimo {fmt_pct(CUMULATIVO)} (regime cumulativo) sobre o faturamento",
                hits=[b.first("iss")], impact=impact,
            )]
        return []

    if pis_r is not None and cof_r is not None and pis_r > 0 and cof_r > 0:
        t = pis_r + cof_r
        ratio = pis_r / cof_r
        ratio_txt = f"{ratio:.3f}".replace(".", ",")
        found = f"PIS {fmt_pct(pis_r)} + COFINS {fmt_pct(cof_r)} = {fmt_pct(t)} (proporção PIS/COFINS {ratio_txt})"
        if abs(ratio - PC_RATIO) > PC_RATIO_TOL:
            scen = []
            for name, target in (("Cumulativo (Lucro Presumido)", CUMULATIVO), ("Não cumulativo nominal (Lucro Real)", NAO_CUMULATIVO)):
                imp = (target - t) * P if P else None
                scen.append({"cenario": name, "aliquota": fmt_pct(target), "impacto": None if imp is None else round(imp, 2)})
            if ctx.regime == "presumido":
                impact, extra = scen[0]["impacto"], _regime_note(ctx)
            elif ctx.regime == "real":
                impact, extra = scen[1]["impacto"], _regime_note(ctx)
            else:
                imps = [s["impacto"] for s in scen if s["impacto"] is not None]
                extra = "O regime da vencedora não foi identificado nos documentos."
                if imps and all(i > 0 for i in imps):
                    impact = min(imps)
                elif imps and all(i <= 0 for i in imps):
                    impact = max(imps)
                else:
                    impact = None
                    extra += " O impacto depende do regime: veja os dois cenários."
            return [make_alert(
                "R01", b, m, found=found,
                expected="Mesmo regime para os dois: 0,65% + 3,00% (cumulativo) ou 1,65% + 7,60% (não cumulativo); proporção ≈ 0,217",
                hits=[pis, cof], impact=impact, extra=extra, scenarios=scen,
            )]
    elif pc is not None and pc.rate is not None:
        t = pc.rate
        found = f"PIS/COFINS {fmt_pct(t)} (linha única)"
    else:
        missing = "COFINS" if cof_r is None or cof_r == 0 else "PIS"
        minimum = 0.03 if missing == "COFINS" else 0.0065
        impact = minimum * P if P else None
        present = pis if missing == "COFINS" else cof
        return [make_alert(
            "R02", b, m, title=f"{missing} ausente ou zerado na planilha",
            found=f"{missing} sem alíquota na planilha",
            expected=f"{missing} de no mínimo {fmt_pct(minimum)} (regime cumulativo) sobre o faturamento",
            hits=[present], impact=impact,
        )]

    hits = [pis, cof] if pc is None else [pc]
    if ctx.regime == "presumido":
        if t < CUMULATIVO - EPS:
            return [make_alert(
                "R02", b, m, title="PIS/COFINS abaixo do regime cumulativo (Lucro Presumido)",
                found=found, expected="Lucro Presumido: PIS 0,65% + COFINS 3,00% = 3,65%, sem créditos",
                hits=hits, impact=(CUMULATIVO - t) * P if P else None, extra=_regime_note(ctx),
            )]
        if t > CUMULATIVO + 2 * EPS:
            return [make_alert(
                "R02", b, m, title="Lucro Presumido cotando PIS/COFINS acima do cumulativo",
                found=found, expected="Lucro Presumido: PIS 0,65% + COFINS 3,00% = 3,65%",
                hits=hits, impact=(CUMULATIVO - t) * P if P else None,
                extra=_regime_note(ctx) + " A alíquota cotada é maior que a devida (sobrepreço).",
            )]
    elif t < CUMULATIVO - EPS:
        scen = [{
            "cenario": "Sem comprovar créditos (alíquota nominal 9,25%)", "aliquota": fmt_pct(NAO_CUMULATIVO),
            "impacto": round((NAO_CUMULATIVO - t) * P, 2) if P else None,
        }]
        if ctx.regime != "real":
            scen.insert(0, {
                "cenario": "Se for Lucro Presumido (3,65%)", "aliquota": fmt_pct(CUMULATIVO),
                "impacto": round((CUMULATIVO - t) * P, 2) if P else None,
            })
        title = None if ctx.regime == "real" else "PIS/COFINS abaixo do mínimo do regime cumulativo"
        return [make_alert(
            "R09", b, m, title=title, found=found,
            expected="Alíquota efetiva abaixo da nominal só com créditos comprovados na EFD-Contribuições",
            hits=hits, impact=None, scenarios=scen, verdict=Verdict.INDETERMINADO, severity=Severity.MEDIA,
        )]
    return []


def rule_iss(b: CostBlock, ctx: CaseContext, m: Margins, cat: str | None) -> list[Alert]:
    iss = b.first("iss")
    P = m.price
    if iss is not None and iss.rate is not None and cat == "bens" and iss.rate > 0:
        return [make_alert(
            "R04", b, m, found=f"ISS {fmt_pct(iss.rate)} em contratação de fornecimento de bens",
            expected="Fornecimento de mercadorias sujeito ao ICMS, não ao ISS", hits=[iss], impact=None,
        )]
    if ctx.regime == "simples" or b.first("simples"):
        return []
    if iss is not None and iss.rate is not None:
        r = iss.rate
        if r < 0.02 - EPS and cat != "obras":
            return [make_alert(
                "R03", b, m, title="ISS abaixo da alíquota mínima de 2%" if r > 0 else "ISS zerado",
                found=f"ISS {fmt_pct(r)}", expected="Mínimo de 2% sobre o preço do serviço (LC 116/2003, art. 8º-A)",
                hits=[iss], impact=(0.02 - r) * P if P else None,
                extra="Usado o mínimo legal de 2% para o cálculo; se a alíquota do município for maior, o impacto também é maior.",
            )]
        if r > 0.05 + EPS:
            return [make_alert(
                "R03", b, m, title="ISS acima do teto de 5%", found=f"ISS {fmt_pct(r)}",
                expected="Máximo de 5% (LC 116/2003, art. 8º, II)", hits=[iss], impact=(0.05 - r) * P if P else None,
            )]
        return []
    has_pc = any(b.first(f) for f in ("pis", "cofins", "pis_cofins"))
    if iss is None and has_pc and cat != "bens":
        if cat == "obras":
            return [make_alert(
                "R03", b, m, title="ISS ausente da planilha", found="Nenhuma linha de ISS na planilha",
                expected="ISS do município da obra (construção civil pode ter alíquota abaixo de 2%)",
                hits=[b.first("pis") or b.first("pis_cofins")], impact=None,
            )]
        return [make_alert(
            "R03", b, m, title="ISS ausente da planilha", found="Nenhuma linha de ISS na planilha",
            expected="Mínimo de 2% sobre o preço do serviço (LC 116/2003, art. 8º-A)",
            hits=[b.first("pis") or b.first("pis_cofins")], impact=0.02 * P if P else None,
            extra="Usado o mínimo legal de 2% para o cálculo.",
        )]
    return []


def rule_inss(b: CostBlock, ctx: CaseContext, m: Margins, cat: str | None) -> list[Alert]:
    fgts, inss = b.first("fgts"), b.first("inss")
    if fgts is None and inss is None:
        return []
    base = b.encargos_base(exclude=("inss",))
    if inss is None:
        declared, found_txt = 0.0, "Linha de INSS patronal ausente na planilha"
    elif inss.rate is None:
        if inss.value:
            return []
        declared, found_txt = 0.0, f"INSS patronal zerado: {inss.row.raw()}"
    else:
        declared, found_txt = inss.rate, f"INSS patronal {fmt_pct(inss.rate)}"
    impact = max(0.0, 0.20 - declared) * base if base else None
    base_txt = f" sobre a base de encargos de {fmt_money(base)}" if base else ""
    hits = [inss or fgts]
    simples = ctx.regime == "simples"
    regime_note = _regime_note(ctx)

    if simples and cat == "cessao_outros" and ctx.dedicacao_exclusiva:
        return [make_alert(
            "R06", b, m, found=f"Optante do Simples prestando cessão de mão de obra ({b.title}); {found_txt}",
            expected=f"Empresa excluída do Simples, com INSS patronal de 20%{base_txt} (mais RAT e terceiros)",
            hits=hits, impact=impact, extra=regime_note,
        )]
    needs_20 = cat in ("limpeza", "vigilancia", "cessao_outros") or (cat == "obras" and simples)
    if needs_20 and declared < 0.20 - EPS:
        if simples:
            title = "Simples no Anexo IV sem a CPP: INSS patronal de 20% omitido"
            extra = f"{regime_note} A atividade ({cat}) é do Anexo IV, em que a CPP é recolhida fora do DAS."
        else:
            title = "INSS patronal abaixo de 20%"
            extra = f"{regime_note} Confirme se a vencedora alega desoneração da folha (CPRB); fora disso, a alíquota é 20%."
        return [make_alert(
            "R05", b, m, title=title, found=found_txt, expected=f"INSS patronal de 20%{base_txt}",
            hits=hits, impact=impact, extra=extra.strip(),
        )]
    return []


def rule_fgts(b: CostBlock, ctx: CaseContext, m: Margins) -> list[Alert]:
    fgts = b.first("fgts")
    if fgts is not None and fgts.rate is not None:
        r = fgts.rate
        base = b.encargos_base(exclude=("fgts",))
        if abs(r - 0.08) <= EPS:
            return []
        base_txt = f" sobre a base de encargos de {fmt_money(base)}" if base else ""
        return [make_alert(
            "R07", b, m, found=f"FGTS {fmt_pct(r)}", expected=f"FGTS de 8%{base_txt}",
            hits=[fgts], impact=(0.08 - r) * base if base else None,
        )]
    if fgts is None and b.first("inss") is not None:
        base = b.encargos_base()
        return [make_alert(
            "R07", b, m, title="FGTS ausente da planilha", found="Nenhuma linha de FGTS nos encargos",
            expected="FGTS de 8%" + (f" sobre a base de encargos de {fmt_money(base)}" if base else ""),
            hits=[b.first("inss")], impact=0.08 * base if base else None,
        )]
    return []


def rule_gross_up(b: CostBlock, ctx: CaseContext, m: Margins, g: GrossUp) -> list[Alert]:
    if g.status not in ("por_fora", "misto") or not g.price:
        return []
    if g.status == "por_fora":
        found = (
            f"Tributos de {fmt_money(g.taxes)} = {fmt_pct(g.rate)} aplicados sobre {fmt_money(g.base)}, "
            f"o subtotal antes dos tributos"
        )
    else:
        found = f"Parte dos tributos foi calculada sobre base menor que o preço final de {fmt_money(g.price)}"
    correct_price = g.base / (1 - g.rate) if g.base and g.rate < 1 else None
    expected = f"{fmt_pct(g.rate)} sobre o preço final de {fmt_money(g.price)} = {fmt_money(g.rate * g.price)}"
    if correct_price:
        expected += f"; para a mesma base, o preço correto seria {fmt_money(correct_price)}"
    return [make_alert("R08", b, m, found=found, expected=expected, hits=list(g.hits), impact=g.impact)]


def rule_combined(b: CostBlock, m: Margins, block_alerts: list[Alert]) -> list[Alert]:
    items = [a for a in block_alerts if a.impact is not None and a.impact > 0.01]
    if len(items) < 2 or m.lucro is None or any(a.severity == Severity.ALTA.value for a in items):
        return []
    total = sum(a.impact for a in items)
    v, s = judge(total, m)
    if s != Severity.ALTA:
        return []
    found = "; ".join(f"{a.rule_id} {a.title}: {fmt_money(a.impact)}" for a in items)
    rule = RULES["R99"]
    return [Alert(
        rule_id="R99", severity=s.value, verdict=v.value, title=rule["title"],
        evidence="\n".join(a.evidence for a in items), location=items[0].location,
        found=f"Soma das correções: {fmt_money(total)} ({found})",
        expected=f"Correções que caibam no lucro declarado de {fmt_money(m.lucro)}",
        technical_explanation=" ".join((rule["explanation"], _impact_sentence(total, m), VERDICT_TEXT[v])),
        legal_basis=rule["legal_basis"], actionable_solution=rule["solution"], doc=b.doc, block=items[0].block,
        impact=round(total, 2), lucro=round(m.lucro, 2), lucro_estimado=m.lucro_estimado,
        custos_indiretos=None if m.ci is None else round(m.ci, 2), preco=None if m.price is None else round(m.price, 2),
    )]


THIN_MARGIN = 0.02  # lucro + custos indiretos abaixo de 2% do preço


def rule_thin_margin(b: CostBlock, m: Margins, block_alerts: list[Alert]) -> list[Alert]:
    """Sem erro tributário que derrube, mas a margem é tão pequena que qualquer custo omitido derruba."""
    if not m.price or m.lucro is None or m.lucro_estimado or doc_role(b.doc) == "orgao":
        return []
    if any(a.severity == Severity.ALTA.value for a in block_alerts):
        return []
    margin = m.lucro + (m.ci or 0.0)
    if margin / m.price >= THIN_MARGIN:
        return []
    rule = RULES["R98"]
    lucro_h, ci_h = b.first("lucro"), b.first("ci")
    hits = [h for h in (ci_h, lucro_h) if h is not None]
    return [Alert(
        rule_id="R98", severity=Severity.MEDIA.value, verdict=Verdict.INDETERMINADO.value,
        title=f"Margem quase zero: qualquer custo omitido acima de {fmt_money(margin)} por mês derruba a proposta",
        evidence="\n".join(f"{h.row.location}: {h.row.raw()}" for h in hits),
        location=hits[0].row.location if hits else b.doc,
        found=f"Lucro {fmt_money(m.lucro)} + custos indiretos {fmt_money(m.ci or 0)} = {fmt_money(margin)} "
              f"({fmt_pct(margin / m.price)} do preço de {fmt_money(m.price)})",
        expected="Margem capaz de absorver ajustes da planilha sem aumentar o preço",
        technical_explanation=rule["explanation"], legal_basis=rule["legal_basis"], actionable_solution=rule["solution"],
        doc=b.doc, block=b.title, impact=None, lucro=round(m.lucro, 2), custos_indiretos=None if m.ci is None else round(m.ci, 2),
        preco=round(m.price, 2),
    )]


def audit_blocks(blocks: list[CostBlock], ctx: CaseContext) -> tuple[list[Alert], list[dict]]:
    alerts: list[Alert] = []
    summaries: list[dict] = []
    for b in blocks:
        g = analyze_gross_up(b)
        m = margins(b, g)
        cat = block_category(b, ctx)
        block_alerts: list[Alert] = []
        block_alerts += rule_pis_cofins(b, ctx, m)
        block_alerts += rule_iss(b, ctx, m, cat)
        block_alerts += rule_inss(b, ctx, m, cat)
        block_alerts += rule_fgts(b, ctx, m)
        block_alerts += rule_gross_up(b, ctx, m, g)
        block_alerts += rule_combined(b, m, block_alerts)
        block_alerts += rule_thin_margin(b, m, block_alerts)
        alerts += block_alerts
        summaries.append({
            "title": b.title, "doc": b.doc, "role": doc_role(b.doc), "category": cat,
            "price": None if m.price is None else round(m.price, 2), "price_estimated": g.price_estimated,
            "lucro": None if m.lucro is None else round(m.lucro, 2), "lucro_estimado": m.lucro_estimado,
            "custos_indiretos": None if m.ci is None else round(m.ci, 2),
            "gross_up": g.status, "fields": sorted(b.hits.keys()),
        })
    rank = {"ALTA": 3, "MEDIA": 2, "INFO": 1}
    alerts.sort(key=lambda a: (-rank[a.severity], -(a.impact or 0)))
    return alerts, summaries
