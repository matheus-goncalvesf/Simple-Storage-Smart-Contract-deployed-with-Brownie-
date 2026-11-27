import asyncio
import os
import shutil
import time
from datetime import date
from pathlib import Path

from app.database import Database
from app.pipeline import JobManager, ScanParams, run_inbox_scan, run_pncp_scan, select_files
from app.pncp_client import PNCPError

COMPRAS = [
    {"numeroControlePNCP": "11111111000111-1-000001/2026", "objetoCompra": "Serviços de limpeza e conservação",
     "orgaoEntidade": {"cnpj": "11111111000111", "razaoSocial": "Órgão A"}, "anoCompra": 2026, "sequencialCompra": 1},
    {"numeroControlePNCP": "22222222000122-1-000002/2026", "objetoCompra": "Limpeza hospitalar",
     "orgaoEntidade": {"cnpj": "22222222000122", "razaoSocial": "Órgão B"}, "anoCompra": 2026, "sequencialCompra": 2},
    {"numeroControlePNCP": "33333333000133-1-000003/2026", "objetoCompra": "Aquisição de computadores",
     "orgaoEntidade": {"cnpj": "33333333000133", "razaoSocial": "Órgão C"}, "anoCompra": 2026, "sequencialCompra": 3},
]


class FakeClient:
    def __init__(self, fx):
        self.fx = fx

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def iter_contratacoes(self, *args, **kwargs):
        yield COMPRAS

    async def list_arquivos(self, cnpj, ano, seq):
        if seq == 2:
            raise PNCPError("GET arquivos: falhou após 5 tentativas (ReadTimeout)")
        return [
            {"sequencialDocumento": 1, "titulo": "Edital", "statusAtivo": True},
            {"sequencialDocumento": 2, "titulo": "Planilha de custos vencedora", "statusAtivo": True},
            {"sequencialDocumento": 3, "titulo": "Antigo", "statusAtivo": False},
        ]

    def arquivo_url(self, cnpj, ano, seq, arquivo):
        return f"https://pncp.gov.br/fake/{seq}/{arquivo['sequencialDocumento']}"

    async def download(self, url, dest_dir: Path, *, suggested_name=None, max_bytes=0):
        if url.endswith("/1"):
            raise PNCPError("download: ReadTimeout")
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / "planilha_custos.xlsx"
        shutil.copy(self.fx["limpeza_simples_sem_cpp"], target)
        return target

    async def list_itens(self, cnpj, ano, seq):
        return [{"numeroItem": 1, "descricao": "Serviço de limpeza", "temResultado": True}]

    async def list_resultados(self, cnpj, ano, seq, item):
        return [{"nomeRazaoSocialFornecedor": "Vencedora Ltda", "niFornecedor": "12345678000190", "valorTotalHomologado": 100.0}]


PARAMS = ScanParams(data_inicial=date(2026, 1, 1), data_final=date(2026, 1, 31), keywords=["limpeza"], modalidades=[6])


def test_select_files_prioritizes_spreadsheets():
    arquivos = [{"titulo": "Edital"}, {"titulo": "Ata de julgamento"}, {"titulo": "Proposta vencedora"}, {"titulo": "X", "statusAtivo": False}]
    assert [a["titulo"] for a in select_files(arquivos, 10)] == ["Proposta vencedora", "Ata de julgamento", "Edital"]


def test_scan_isolates_failures_per_item_and_file(fx, tmp_path):
    async def main():
        db = Database(tmp_path / "t.db")
        jobs = JobManager()
        job = jobs.start("pncp", {}, lambda job: run_pncp_scan(job, FakeClient(fx), db, PARAMS, tmp_path / "dl"))
        events = [ev async for ev in job.stream()]
        return job, events, db

    job, events, db = asyncio.run(main())
    assert job.status == "done"
    assert (job.total, job.done, job.failures) == (2, 2, 1)  # o terceiro item não tem a palavra-chave
    types = [e["type"] for e in events]
    assert types[0] == "started" and types[-1] == "done"
    assert "item_error" in types and "item_done" in types
    [audit] = db.list_audits()
    assert audit["source"] == "pncp" and audit["ref"] == COMPRAS[0]["numeroControlePNCP"]
    assert any(a["rule_id"] == "R05" for a in audit["alerts"])
    assert any(d["error"] and "ReadTimeout" in d["error"] for d in audit["documents"])
    assert audit["meta"]["vencedores"][0]["fornecedor"] == "Vencedora Ltda"
    assert audit["pncp_url"] == "https://pncp.gov.br/app/editais/11111111000111/2026/1"


def test_scan_can_be_cancelled(fx, tmp_path):
    class SlowClient(FakeClient):
        async def list_arquivos(self, cnpj, ano, seq):
            await asyncio.sleep(3600)

    async def main():
        jobs = JobManager()
        db = Database(tmp_path / "t.db")
        job = jobs.start("pncp", {}, lambda job: run_pncp_scan(job, SlowClient(fx), db, PARAMS, tmp_path / "dl"))
        await asyncio.sleep(0.05)
        assert jobs.cancel(job.id)
        await asyncio.wait_for(job.task, 2)
        return job

    job = asyncio.run(main())
    assert job.status == "cancelled"
    assert job.events[-1]["type"] == "cancelled"


def test_inbox_processes_each_entry_once(fx, tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "caso2").mkdir(parents=True)
    a = shutil.copy(fx["limpeza_simples_sem_cpp"], inbox / "caso1.xlsx")
    b = shutil.copy(fx["recepcao_simples"], inbox / "caso2" / "proposta.xlsx")
    old = time.time() - 60
    for f in (a, b):
        os.utime(f, (old, old))

    async def main():
        db = Database(tmp_path / "t.db")
        jobs = JobManager()
        first = jobs.start("inbox", {}, lambda job: run_inbox_scan(job, db, inbox))
        await first.task
        second = jobs.start("inbox", {}, lambda job: run_inbox_scan(job, db, inbox))
        await second.task
        return first, second, db

    first, second, db = asyncio.run(main())
    assert (first.total, first.failures) == (2, 0)
    assert second.total == 0
    titles = sorted(x["title"] for x in db.list_audits())
    assert titles == ["caso1.xlsx", "caso2"]
