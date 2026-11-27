from dataclasses import replace

import pytest

from app.auditor import audit_paths
from make_fixtures import ENCARGOS_PADRAO, PlanSpec, compute, specs, write_edital_docx, write_xlsx


def only(result, rule_id):
    found = [a for a in result["alerts"] if a["rule_id"] == rule_id]
    assert len(found) == 1, [(a["rule_id"], a["title"]) for a in result["alerts"]]
    return found[0]


def rule_ids(result):
    return sorted(a["rule_id"] for a in result["alerts"])


def audit_spec(tmp_path, spec: PlanSpec, name="proposta.xlsx", **kwargs):
    return audit_paths([write_xlsx(spec, tmp_path / name)], **kwargs)


# ---------------------------------------------------------------- fixtures com erro proposital


@pytest.mark.parametrize("key", ["limpeza_presumido_correta", "limpeza_presumido_correta_valores"])
def test_planilha_correta_nao_gera_alerta(fx, key):
    r = audit_paths([fx[key]])
    assert r["alerts"] == []
    [block] = r["blocks"]
    assert block["gross_up"] == "correto"
    assert block["price"] == pytest.approx(compute(PlanSpec())["price"], abs=0.01)
    assert block["title"] == "Posto: Servente de limpeza 44h"


@pytest.mark.parametrize("key", ["limpeza_simples_sem_cpp", "limpeza_simples_sem_cpp_docx"])
def test_simples_anexo_iv_sem_cpp_derruba(fx, key):
    r = audit_paths([fx[key]])
    assert r["context"]["regime"] == "simples"
    a = only(r, "R05")
    assert (a["severity"], a["verdict"]) == ("ALTA", "FORTE")
    assert a["impact"] == pytest.approx(0.20 * 2082.40, abs=0.02)
    assert "Anexo IV" in a["title"]
    assert "art. 18, § 5º-C" in a["legal_basis"]
    assert "INSS" in a["evidence"]
    assert rule_ids(r) == ["R05"]


def test_grossup_por_fora_e_sanavel_sozinho(fx):
    r = audit_paths([fx["limpeza_grossup_por_fora"]])
    a = only(r, "R08")
    v = compute(specs()["limpeza_grossup_por_fora"])
    assert (a["severity"], a["verdict"]) == ("MEDIA", "SANAVEL")
    assert a["impact"] == pytest.approx(v["t"] * v["price"] - v["taxes_total"], abs=0.01)
    assert "fórmula D" in a["evidence"] and "*C" in a["evidence"]
    assert rule_ids(r) == ["R08"]


def test_pis_cofins_de_regimes_diferentes_no_lucro_real(fx):
    r = audit_paths([fx["vigilancia_real_pis_cofins_misturado"]])
    a = only(r, "R01")
    price = r["blocks"][0]["price"]
    assert a["impact"] == pytest.approx((0.0925 - 0.0465) * price, abs=0.02)
    assert (a["severity"], a["verdict"]) == ("ALTA", "MODERADO")
    assert len(a["scenarios"]) == 2


def test_iss_abaixo_do_minimo(fx):
    r = audit_paths([fx["limpeza_iss_abaixo_minimo"]])
    a = only(r, "R03")
    assert a["impact"] == pytest.approx(0.005 * r["blocks"][0]["price"], abs=0.01)
    assert (a["severity"], a["verdict"]) == ("MEDIA", "SANAVEL")


def test_simples_em_recepcao_e_vedado(fx):
    r = audit_paths([fx["recepcao_simples"]])
    a = only(r, "R06")
    assert (a["severity"], a["verdict"]) == ("ALTA", "FORTE")
    assert "art. 17, XII" in a["legal_basis"]
    assert "R05" not in rule_ids(r)


def test_pdf_fgts_errado_e_grossup(fx):
    r = audit_paths([fx["limpeza_fgts_errado_por_fora_pdf"]])
    fgts = only(r, "R07")
    assert fgts["impact"] == pytest.approx(0.06 * 2082.40, abs=0.05)
    assert (fgts["severity"], fgts["verdict"]) == ("ALTA", "MODERADO")
    gross = only(r, "R08")
    assert gross["verdict"] == "SANAVEL"
    assert "R99" not in rule_ids(r)  # já existe achado ALTA isolado
    assert r["recurso"].startswith("RASCUNHO")
    assert "FGTS diferente de 8%" in r["recurso"]


def test_lote_zip_usa_edital_como_contexto_sem_herdar_regime(fx):
    r = audit_paths([fx["lote_copeiragem"]])
    assert r["context"]["regime"] == "simples"  # o edital cita Lucro Real de forma genérica
    assert r["context"]["pregao"] == "90077/2026"
    assert r["context"]["orgao"] == "Universidade Federal Exemplo"
    assert r["context"]["licitante"] == "Empresa Exemplo Serviços Ltda"
    a = only(r, "R06")
    assert a["verdict"] == "FORTE"
    assert "Universidade Federal Exemplo" in r["recurso"]


def test_arquivos_problematicos_nao_param_o_caso(fx):
    r = audit_paths([fx["corrompido"], fx["digitalizado_pdf"], fx["limpeza_simples_sem_cpp"]])
    errors = [d for d in r["documents"] if d["error"]]
    assert len(errors) == 1
    assert "R05" in rule_ids(r)


# ---------------------------------------------------------------- variações unitárias


def test_iss_acima_de_5_por_cento_e_a_favor(tmp_path):
    spec = replace(PlanSpec(), tributos=[("C.1", "PIS", 0.0065), ("C.2", "COFINS", 0.03), ("C.3", "ISS", 0.06)])
    a = only(audit_spec(tmp_path, spec), "R03")
    assert (a["severity"], a["verdict"]) == ("INFO", "A_FAVOR")
    assert a["impact"] < 0


def test_lucro_zero_torna_qualquer_erro_forte(tmp_path):
    spec = replace(PlanSpec(), lucro=0.0, ci=0.0,
                   tributos=[("C.1", "PIS", 0.0065), ("C.2", "COFINS", 0.03), ("C.3", "ISS", 0.015)])
    a = only(audit_spec(tmp_path, spec), "R03")
    assert (a["severity"], a["verdict"]) == ("ALTA", "FORTE")


def test_erros_pequenos_somados_superam_o_lucro(tmp_path):
    spec = replace(
        PlanSpec(), lucro=0.005, por_dentro=False,
        tributos=[("C.1", "PIS", 0.006), ("C.2", "COFINS", 0.028), ("C.3", "ISS", 0.018)],
    )
    r = audit_spec(tmp_path, spec)
    singles = [a for a in r["alerts"] if a["rule_id"] != "R99"]
    assert {a["rule_id"] for a in singles} == {"R02", "R03", "R08"}
    assert all(a["severity"] == "MEDIA" for a in singles)
    combo = only(r, "R99")
    assert combo["severity"] == "ALTA"
    assert combo["impact"] == pytest.approx(sum(a["impact"] for a in singles), abs=0.02)


def test_regime_desconhecido_mostra_cenarios(tmp_path):
    spec = replace(PlanSpec(), regime="", tributos=[("C.1", "PIS", 0.0065), ("C.2", "COFINS", 0.076), ("C.3", "ISS", 0.05)])
    a = only(audit_spec(tmp_path, spec), "R01")
    assert a["verdict"] == "INDETERMINADO" and a["severity"] == "MEDIA"
    impacts = [s["impacto"] for s in a["scenarios"]]
    assert impacts[0] < 0 < impacts[1]


def test_lucro_real_com_aliquota_efetiva_baixa_pede_comprovacao(tmp_path):
    spec = replace(PlanSpec(), regime="Regime de tributação: Lucro Real",
                   tributos=[("C.1", "PIS", 0.005), ("C.2", "COFINS", 0.023), ("C.3", "ISS", 0.05)])
    a = only(audit_spec(tmp_path, spec), "R09")
    assert a["verdict"] == "INDETERMINADO"
    assert "EFD-Contribuições" in a["expected"]


def test_pis_cofins_ausentes(tmp_path):
    spec = replace(PlanSpec(), tributos=[("C.1", "ISS", 0.05)])
    a = only(audit_spec(tmp_path, spec), "R02")
    assert a["title"] == "PIS e COFINS ausentes da planilha"
    assert a["impact"] == pytest.approx(0.0365 * a["preco"], abs=0.02)


def test_fgts_ausente(tmp_path):
    spec = replace(PlanSpec(), encargos=[e for e in ENCARGOS_PADRAO if e[1] != "FGTS"])
    a = only(audit_spec(tmp_path, spec), "R07")
    assert a["title"] == "FGTS ausente da planilha"
    assert a["impact"] == pytest.approx(0.08 * 2082.40, abs=0.02)


def test_inss_abaixo_de_20_fora_do_simples(tmp_path):
    spec = replace(PlanSpec(), encargos=[e if e[1] != "INSS" else ("A", "INSS", 0.10) for e in ENCARGOS_PADRAO])
    a = only(audit_spec(tmp_path, spec), "R05")
    assert a["title"] == "INSS patronal abaixo de 20%"
    assert a["impact"] == pytest.approx(0.10 * 2082.40, abs=0.02)


def test_obras_sem_simples_nao_cobra_inss_20(tmp_path):
    spec = replace(
        PlanSpec(), posto="Pedreiro", objeto="Execução de obras de reforma predial",
        encargos=[e if e[1] != "INSS" else ("A", "INSS", 0.10) for e in ENCARGOS_PADRAO],
    )
    assert "R05" not in rule_ids(audit_spec(tmp_path, spec))


def test_edital_sozinho_nao_define_regime(tmp_path):
    edital = write_edital_docx(tmp_path / "edital.docx", "serviços de limpeza", "123/2026")
    r = audit_paths([edital])
    assert r["context"]["regime"] is None
    assert r["alerts"] == []


def test_override_manual_de_categoria_e_regime(tmp_path):
    spec = specs()["limpeza_simples_sem_cpp"]
    r = audit_spec(tmp_path, spec, overrides={"categoria": "cessao_outros"})
    assert "R06" in rule_ids(r) and "R05" not in rule_ids(r)
    r2 = audit_spec(tmp_path, PlanSpec(), name="p2.xlsx", overrides={"regime": "simples"})
    assert r2["context"]["regime"] == "simples"
    assert r2["alerts"] == []


def test_tese_tem_fundamento_e_aviso_de_revisao(fx):
    r = audit_paths([fx["limpeza_simples_sem_cpp"]])
    tese = r["alerts"][0]["tese"]
    assert "Fundamentação:" in tese and "revise com advogado" in tese


# ---------------------------------------------------------------- layout real de edital (Anexo IV), preenchido


def test_layout_real_correto_nao_gera_alerta(fx):
    r = audit_paths([fx["layout_real_correto_pdf"]])
    assert r["alerts"] == []
    [block] = r["blocks"]
    assert block["title"] == "Posto: Vigilante 12x36 diurno armado"
    assert block["gross_up"] == "correto"
    assert block["category"] == "vigilancia"
    assert r["context"]["regime"] == "presumido"


def test_layout_real_simples_sem_cpp(fx):
    r = audit_paths([fx["layout_real_simples_pdf"]])
    a = only(r, "R05")
    assert (a["severity"], a["verdict"]) == ("ALTA", "FORTE")
    assert a["impact"] == pytest.approx(0.20 * 2860.00, abs=0.05)
    assert rule_ids(r) == ["R05"]


def test_modelo_em_branco_nao_e_auditado(fx):
    r = audit_paths([fx["modelo_em_branco_pdf"]])
    assert r["blocks"] == [] and r["alerts"] == []
    assert any("modelo de planilha em branco" in w for d in r["documents"] for w in d["warnings"])


def test_palavra_das_nao_vira_simples_nacional():
    from app.sheet_parser import detect_field

    assert detect_field("B Substituto na Cobertura das Ausências Legais") is None
    assert detect_field("C Multa do FGTS sobre o Aviso Prévio Indenizado") is None
    assert detect_field("2.2 GPS, FGTS e outras contribuições") is None
    assert detect_field("F Módulo 6 – Custos indiretos, tributos e lucro") is None
    assert detect_field("TOTAL") is None
    assert detect_field("H FGTS") == "fgts"
    assert detect_field("C.1 Simples Nacional - alíquota efetiva") == "simples"


# ---------------------------------------------------------------- planilha real do PNCP (estimativa de edital, 9 cargos)


def test_planilha_real_pncp_9_cargos():
    from pathlib import Path

    r = audit_paths([Path(__file__).parent / "fixtures" / "real_pncp_apoio_9_cargos.xlsx"])
    assert r["alerts"] == []
    assert len(r["blocks"]) == 9
    assert all(b["gross_up"] == "correto" for b in r["blocks"])
    copeiro = next(b for b in r["blocks"] if b["title"].startswith("Copeiro"))
    assert copeiro["price"] == pytest.approx(7284.24, abs=0.5)
    assert copeiro["lucro"] == pytest.approx(317.21, abs=0.5)
    assert copeiro["category"] == "cessao_outros"


def test_mao_de_obra_nao_e_obra_de_construcao():
    from app.radar import match_segments
    from app.sheet_parser import detect_category

    assert detect_category("Tipo de serviço: Mão de Obra; Copeira")[0] == "cessao_outros"
    assert match_segments("Contratação de mão de obra terceirizada de copeiragem", ["obras"]) == []
    assert match_segments("Execução de obra de reforma da escola", ["obras"]) == ["obras"]


def test_planilha_real_com_erros_injetados(tmp_path):
    """Mesma planilha real, com INSS zerado no Copeiro e ISS de 1,5% na Recepcionista."""
    from pathlib import Path

    import openpyxl

    wb = openpyxl.load_workbook(Path(__file__).parent / "fixtures" / "real_pncp_apoio_9_cargos.xlsx")
    copeiro = next(ws for ws in wb.worksheets if ws.title.startswith("Copeiro"))
    recep = next(ws for ws in wb.worksheets if ws.title.startswith("Recepcionista"))
    assert copeiro["B34"].value.strip() == "INSS"
    copeiro["D34"] = 0
    iss_row = next(c.row for c in recep["B"] if isinstance(c.value, str) and c.value.strip().upper() == "ISSQN")
    recep[f"D{iss_row}"] = 0.015
    target = tmp_path / "planilha_com_erros.xlsx"
    wb.save(target)

    r = audit_paths([target])
    by_block = {}
    for a in r["alerts"]:
        by_block.setdefault(a["block"].split(" (")[0], []).append(a)
    assert set(by_block) == {"Copeiro", "Recepcionista Executiva"}
    inss = by_block["Copeiro"][0]
    assert inss["rule_id"] == "R05" and inss["severity"] == "ALTA"
    assert inss["impact"] > inss["lucro"]
    iss = by_block["Recepcionista Executiva"][0]
    assert iss["rule_id"] == "R03"
    assert iss["impact"] == pytest.approx(0.005 * iss["preco"], rel=0.01)
