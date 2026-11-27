"""Rascunhos de tese por achado e de razões de recurso por caso."""

from __future__ import annotations

from collections import OrderedDict

from .legal import RECURSO_BASIS, REVIEW_NOTICE
from .models import Alert
from .numbers import fmt_money
from .rules_engine import VERDICT_TEXT
from .models import Verdict
from .sheet_parser import CaseContext


def build_tese(alert: Alert) -> str:
    lines = [
        f"TESE — {alert.title}",
        "",
        f"Na planilha ({alert.block}; {alert.location}) consta: {alert.found}.",
        f"O correto: {alert.expected}.",
    ]
    if alert.impact is not None:
        lucro = f"{fmt_money(alert.lucro)}" if alert.lucro is not None else "não identificado"
        lines.append(f"Impacto da correção mantendo o preço: {fmt_money(alert.impact)} por unidade da planilha; lucro declarado: {lucro}.")
    lines.append(VERDICT_TEXT[Verdict(alert.verdict)])
    for s in alert.scenarios:
        imp = fmt_money(s["impacto"]) if s.get("impacto") is not None else "—"
        lines.append(f"  • Cenário {s['cenario']} ({s['aliquota']}): impacto {imp}")
    lines += [
        "",
        f"Fundamentação: {alert.legal_basis}",
        "",
        f"Pedido sugerido: {alert.actionable_solution}",
        "",
        f"({REVIEW_NOTICE})",
    ]
    return "\n".join(lines)


def build_recurso(ctx: CaseContext, alerts: list[Alert]) -> str:
    relevant = [a for a in alerts if a.severity == "ALTA"] or [a for a in alerts if a.severity == "MEDIA"]
    if not relevant:
        return ""
    orgao = ctx.orgao or "[ÓRGÃO]"
    pregao = ctx.pregao or "[NÚMERO/ANO]"
    recorrida = ctx.licitante or "[RECORRIDA]"
    cnpj = f", inscrita no CNPJ sob o nº {ctx.cnpj}" if ctx.cnpj else ", inscrita no CNPJ sob o nº [CNPJ DA RECORRIDA]"

    out = [
        f"RASCUNHO — {REVIEW_NOTICE}",
        "",
        f"ILUSTRÍSSIMO(A) SENHOR(A) AGENTE DE CONTRATAÇÃO / PREGOEIRO(A) — {orgao}",
        "",
        f"Pregão Eletrônico nº {pregao}",
        "",
        "[RECORRENTE], inscrita no CNPJ sob o nº [CNPJ], por seu representante legal, vem, com fundamento no "
        "art. 165, I, \"b\", da Lei nº 14.133/2021, apresentar RAZÕES DE RECURSO contra a aceitação da proposta de "
        f"{recorrida}{cnpj}, pelos fatos e fundamentos a seguir.",
        "",
        "I — DOS FATOS",
        "",
        "A planilha de custos e formação de preços da recorrida contém os seguintes erros tributários:",
        "",
    ]
    for i, a in enumerate(relevant, start=1):
        out.append(f"{i}. {a.title} — {a.block}")
        out.append(f"   Na planilha: {a.found}.")
        out.append(f"   O correto: {a.expected}.")
        out.append(f"   Local: {a.location}.")
        if a.impact is not None:
            lucro = fmt_money(a.lucro) if a.lucro is not None else "não identificado"
            out.append(f"   Impacto mantido o preço: {fmt_money(a.impact)} por unidade da planilha; lucro declarado: {lucro}.")
        out.append("")

    by_block: "OrderedDict[str, list[Alert]]" = OrderedDict()
    for a in relevant:
        by_block.setdefault(a.block, []).append(a)
    out += ["II — DO DIREITO", ""]
    bases = list(OrderedDict.fromkeys(a.legal_basis for a in relevant))
    for basis in bases:
        out.append(f"• {basis}")
    out += ["", RECURSO_BASIS, ""]
    for block, items in by_block.items():
        with_impact = [a for a in items if a.impact is not None and a.rule_id != "R99"]
        total = sum(a.impact for a in with_impact)
        lucro = next((a.lucro for a in items if a.lucro is not None), None)
        if with_impact and lucro is not None:
            relation = "superior" if total > lucro else "inferior"
            out.append(
                f"No {block}, as correções somam {fmt_money(total)} por unidade da planilha, valor {relation} ao lucro "
                f"declarado de {fmt_money(lucro)}."
                + (" A proposta, portanto, não se sustenta sem aumento de preço, o que o item 7.9 do Anexo VII-A da "
                   "IN SEGES/MP nº 5/2017 não admite." if total > lucro else "")
            )
    out += [
        "",
        "III — DOS PEDIDOS",
        "",
        "a) o provimento do recurso, com a desclassificação da proposta da recorrida por inexequibilidade "
        "(art. 59, III, da Lei nº 14.133/2021);",
        "b) subsidiariamente, a realização de diligência (art. 59, § 2º) para que a recorrida demonstre a "
        "exequibilidade com a planilha corrigida, sem alteração do preço ofertado;",
        "c) a convocação da licitante seguinte na ordem de classificação.",
        "",
        "Nestes termos, pede deferimento.",
        "",
        "[Local], [data].",
        "",
        "[RECORRENTE — representante legal]",
    ]
    return "\n".join(out)
