import asyncio
import shutil
from pathlib import Path

from app.database import Database
from app.pipeline import JobManager
from app.radar import RadarConfig
from app.sweep import pick_company_files, run_proposal_sweep

CONTRATO = {
    "numeroControlePNCP": "11111111000111-2-000010/2026", "numeroControlePncpCompra": "11111111000111-1-000005/2026",
    "objetoContrato": "Prestação de serviços de vigilância patrimonial armada", "anoContrato": 2026, "sequencialContrato": 10,
    "orgaoEntidade": {"cnpj": "11111111000111", "razaoSocial": "Instituto Exemplo"},
    "nomeRazaoSocialFornecedor": "Vigilância Exemplo Ltda", "niFornecedor": "22333444000155", "valorGlobal": 700000.0,
}
OUTRO = {**CONTRATO, "numeroControlePNCP": "11111111000111-2-000011/2026", "objetoContrato": "Aquisição de computadores"}


class FakeClient:
    def __init__(self, fx):
        self.fx = fx

    async def list_contratos(self, ini, fim, pagina=1, tamanho=500):
        return {"data": [CONTRATO, OUTRO] if pagina == 1 else []}

    async def list_arquivos_contrato(self, cnpj, ano, seq):
        return [{"titulo": "Termo de contrato", "url": "u/1"}, {"titulo": "Planilha de custos da contratada", "url": "u/2"}]

    async def list_arquivos(self, cnpj, ano, seq):
        return [{"titulo": "Edital", "url": "u/3"}, {"titulo": "Planilha estimativa", "url": "u/4"}]

    async def download(self, url, dest: Path, suggested_name=None, max_bytes=0):
        dest.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(self.fx["limpeza_simples_sem_cpp"], dest / "planilha_contratada.xlsx"))


def test_pick_company_files():
    arqs = [{"titulo": "Edital"}, {"titulo": "PROPOSTA DO FORNECEDOR CONTRATADO"}, {"titulo": "Modelo de proposta"},
            {"titulo": "Planilha estimativa"}, {"titulo": "Planilha_de_custos"}]
    assert [a["titulo"] for a in pick_company_files(arqs, from_contract=True)] == ["PROPOSTA DO FORNECEDOR CONTRATADO", "Planilha_de_custos"]
    assert [a["titulo"] for a in pick_company_files(arqs, from_contract=False)] == ["PROPOSTA DO FORNECEDOR CONTRATADO"]


def test_sweep_audits_published_company_spreadsheets_once(fx, tmp_path):
    async def main():
        db = Database(tmp_path / "t.db")
        jobs = JobManager()
        cfg = RadarConfig(segments=["vigilancia", "limpeza"])
        first = jobs.start("sweep", {}, lambda job: run_proposal_sweep(job, FakeClient(fx), db, cfg, tmp_path / "dl"))
        await first.task
        second = jobs.start("sweep", {}, lambda job: run_proposal_sweep(job, FakeClient(fx), db, cfg, tmp_path / "dl"))
        await second.task
        return first, second, db

    first, second, db = asyncio.run(main())
    assert (first.status, first.total) == ("done", 1)  # o contrato de computadores fica de fora
    [audit] = db.list_audits()
    assert audit["context"]["orgao"] == "Instituto Exemplo"
    assert audit["meta"]["fornecedor"] == "Vigilância Exemplo Ltda"
    assert audit["alerts"][0]["rule_id"] == "R05"
    assert len(first.audit_ids) == 1
    assert second.total == 0  # contrato já varrido não é baixado de novo
