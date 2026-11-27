import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.pipeline import JobManager
from app.pncp_client import PNCPError
from app.radar import RadarConfig, RadarScheduler, infer_status, match_segments, portal_name, run_radar_scan

NOW = datetime.now()
FUTURE = (NOW + timedelta(days=5)).isoformat(timespec="seconds")
PAST = (NOW - timedelta(days=2)).isoformat(timespec="seconds")


def compra(seq, objeto, encerramento, situacao="Divulgada no PNCP", uf="DF"):
    cnpj = "00509018000113"
    return {
        "numeroControlePNCP": f"{cnpj}-1-{seq:06d}/2026", "objetoCompra": objeto, "anoCompra": 2026,
        "sequencialCompra": seq, "numeroCompra": f"9{seq:04d}", "orgaoEntidade": {"cnpj": cnpj, "razaoSocial": "Ministério Exemplo"},
        "unidadeOrgao": {"ufSigla": uf, "municipioNome": "Brasília"}, "dataEncerramentoProposta": encerramento,
        "situacaoCompraNome": situacao, "valorTotalEstimado": 1_000_000.0,
        "linkSistemaOrigem": f"https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra={seq}",
    }


ABERTA = compra(1, "Contratação de serviços continuados de limpeza e conservação", FUTURE)
EM_DISPUTA = compra(2, "Serviços de vigilância patrimonial armada", PAST)
MATERIAL = compra(3, "Aquisição de materiais de limpeza", FUTURE)
TI = compra(4, "Contratação de serviços de tecnologia da informação", PAST)


class FakeRadarClient:
    def __init__(self, fail_endpoint=None):
        self.fail_endpoint = fail_endpoint

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def iter_contratacoes(self, ini, fim, modalidade, uf=None, endpoint="publicacao", max_pages=20):
        if endpoint == self.fail_endpoint:
            raise PNCPError("HTTP 503 depois de 5 tentativas")
        if modalidade != 6:
            return
        yield [ABERTA, MATERIAL, TI] if endpoint == "proposta" else [EM_DISPUTA, TI]

    async def list_itens(self, cnpj, ano, seq):
        return [{"numeroItem": 1, "descricao": "Vigilância", "temResultado": True}]

    async def list_resultados(self, cnpj, ano, seq, item):
        return [{"nomeRazaoSocialFornecedor": "Vigilância Forte Ltda", "niFornecedor": "11222333000144", "valorTotalHomologado": 900000.0}]


def test_match_segments():
    assert match_segments(ABERTA["objetoCompra"], ["limpeza", "vigilancia"]) == ["limpeza"]
    assert match_segments(MATERIAL["objetoCompra"], ["limpeza"]) == []
    assert match_segments("Registro de preços para a aquisições de fármacos e mão-de-obra? não", ["terceirizacao"]) == []
    assert match_segments("[Portal de Compras Públicas] - Aquisição de produtos de limpeza", ["limpeza"]) == []
    assert match_segments("Contratação de empresa para fornecimento de mão de obra terceirizada", ["terceirizacao"]) == ["terceirizacao"]
    assert match_segments("Ações de vigilância sanitária no município", ["vigilancia"]) == []
    assert match_segments("Execução de obra de reforma da escola", ["limpeza"]) == []
    assert match_segments("Execução de obra de reforma da escola", ["obras"]) == ["obras"]
    assert match_segments("Serviços de recepção e copeiragem com dedicação exclusiva", ["apoio", "terceirizacao"]) == ["apoio", "terceirizacao"]


def test_infer_status_and_portal():
    assert infer_status(ABERTA, [], NOW) == "propostas_abertas"
    assert infer_status(EM_DISPUTA, [], NOW) == "em_disputa"
    assert infer_status(EM_DISPUTA, [{"fornecedor": "X"}], NOW) == "resultado"
    assert infer_status(compra(9, "limpeza", FUTURE, situacao="Revogada"), [], NOW) == "encerrada"
    assert portal_name(ABERTA["linkSistemaOrigem"]) == "Compras.gov"
    assert portal_name("https://bllcompras.com/x") == "BLL"
    assert portal_name("sai.io.org.br/site/publicadopncp/1?x=2") == "sai.io.org.br"
    assert portal_name(" ") == "—"


def run_scan(tmp_path, client):
    async def main():
        db = Database(tmp_path / "t.db")
        jobs = JobManager()
        cfg = RadarConfig(segments=["limpeza", "vigilancia"])
        job = jobs.start("radar", {}, lambda job: run_radar_scan(job, client, db, cfg))
        await job.task
        return job, db

    return asyncio.run(main())


def test_radar_scan_saves_opportunities_and_finds_winner(tmp_path):
    job, db = run_scan(tmp_path, FakeRadarClient())
    assert job.status == "done" and job.failures == 0
    opps = {o["id"]: o for o in db.list_opportunities()}
    assert set(opps) == {ABERTA["numeroControlePNCP"], EM_DISPUTA["numeroControlePNCP"]}
    aberta = opps[ABERTA["numeroControlePNCP"]]
    assert aberta["status"] == "propostas_abertas" and aberta["segmentos"] == ["limpeza"]
    assert aberta["portal"] == "Compras.gov" and aberta["numero"] == "90001/2026"
    disputa = opps[EM_DISPUTA["numeroControlePNCP"]]
    assert disputa["status"] == "resultado"
    assert disputa["vencedores"][0]["fornecedor"] == "Vigilância Forte Ltda"
    assert db.setting_get("radar_last_run")["found"] == 2


def test_radar_scan_isolates_failed_query(tmp_path):
    job, db = run_scan(tmp_path, FakeRadarClient(fail_endpoint="atualizacao"))
    assert job.status == "done" and job.failures == 2  # uma por modalidade
    assert [o["id"] for o in db.list_opportunities()] == [ABERTA["numeroControlePNCP"]]


def test_scheduler_due(tmp_path):
    db = Database(tmp_path / "t.db")
    sched = RadarScheduler(db, lambda: None)
    assert sched.due()
    db.setting_put("radar_last_run", {"at": NOW.isoformat(timespec="seconds")})
    assert not sched.due(NOW + timedelta(hours=1))
    assert sched.due(NOW + timedelta(hours=7))
    RadarConfig(enabled=False).save(db)
    assert not sched.due(NOW + timedelta(hours=7))


@pytest.fixture
def api(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox", watch_inbox=False, radar_auto=False)
    app = create_app(settings, client_factory=lambda: FakeRadarClient())
    with TestClient(app) as client:
        yield client


def wait_job(client, job_id):
    for _ in range(200):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] != "running":
            return status
        asyncio.run(asyncio.sleep(0.02))
    raise AssertionError("job não terminou")


def test_radar_api_flow(api, fx):
    r = api.put("/api/radar/config", json={"segments": ["limpeza", "vigilancia"], "ufs": ["df"], "interval_hours": 4})
    assert r.status_code == 200 and r.json()["ufs"] == ["DF"]
    assert api.put("/api/radar/config", json={"segments": ["inexistente"]}).status_code == 422

    job = api.post("/api/radar/scan").json()
    assert wait_job(api, job["id"])["status"] == "done"
    status = api.get("/api/radar").json()
    assert status["last_run"]["found"] == 2 and status["config"]["interval_hours"] == 4

    opps = api.get("/api/opportunities").json()
    assert [o["status"] for o in opps] == ["propostas_abertas", "resultado"]
    assert "_item" not in opps[0]
    assert len(api.get("/api/opportunities", params={"segmento": "vigilancia"}).json()) == 1

    opp_id = EM_DISPUTA["numeroControlePNCP"]
    with open(fx["limpeza_simples_sem_cpp"], "rb") as f:
        r = api.post(f"/api/opportunities/{opp_id}/audit", files=[("files", ("planilha_vencedora.xlsx", f))])
    assert r.status_code == 200, r.text
    audit = r.json()
    assert audit["opportunity_id"] == opp_id
    assert audit["context"]["orgao"] == "Ministério Exemplo"
    assert audit["alerts"][0]["rule_id"] == "R05"
    assert "Ministério Exemplo" in audit["recurso"]
    linked = next(o for o in api.get("/api/opportunities").json() if o["id"] == opp_id)
    assert linked["audit_ids"] == [audit["id"]]
    assert api.post("/api/opportunities/nao-existe/audit", files=[("files", ("a.xlsx", b"x"))]).status_code == 404
