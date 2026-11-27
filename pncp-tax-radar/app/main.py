"""API e interface web do PNCP Tax Radar (http://127.0.0.1:8000)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from .auditor import audit_paths
from .config import Settings, load_settings
from .database import Database
from .extractor import MAX_FILE_BYTES
from .folder_watcher import InboxWatcher, list_inbox_entries
from .legal import REVIEW_NOTICE
from .models import SEVERITY_RANK
from .pipeline import JobManager, ScanParams, run_inbox_scan, run_pncp_scan
from .pncp_client import MODALIDADES, PNCPClient, safe_filename
from .radar import SEGMENTS, RadarConfig, RadarScheduler, refresh_status, start_radar_job
from .sweep import run_proposal_sweep

STATIC_DIR = Path(__file__).resolve().parent / "static"

UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR",
       "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]
REGIMES = {"auto": "Detectar automaticamente", "simples": "Simples Nacional", "presumido": "Lucro Presumido", "real": "Lucro Real"}
CATEGORIAS = {
    "auto": "Detectar automaticamente",
    "limpeza": "Limpeza e conservação",
    "vigilancia": "Vigilância",
    "obras": "Obras e engenharia",
    "cessao_outros": "Cessão de mão de obra – outros (recepção, copeiragem, portaria, motorista...)",
    "bens": "Fornecimento de bens",
}


class ScanRequest(BaseModel):
    data_inicial: date
    data_final: date
    keywords: list[str] | str = Field(default_factory=list)
    modalidades: list[int] = Field(default_factory=lambda: [6])
    uf: str | None = None
    limite: int = Field(20, ge=1, le=200)
    endpoint: Literal["publicacao", "atualizacao", "proposta"] = "publicacao"
    max_files: int = Field(12, ge=1, le=40)

    @field_validator("keywords", mode="before")
    @classmethod
    def split_keywords(cls, v):
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    @field_validator("uf")
    @classmethod
    def check_uf(cls, v):
        if v in (None, "", "TODAS"):
            return None
        if v.upper() not in UFS:
            raise ValueError("UF inválida")
        return v.upper()

    @field_validator("modalidades")
    @classmethod
    def check_modalidades(cls, v):
        bad = [m for m in v if m not in MODALIDADES]
        if bad or not v:
            raise ValueError(f"modalidade inválida: {bad}")
        return v

    @model_validator(mode="after")
    def check_period(self):
        if self.data_final < self.data_inicial:
            raise ValueError("data final anterior à inicial")
        if (self.data_final - self.data_inicial).days > 365:
            raise ValueError("período máximo de 365 dias")
        return self


class RadarConfigRequest(BaseModel):
    enabled: bool = True
    interval_hours: float = Field(6, ge=1, le=168)
    days_back: int = Field(3, ge=1, le=30)
    segments: list[str] = Field(default_factory=list)
    ufs: list[str] = Field(default_factory=list)
    modalidades: list[int] = Field(default_factory=lambda: [6, 4])
    max_pages: int = Field(20, ge=1, le=100)
    winner_checks: int = Field(25, ge=0, le=200)

    @field_validator("segments")
    @classmethod
    def check_segments(cls, v):
        bad = [s for s in v if s not in SEGMENTS]
        if bad or not v:
            raise ValueError(f"escolha ao menos um segmento válido ({bad})")
        return v

    @field_validator("ufs")
    @classmethod
    def check_ufs(cls, v):
        v = [u.upper() for u in v]
        if any(u not in UFS for u in v):
            raise ValueError("UF inválida")
        return v

    @field_validator("modalidades")
    @classmethod
    def check_modalidades(cls, v):
        if not v or any(m not in MODALIDADES for m in v):
            raise ValueError("modalidade inválida")
        return v


def create_app(settings: Settings | None = None, client_factory: Callable[[], PNCPClient] | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    jobs = JobManager()
    client_factory = client_factory or PNCPClient

    def inbox_has_pending() -> bool:
        return any(db.inbox_fingerprint(str(p.resolve())) != fp for p, fp in list_inbox_entries(settings.inbox_dir))

    def start_inbox_job(force: bool = False):
        running = jobs.running("inbox")
        if running:
            return running
        return jobs.start("inbox", {"force": force}, lambda job: run_inbox_scan(job, db, settings.inbox_dir, force))

    def start_sweep_job():
        running = jobs.running("sweep")
        if running:
            return running
        cfg = RadarConfig.load(db)

        async def runner(job):
            async with client_factory() as client:
                await run_proposal_sweep(job, client, db, cfg, settings.downloads_dir / "varredura")

        return jobs.start("sweep", {"segments": cfg.segments}, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        watcher = InboxWatcher(inbox_has_pending, start_inbox_job, settings.watch_interval) if settings.watch_inbox else None
        if watcher:
            watcher.start()
        scheduler = RadarScheduler(
            db, lambda: start_radar_job(jobs, db, client_factory), start_sweep=start_sweep_job,
        ) if settings.radar_auto else None
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            await scheduler.stop()
        if watcher:
            await watcher.stop()
        for job in list(jobs.jobs.values()):
            jobs.cancel(job.id)
        pending = [j.task for j in jobs.jobs.values() if j.task and not j.task.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)

    app = FastAPI(title="PNCP Tax Radar", lifespan=lifespan)
    app.state.db, app.state.jobs, app.state.settings = db, jobs, settings
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "inbox_dir": str(settings.inbox_dir), "watch_inbox": settings.watch_inbox}

    @app.get("/api/meta")
    async def meta():
        return {
            "ufs": UFS, "modalidades": MODALIDADES, "regimes": REGIMES, "categorias": CATEGORIAS,
            "review_notice": REVIEW_NOTICE, "inbox_dir": str(settings.inbox_dir),
        }

    # ------------------------------------------------------------ auditoria manual

    async def save_uploads(files: list[UploadFile]) -> tuple[Path, list[Path]]:
        folder = settings.uploads_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        folder.mkdir(parents=True)
        paths: list[Path] = []
        for i, up in enumerate(files):
            name = safe_filename(up.filename or f"arquivo_{i}")
            (folder / f"{i:02d}").mkdir()
            target = folder / f"{i:02d}" / name
            size = 0
            with open(target, "wb") as out:
                while chunk := await up.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        out.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(413, f"{up.filename}: maior que {MAX_FILE_BYTES // 2**20} MB")
                    out.write(chunk)
            paths.append(target)
        if not paths:
            raise HTTPException(422, "nenhum arquivo enviado")
        return folder, paths

    def check_overrides(regime: str, categoria: str) -> dict:
        if regime not in REGIMES or categoria not in CATEGORIAS:
            raise HTTPException(422, "regime ou categoria inválidos")
        return {"regime": regime, "categoria": categoria}

    @app.post("/api/audit/upload")
    async def upload(
        files: list[UploadFile] = File(...),
        regime: str = Form("auto"),
        categoria: str = Form("auto"),
        titulo: str = Form(""),
    ):
        overrides = check_overrides(regime, categoria)
        folder, paths = await save_uploads(files)
        result = await asyncio.to_thread(
            audit_paths, paths, overrides, title=titulo.strip() or None, source="upload", ref=folder.name
        )
        result["id"] = db.save_audit(result)
        return result

    # ------------------------------------------------------------ radar de oportunidades

    @app.get("/api/radar")
    async def radar_status():
        cfg = RadarConfig.load(db)
        running = jobs.running("radar")
        return {
            "config": asdict(cfg), "segments": {k: v[0] for k, v in SEGMENTS.items()},
            "last_run": db.setting_get("radar_last_run"), "running_job": running.id if running else None,
            "auto": settings.radar_auto,
        }

    @app.put("/api/radar/config")
    async def radar_config(req: RadarConfigRequest):
        cfg = RadarConfig(**req.model_dump())
        cfg.save(db)
        return asdict(cfg)

    @app.post("/api/radar/scan")
    async def radar_scan():
        return start_radar_job(jobs, db, client_factory).snapshot()

    @app.post("/api/sweep/scan")
    async def sweep_scan():
        return start_sweep_job().snapshot()

    @app.get("/api/sweep")
    async def sweep_status():
        running = jobs.running("sweep")
        return {"last_run": db.setting_get("sweep_last_run"), "running_job": running.id if running else None}

    @app.get("/api/opportunities")
    async def opportunities(status: str | None = None, segmento: str | None = None, uf: str | None = None):
        now = datetime.now()
        out = []
        for opp in db.list_opportunities():
            refresh_status(opp, now)
            if status and opp["status"] != status:
                continue
            if segmento and segmento not in opp.get("segmentos", []):
                continue
            if uf and opp.get("uf") != uf:
                continue
            opp.pop("_item", None)
            out.append(opp)
        order = {"propostas_abertas": 0, "em_disputa": 1, "resultado": 2, "encerrada": 3}
        out.sort(key=lambda o: (order.get(o["status"], 9), o.get("data_encerramento") or ""))
        return out

    @app.post("/api/opportunities/{opp_id:path}/audit")
    async def opportunity_audit(
        opp_id: str,
        files: list[UploadFile] = File(...),
        regime: str = Form("auto"),
        categoria: str = Form("auto"),
    ):
        opp = db.get_opportunity(opp_id)
        if not opp:
            raise HTTPException(404, "oportunidade não encontrada")
        overrides = check_overrides(regime, categoria)
        folder, paths = await save_uploads(files)
        winner = (opp.get("vencedores") or [{}])[0]
        defaults = {"orgao": opp.get("orgao"), "pregao": opp.get("numero"),
                    "licitante": winner.get("fornecedor"), "cnpj": winner.get("ni")}
        meta = {k: v for k, v in opp.items() if k not in ("_item", "audit_ids")}
        title = f"{opp.get('orgao') or ''} — {(opp.get('objeto') or '')[:100]}"
        result = await asyncio.to_thread(
            audit_paths, paths, overrides, title=title, source="upload", ref=folder.name, meta=meta, defaults=defaults,
        )
        result["opportunity_id"] = opp_id
        result["id"] = db.save_audit(result)
        db.add_opportunity_audit(opp_id, result["id"])
        return result

    # ------------------------------------------------------------ pasta monitorada

    @app.get("/api/inbox")
    async def inbox():
        entries = await asyncio.to_thread(list_inbox_entries, settings.inbox_dir, 0)
        seen = {e["path"]: e for e in db.inbox_entries()}
        out = []
        for path, fp in entries:
            s = seen.get(str(path.resolve()))
            out.append({
                "name": path.name, "is_dir": path.is_dir(),
                "status": "auditado" if s and s["fingerprint"] == fp else ("alterado" if s else "novo"),
                "audit_id": s["audit_id"] if s else None, "processed_at": s["processed_at"] if s else None,
            })
        return {"dir": str(settings.inbox_dir), "watch": settings.watch_inbox, "entries": out}

    @app.post("/api/inbox/scan")
    async def inbox_scan(force: bool = False):
        return start_inbox_job(force).snapshot()

    # ------------------------------------------------------------ varredura PNCP

    @app.post("/api/pncp/scan")
    async def pncp_scan(req: ScanRequest):
        params = ScanParams(
            data_inicial=req.data_inicial, data_final=req.data_final, keywords=req.keywords,
            modalidades=req.modalidades, uf=req.uf, limite=req.limite, endpoint=req.endpoint, max_files=req.max_files,
        )

        async def runner(job):
            async with client_factory() as client:
                await run_pncp_scan(job, client, db, params, settings.downloads_dir)

        job = jobs.start("pncp", req.model_dump(mode="json"), runner)
        return job.snapshot()

    # ------------------------------------------------------------ jobs

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job não encontrado")
        return job.snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(job_id: str):
        if not jobs.get(job_id):
            raise HTTPException(404, "job não encontrado")
        return {"cancelled": jobs.cancel(job_id)}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, start: int = Query(0, alias="from", ge=0)):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job não encontrado")
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isdigit():
            start = int(last_id) + 1

        async def gen():
            async for ev in job.stream(start):
                if await request.is_disconnected():
                    break
                if ev["type"] == "ping":
                    yield ": ping\n\n"
                    continue
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ resultados

    @app.get("/api/audits")
    async def audits(min_severity: str | None = None, source: str | None = None):
        return db.list_audits(min_severity=min_severity, source=source)

    @app.get("/api/audits/{audit_id}")
    async def audit_detail(audit_id: int):
        data = db.get_audit(audit_id)
        if not data:
            raise HTTPException(404, "auditoria não encontrada")
        return data

    @app.delete("/api/audits/{audit_id}")
    async def audit_delete(audit_id: int):
        if not db.delete_audit(audit_id):
            raise HTTPException(404, "auditoria não encontrada")
        return {"deleted": audit_id}

    @app.get("/api/export")
    async def export(format: Literal["csv", "json"] = "csv", min_severity: str | None = None, source: str | None = None):
        rows = []
        threshold = SEVERITY_RANK.get(min_severity or "", 0)
        for audit in db.list_audits(source=source):
            for a in audit.get("alerts", []):
                if threshold and SEVERITY_RANK.get(a["severity"], 0) < threshold:
                    continue
                rows.append({
                    "auditoria_id": audit["id"], "caso": audit.get("title"), "fonte": audit.get("source"),
                    "referencia": audit.get("ref"), "link_pncp": audit.get("pncp_url") or "",
                    "regra": a["rule_id"], "severidade": a["severity"], "veredito": a["verdict"], "achado": a["title"],
                    "posto": a["block"], "documento": a["doc"], "local": a["location"], "na_planilha": a["found"],
                    "correto": a["expected"], "impacto_rs": a["impact"], "lucro_rs": a["lucro"], "preco_rs": a["preco"],
                    "evidencia": a["evidence"], "base_legal": a["legal_basis"], "solucao": a["actionable_solution"],
                })
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        if format == "json":
            return JSONResponse(rows, headers={"Content-Disposition": f'attachment; filename="achados-{stamp}.json"'})
        buf = io.StringIO()
        fields = list(rows[0].keys()) if rows else ["auditoria_id", "caso", "regra", "severidade", "achado"]
        writer = csv.DictWriter(buf, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if v is None else str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="achados-{stamp}.csv"'})

    return app
