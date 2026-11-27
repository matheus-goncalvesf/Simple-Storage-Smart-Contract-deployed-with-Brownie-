"""Jobs em segundo plano com progresso em tempo real (SSE), cancelamento e isolamento de falha por item."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from .auditor import audit_documents
from .database import Database
from .extractor import extract_path
from .folder_watcher import audit_inbox_entry, list_inbox_entries
from .pncp_client import PNCPClient, PNCPError, compra_keys, matches_keywords, portal_url

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    kind: str
    params: dict
    status: str = "running"  # running | done | cancelled | error
    done: int = 0
    total: int = 0
    failures: int = 0
    stage: str = ""
    events: list[dict] = field(default_factory=list)
    audit_ids: list[int] = field(default_factory=list)
    task: asyncio.Task | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, type_: str, **data) -> dict:
        ev = {
            "seq": len(self.events), "type": type_, "job_id": self.id, "status": self.status,
            "done": self.done, "total": self.total, "failures": self.failures, "stage": self.stage, **data,
        }
        self.events.append(ev)
        self._changed.set()
        self._changed = asyncio.Event()
        return ev

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "params": self.params, "status": self.status, "done": self.done,
            "total": self.total, "failures": self.failures, "stage": self.stage, "audit_ids": self.audit_ids,
            "errors": [e for e in self.events if e["type"] == "item_error"][-50:],
        }

    async def stream(self, start: int = 0, keepalive: float = 15.0) -> AsyncIterator[dict]:
        i = start
        while True:
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self.status != "running":
                return
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), timeout=keepalive)
            except TimeoutError:
                yield {"type": "ping"}


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def start(self, kind: str, params: dict, runner: Callable[[Job], Awaitable[None]]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=params)
        self.jobs[job.id] = job

        async def wrapper() -> None:
            job.stage = "Iniciando..."
            job.emit("started")
            try:
                await runner(job)
                job.status, job.stage = "done", f"Concluído: {job.done}/{job.total} processados, {job.failures} falha(s)"
                job.emit("done")
            except asyncio.CancelledError:
                job.status, job.stage = "cancelled", f"Parado pelo usuário em {job.done}/{job.total}"
                job.emit("cancelled")
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s falhou", job.id)
                job.status, job.stage = "error", f"Erro: {exc}"
                job.emit("error", message=str(exc)[:500])

        job.task = asyncio.create_task(wrapper())
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status != "running" or job.task is None:
            return False
        job.task.cancel()
        return True

    def running(self, kind: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.kind == kind and j.status == "running"), None)


# ---------------------------------------------------------------- varredura PNCP


@dataclass
class ScanParams:
    data_inicial: date
    data_final: date
    keywords: list[str]
    modalidades: list[int]
    uf: str | None = None
    limite: int = 20
    endpoint: str = "publicacao"
    max_pages: int = 10
    max_files: int = 12


PRIORITY = [
    re.compile(r"propost|planilha|custo|pre[cç]o|composi", re.I),
    re.compile(r"\bata\b|julgamento|resultado|homolog|adjudic", re.I),
    re.compile(r"edital|termo de refer|\btr\b|anexo", re.I),
]


def select_files(arquivos: list[dict], max_files: int) -> list[dict]:
    active = [a for a in arquivos if a.get("statusAtivo", True) is not False]

    def score(a: dict) -> int:
        text = f"{a.get('titulo') or ''} {a.get('tipoDocumentoNome') or ''}"
        for i, pattern in enumerate(PRIORITY):
            if pattern.search(text):
                return i
        return len(PRIORITY)

    return sorted(active, key=score)[:max_files]


async def _cached(db: Database, key: str, max_age: timedelta, fetch: Callable[[], Awaitable]):
    hit = db.cache_get(key, max_age)
    if hit is not None:
        return hit
    value = await fetch()
    db.cache_put(key, value)
    return value


async def fetch_vencedores(client: PNCPClient, cnpj: str, ano: int, seq: int, max_items: int = 5) -> list[dict]:
    try:
        itens = await client.list_itens(cnpj, ano, seq)
    except Exception:  # noqa: BLE001 - informação complementar, não bloqueia a auditoria
        return []
    out = []
    for item in [i for i in itens if i.get("temResultado", True)][:max_items]:
        numero = item.get("numeroItem")
        if numero is None:
            continue
        try:
            resultados = await client.list_resultados(cnpj, ano, seq, numero)
        except Exception:  # noqa: BLE001
            continue
        for r in resultados:
            out.append({
                "item": numero,
                "descricao": (item.get("descricao") or "")[:120],
                "fornecedor": r.get("nomeRazaoSocialFornecedor"),
                "ni": r.get("niFornecedor"),
                "valor": r.get("valorTotalHomologado"),
                "porte": r.get("porteFornecedorNome"),
            })
    return out


def _compra_meta(item: dict, vencedores: list[dict]) -> dict:
    org = item.get("orgaoEntidade") or {}
    unidade = item.get("unidadeOrgao") or {}
    return {
        "numero_controle": item.get("numeroControlePNCP"),
        "orgao": org.get("razaoSocial"),
        "orgao_cnpj": org.get("cnpj"),
        "uf": unidade.get("ufSigla"),
        "municipio": unidade.get("municipioNome"),
        "modalidade": item.get("modalidadeNome"),
        "objeto": item.get("objetoCompra"),
        "situacao": item.get("situacaoCompraNome"),
        "valor_estimado": item.get("valorTotalEstimado"),
        "valor_homologado": item.get("valorTotalHomologado"),
        "data_publicacao": item.get("dataPublicacaoPncp"),
        "data_abertura": item.get("dataAberturaProposta"),
        "data_encerramento": item.get("dataEncerramentoProposta"),
        "link_sistema_origem": item.get("linkSistemaOrigem"),
        "pncp_url": portal_url(item),
        "vencedores": vencedores,
    }


def _extract_and_audit(paths: list[Path], title: str, ref: str, meta: dict) -> dict:
    docs = []
    for p in paths:
        docs.extend(extract_path(p))
    return audit_documents(docs, title=title, source="pncp", ref=ref, meta=meta)


async def process_compra(job: Job, client: PNCPClient, db: Database, item: dict, params: ScanParams, work_dir: Path) -> None:
    ctrl = item.get("numeroControlePNCP") or "sem-numero"
    label = f"{ctrl} — {(item.get('objetoCompra') or '')[:90]}"
    try:
        keys = compra_keys(item)
        if not keys:
            raise PNCPError("contratação sem CNPJ/ano/sequencial")
        cnpj, ano, seq = keys
        job.stage = f"Listando anexos de {ctrl}"
        job.emit("progress", item=label)
        arquivos = await _cached(db, f"arquivos:{cnpj}/{ano}/{seq}", timedelta(hours=6), lambda: client.list_arquivos(cnpj, ano, seq))
        chosen = select_files(arquivos, params.max_files)
        dest = work_dir / re.sub(r"[^\w\-]", "_", ctrl)
        paths: list[Path] = []
        download_errors: list[dict] = []
        for arq in chosen:
            titulo = arq.get("titulo") or arq.get("tipoDocumentoNome") or "arquivo"
            url = client.arquivo_url(cnpj, ano, seq, arq)
            if not url:
                download_errors.append({"name": titulo, "error": "anexo sem URL"})
                continue
            job.stage = f"Baixando “{titulo}” ({ctrl})"
            job.emit("progress", item=label)
            try:
                paths.append(await client.download(url, dest, suggested_name=titulo))
            except Exception as exc:  # noqa: BLE001 - um anexo com erro não derruba a licitação
                download_errors.append({"name": titulo, "error": f"download falhou: {exc}"[:300]})
        job.stage = f"Buscando vencedores de {ctrl}"
        job.emit("progress", item=label)
        vencedores = await fetch_vencedores(client, cnpj, ano, seq)
        job.stage = f"Auditando {len(paths)} anexo(s) de {ctrl}"
        job.emit("progress", item=label)
        meta = _compra_meta(item, vencedores)
        title = f"{meta['orgao'] or cnpj} — {(meta['objeto'] or '')[:100]}"
        result = await asyncio.to_thread(_extract_and_audit, paths, title, ctrl, meta)
        for err in download_errors:
            result["documents"].append({
                "name": err["name"], "kind": "download", "role": "desconhecido", "error": err["error"],
                "warnings": [], "rows": 0, "blocks": 0,
            })
        if not chosen:
            result["documents"].append({
                "name": "(nenhum anexo publicado)", "kind": "-", "role": "desconhecido", "error": None,
                "warnings": ["a contratação não tem arquivos no PNCP"], "rows": 0, "blocks": 0,
            })
        audit_id = db.save_audit(result)
        job.audit_ids.append(audit_id)
        job.emit("item_done", item=label, audit_id=audit_id, max_severity=result["max_severity"], n_alerts=result["n_alerts"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - isolamento de falha por licitação
        job.failures += 1
        job.emit("item_error", item=label, message=str(exc)[:300])
    finally:
        job.done += 1
        job.emit("progress", item=label)


async def _collect_modalidade(job: Job, client: PNCPClient, params: ScanParams, modalidade: int,
                              compras: list[dict], seen: set[str]) -> None:
    pages = client.iter_contratacoes(
        params.data_inicial, params.data_final, modalidade, uf=params.uf,
        endpoint=params.endpoint, max_pages=params.max_pages,
    )
    async with aclosing(pages):
        async for items in pages:
            for it in items:
                ctrl = it.get("numeroControlePNCP") or str(compra_keys(it))
                if ctrl in seen:
                    continue
                text = f"{it.get('objetoCompra') or ''} {it.get('informacaoComplementar') or ''}"
                if matches_keywords(text, params.keywords):
                    seen.add(ctrl)
                    compras.append(it)
                    if len(compras) >= params.limite:
                        return
            job.stage = f"Buscando licitações no PNCP... {len(compras)} com as palavras-chave"
            job.emit("progress")


async def run_pncp_scan(job: Job, client: PNCPClient, db: Database, params: ScanParams, work_dir: Path) -> None:
    job.stage = "Buscando licitações no PNCP..."
    job.emit("progress")
    compras: list[dict] = []
    seen: set[str] = set()
    for modalidade in params.modalidades:
        await _collect_modalidade(job, client, params, modalidade, compras, seen)
        if len(compras) >= params.limite:
            break
    job.total = len(compras)
    job.stage = f"{job.total} licitação(ões) para auditar" if compras else "Nenhuma licitação encontrada com esses filtros"
    job.emit("progress")
    await asyncio.gather(*(process_compra(job, client, db, it, params, work_dir) for it in compras))


# ---------------------------------------------------------------- pasta monitorada


async def run_inbox_scan(job: Job, db: Database, inbox: Path, force: bool = False) -> None:
    entries = list_inbox_entries(inbox)
    pending = [(p, fp) for p, fp in entries if force or db.inbox_fingerprint(str(p.resolve())) != fp]
    job.total = len(pending)
    job.stage = f"{job.total} item(ns) novo(s) na pasta" if pending else "Nada novo na pasta"
    job.emit("progress")
    for path, fp in pending:
        label = path.name
        job.stage = f"Auditando {label}"
        job.emit("progress", item=label)
        try:
            result = await asyncio.to_thread(audit_inbox_entry, path)
            audit_id = db.save_audit(result)
            db.inbox_mark(str(path.resolve()), fp, audit_id)
            job.audit_ids.append(audit_id)
            job.emit("item_done", item=label, audit_id=audit_id, max_severity=result["max_severity"], n_alerts=result["n_alerts"])
        except Exception as exc:  # noqa: BLE001
            job.failures += 1
            job.emit("item_error", item=label, message=str(exc)[:300])
        finally:
            job.done += 1
            job.emit("progress", item=label)
