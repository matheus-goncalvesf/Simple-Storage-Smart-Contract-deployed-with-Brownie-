"""Varredura automática: contratos de terceirização no PNCP cuja proposta ou planilha da contratada foi publicada.

Lê milhares de contratos por dia, filtra pelos segmentos do radar, baixa só anexos com cara de proposta/planilha
da empresa (descarta edital, termo de referência, modelos), audita e guarda apenas o que tem planilha de custos.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .auditor import audit_documents
from .database import Database
from .extractor import extract_path
from .numbers import normalize
from .pipeline import Job
from .pncp_client import PNCPClient
from .radar import RadarConfig, match_segments

COMPANY_DOC = re.compile(r"propost|planilha|custo|composi|formac|preco")
ORGAO_DOC = re.compile(r"edital|termo de refer|\btr\b|minuta|modelo|parecer|aviso|declarac|estimativ|estudo|\betp\b|"
                       r"mapa de risco|quantitativ|relatorio de propostas|ata de|em branco|orcamento estimado")
CTRL = re.compile(r"(\d{14})-\d-(\d+)/(\d{4})")
MAX_FILES = 6


def pick_company_files(arquivos: list[dict], from_contract: bool) -> list[dict]:
    """No contrato, proposta ou planilha anexada é da contratada; na licitação, só arquivo com "proposta" no nome."""
    out = []
    for a in arquivos:
        t = normalize(f"{a.get('titulo') or ''} {a.get('tipoDocumentoNome') or ''}".replace("_", " "))
        wanted = COMPANY_DOC.search(t) if from_contract else re.search(r"propost", t)
        if a.get("statusAtivo", True) and wanted and not ORGAO_DOC.search(t):
            out.append(a)
    return out[:MAX_FILES]


def _audit(paths: list[Path], title: str, ref: str, meta: dict, defaults: dict) -> dict:
    docs = [d for p in paths for d in extract_path(p)]
    return audit_documents(docs, title=title, source="pncp", ref=ref, meta=meta, defaults=defaults)


async def run_proposal_sweep(job: Job, client: PNCPClient, db: Database, cfg: RadarConfig, work_dir: Path,
                             days_back: int = 30, max_pages: int = 20) -> None:
    started = datetime.now()
    db.setting_put("sweep_last_run", {"at": started.isoformat(timespec="seconds"), "status": "rodando"})
    seen = set(db.setting_get("sweep_seen") or [])
    fim = date.today()
    ini = fim - timedelta(days=days_back)
    contratos: list[dict] = []
    scanned = 0
    job.stage = "Lendo contratos publicados no PNCP..."
    job.emit("progress")
    for start in range(1, max_pages + 1, 4):
        pages = await asyncio.gather(*(client.list_contratos(ini, fim, p) for p in range(start, min(start + 4, max_pages + 1))),
                                     return_exceptions=True)
        stop = False
        for page in pages:
            if isinstance(page, Exception):
                job.failures += 1
                job.emit("item_error", item="página de contratos", message=str(page)[:200])
                continue
            items = page.get("data") or []
            scanned += len(items)
            stop = stop or not items
            for c in items:
                ctrl = c.get("numeroControlePNCP")
                if ctrl and ctrl not in seen and match_segments(c.get("objetoContrato") or "", cfg.segments):
                    contratos.append(c)
        job.stage = f"{scanned} contratos lidos, {len(contratos)} novos de terceirização"
        job.emit("progress")
        if stop:
            break
    job.total = len(contratos)
    found = 0

    async def process(c: dict) -> None:
        nonlocal found
        ctrl = c["numeroControlePNCP"]
        cnpj = (c.get("orgaoEntidade") or {}).get("cnpj")
        try:
            files = pick_company_files(
                await client.list_arquivos_contrato(cnpj, c["anoContrato"], c["sequencialContrato"]), from_contract=True)
            if m := CTRL.match(c.get("numeroControlePncpCompra") or ""):
                files += pick_company_files(
                    await client.list_arquivos(m.group(1), int(m.group(3)), int(m.group(2))), from_contract=False)
            files = files[:MAX_FILES]
            paths = []
            for a in files:
                url = a.get("url") or a.get("uri")
                if url:
                    try:
                        paths.append(await client.download(url, work_dir / re.sub(r"\W", "_", ctrl),
                                                           suggested_name=a.get("titulo"), max_bytes=25 * 2**20))
                    except Exception:  # noqa: BLE001 - um anexo com erro não para a varredura
                        pass
            if paths:
                org = c.get("orgaoEntidade") or {}
                meta = {
                    "orgao": org.get("razaoSocial"), "objeto": c.get("objetoContrato"), "valor_global": c.get("valorGlobal"),
                    "fornecedor": c.get("nomeRazaoSocialFornecedor"), "ni_fornecedor": c.get("niFornecedor"),
                    "data_assinatura": c.get("dataAssinatura"), "numero_controle": ctrl,
                    "compra": c.get("numeroControlePncpCompra"),
                    "pncp_url": f"https://pncp.gov.br/app/contratos/{cnpj}/{c['anoContrato']}/{c['sequencialContrato']}",
                }
                defaults = {"orgao": org.get("razaoSocial"), "licitante": c.get("nomeRazaoSocialFornecedor"),
                            "cnpj": c.get("niFornecedor")}
                title = f"{c.get('nomeRazaoSocialFornecedor') or 'Contratada'} — {(c.get('objetoContrato') or '')[:90]}"
                result = await asyncio.to_thread(_audit, paths, title, ctrl, meta, defaults)
                if result["blocks"]:
                    found += 1
                    audit_id = db.save_audit(result)
                    job.audit_ids.append(audit_id)
                    job.emit("item_done", item=title, audit_id=audit_id, max_severity=result["max_severity"],
                             n_alerts=result["n_alerts"])
            seen.add(ctrl)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            job.failures += 1
            job.emit("item_error", item=ctrl, message=str(exc)[:200])
        finally:
            job.done += 1
            job.stage = f"Contrato {job.done}/{job.total}: {found} planilha(s) de contratada auditada(s)"
            job.emit("progress", item=ctrl)

    await asyncio.gather(*(process(c) for c in contratos))
    db.setting_put("sweep_seen", sorted(seen)[-50000:])
    db.setting_put("sweep_last_run", {"at": started.isoformat(timespec="seconds"), "status": "ok", "contratos": scanned,
                                      "terceirizacao": len(contratos), "planilhas": found, "failures": job.failures})
    job.stage = f"Varredura concluída: {scanned} contratos lidos, {len(contratos)} de terceirização, {found} planilha(s) auditada(s)"
