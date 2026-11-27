"""Radar de oportunidades: varre o PNCP periodicamente atrás de licitações dos segmentos escolhidos."""

from __future__ import annotations

import asyncio
import re
from contextlib import aclosing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import urlparse

from .database import Database
from .numbers import normalize
from .pipeline import Job, JobManager, fetch_vencedores
from .pncp_client import PNCPClient, compra_keys, portal_url

SEGMENTS: dict[str, tuple[str, re.Pattern]] = {
    "limpeza": ("Limpeza e conservação", re.compile(r"limpeza|conservacao|asseio|higienizac")),
    "vigilancia": ("Vigilância", re.compile(
        r"vigilancia(?! (sanitaria|epidemiologica|em saude|ambiental))|vigilante|seguranca patrimonial")),
    "apoio": ("Recepção, portaria, copeiragem e apoio", re.compile(
        r"recepc|copeir|portaria|porteir|apoio administrativo|auxiliar administrativo|telefonista|motorista|"
        r"brigadista|maqueiro|ascensorista")),
    "terceirizacao": ("Terceirização / mão de obra", re.compile(r"mao de obra|dedicacao exclusiva|terceiriz")),
    "obras": ("Obras e engenharia", re.compile(r"\bobras?\b|construcao|reforma|engenharia|pavimentac")),
}
GOODS = re.compile(
    r"\baquisic\w*|\bcompras? de\b|fornecimento de (bens|materia|produto|equipamento)|materia(l|is) de (limpeza|consumo|expediente)"
)


def is_goods(norm: str) -> bool:
    head = norm[:120]
    if "servic" in head or "mao de obra" in head:
        return False
    return bool(GOODS.search(head))
CLOSED = ("revogad", "anulad", "desert", "fracassad", "suspens", "cancelad")
MAX_AGE_DAYS = 90  # licitação encerrada há mais tempo já não admite recurso: fica fora do radar
STATUS_LABEL = {
    "propostas_abertas": "Recebendo propostas",
    "em_disputa": "Sessão realizada / em julgamento",
    "resultado": "Resultado publicado",
    "encerrada": "Suspensa, revogada ou fracassada",
}


@dataclass
class RadarConfig:
    enabled: bool = True
    interval_hours: float = 6
    days_back: int = 3
    segments: list[str] = field(default_factory=lambda: ["limpeza", "vigilancia", "apoio", "terceirizacao"])
    ufs: list[str] = field(default_factory=list)
    modalidades: list[int] = field(default_factory=lambda: [6, 4])
    max_pages: int = 20
    winner_checks: int = 25

    @classmethod
    def load(cls, db: Database) -> "RadarConfig":
        data = db.setting_get("radar") or {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, db: Database) -> None:
        db.setting_put("radar", asdict(self))


def match_segments(text: str, wanted: list[str]) -> list[str]:
    norm = normalize(text)
    if is_goods(norm):
        return []
    return [k for k in wanted if k in SEGMENTS and SEGMENTS[k][1].search(norm)]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "")[:19])
    except ValueError:
        return None


def infer_status(item: dict, vencedores: list[dict], now: datetime) -> str:
    situacao = normalize(item.get("situacaoCompraNome") or "")
    if any(s in situacao for s in CLOSED):
        return "encerrada"
    if vencedores:
        return "resultado"
    enc = _parse_dt(item.get("dataEncerramentoProposta"))
    if enc and enc > now:
        return "propostas_abertas"
    return "em_disputa"


def refresh_status(opp: dict, now: datetime | None = None) -> dict:
    """O status muda com o tempo (proposta encerrada) mesmo sem nova varredura."""
    if opp.get("_item"):
        opp["status"] = infer_status(opp["_item"], opp.get("vencedores") or [], now or datetime.now())
    opp["status_label"] = STATUS_LABEL.get(opp["status"], opp["status"])
    return opp


def portal_name(link: str | None) -> str:
    link = (link or "").strip()
    if not link:
        return "—"
    host = (urlparse(link if "://" in link else f"http://{link}").hostname or "").lower()
    if not host:
        return "—"
    if "serpro" in host or "comprasnet" in host or "compras.gov" in host:
        return "Compras.gov"
    if "bll" in host:
        return "BLL"
    if "licitacoes-e" in host or "bb.com.br" in host:
        return "Licitações-e (BB)"
    if "bnc" in host:
        return "BNC"
    return host.removeprefix("www.")


def build_opportunity(item: dict, segments: list[str], now: datetime, previous: dict | None = None) -> dict:
    org = item.get("orgaoEntidade") or {}
    unidade = item.get("unidadeOrgao") or {}
    prev = previous or {}
    vencedores = prev.get("vencedores") or []
    numero = item.get("numeroCompra")
    ano = item.get("anoCompra")
    return {
        "id": item.get("numeroControlePNCP"),
        "orgao": org.get("razaoSocial"),
        "orgao_cnpj": org.get("cnpj"),
        "uf": unidade.get("ufSigla"),
        "municipio": unidade.get("municipioNome"),
        "objeto": item.get("objetoCompra"),
        "modalidade": item.get("modalidadeNome"),
        "numero": f"{numero}/{ano}" if numero and ano else None,
        "valor_estimado": item.get("valorTotalEstimado"),
        "valor_homologado": item.get("valorTotalHomologado"),
        "data_abertura": item.get("dataAberturaProposta"),
        "data_encerramento": item.get("dataEncerramentoProposta"),
        "situacao_pncp": item.get("situacaoCompraNome"),
        "status": infer_status(item, vencedores, now),
        "segmentos": sorted(set(segments) | set(prev.get("segmentos") or [])),
        "link_origem": item.get("linkSistemaOrigem"),
        "portal": portal_name(item.get("linkSistemaOrigem")),
        "pncp_url": portal_url(item),
        "vencedores": vencedores,
        "vencedores_checked_at": prev.get("vencedores_checked_at"),
        "audit_ids": prev.get("audit_ids") or [],
        "first_seen": prev.get("first_seen") or now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "_item": item,
    }


async def _collect(job: Job, client: PNCPClient, cfg: RadarConfig, endpoint: str, modalidade: int,
                   found: dict[str, tuple[dict, list[str]]]) -> None:
    today = date.today()
    ini = today - timedelta(days=cfg.days_back)
    fim = today if endpoint == "atualizacao" else today + timedelta(days=60)
    too_old = datetime.now() - timedelta(days=MAX_AGE_DAYS)
    for uf in cfg.ufs or [None]:
        pages = client.iter_contratacoes(ini, fim, modalidade, uf=uf, endpoint=endpoint, max_pages=cfg.max_pages)
        async with aclosing(pages):
            async for items in pages:
                for it in items:
                    text = f"{it.get('objetoCompra') or ''} {it.get('informacaoComplementar') or ''}"
                    segs = match_segments(text, cfg.segments)
                    ctrl = it.get("numeroControlePNCP")
                    enc = _parse_dt(it.get("dataEncerramentoProposta"))
                    if segs and ctrl and not (enc and enc < too_old):
                        found[ctrl] = (it, segs)


async def run_radar_scan(job: Job, client: PNCPClient, db: Database, cfg: RadarConfig) -> None:
    now = datetime.now()
    db.setting_put("radar_last_run", {"at": now.isoformat(timespec="seconds"), "status": "rodando"})
    sources = [(e, m) for m in cfg.modalidades for e in ("proposta", "atualizacao")]
    job.total = len(sources)
    job.stage = "Buscando licitações no PNCP..."
    job.emit("progress")
    found: dict[str, tuple[dict, list[str]]] = {}

    async def collect(endpoint: str, modalidade: int) -> None:
        label = f"{'propostas abertas' if endpoint == 'proposta' else 'atualizadas'} · modalidade {modalidade}"
        try:
            await _collect(job, client, cfg, endpoint, modalidade, found)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - uma consulta com erro não derruba as outras
            job.failures += 1
            job.emit("item_error", item=label, message=str(exc)[:300])
        finally:
            job.done += 1
            job.stage = f"Buscando no PNCP: {len(found)} licitação(ões) dos seus segmentos até agora"
            job.emit("progress", item=label)

    await asyncio.gather(*(collect(e, m) for e, m in sources))

    new = 0
    for ctrl, (item, segs) in found.items():
        previous = db.get_opportunity(ctrl)
        new += previous is None
        db.upsert_opportunity(build_opportunity(item, segs, now, previous))

    stale = now - timedelta(hours=12)
    pending = [
        o for o in db.list_opportunities(status="em_disputa")
        if not o.get("vencedores_checked_at") or datetime.fromisoformat(o["vencedores_checked_at"]) < stale
    ][: cfg.winner_checks]
    job.total += len(pending)
    job.stage = f"{new} nova(s) oportunidade(s); procurando vencedores de {len(pending)} licitação(ões)"
    job.emit("progress")

    async def winners(opp: dict) -> None:
        try:
            keys = compra_keys(opp["_item"]) if opp.get("_item") else None
            if keys:
                vencedores = await fetch_vencedores(client, *keys)
                opp["vencedores"] = vencedores
                opp["vencedores_checked_at"] = datetime.now().isoformat(timespec="seconds")
                opp["status"] = infer_status(opp["_item"], vencedores, datetime.now())
                db.upsert_opportunity(opp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            job.failures += 1
            job.emit("item_error", item=opp["id"], message=str(exc)[:300])
        finally:
            job.done += 1
            job.emit("progress", item=opp["id"])

    await asyncio.gather(*(winners(o) for o in pending))
    db.setting_put("radar_last_run", {
        "at": now.isoformat(timespec="seconds"), "status": "ok", "found": len(found), "new": new, "failures": job.failures,
    })
    job.stage = f"Radar concluído: {len(found)} licitação(ões) dos seus segmentos, {new} nova(s)"


class RadarScheduler:
    """Dispara a varredura do radar no intervalo configurado, sem sobrepor execuções."""

    def __init__(self, db: Database, start_job: Callable[[], Job | None], check_every: float = 60.0):
        self.db = db
        self.start_job = start_job
        self.check_every = check_every
        self._task: asyncio.Task | None = None

    def due(self, now: datetime | None = None) -> bool:
        cfg = RadarConfig.load(self.db)
        if not cfg.enabled:
            return False
        last = (self.db.setting_get("radar_last_run") or {}).get("at")
        if not last:
            return True
        now = now or datetime.now()
        return now - datetime.fromisoformat(last) >= timedelta(hours=cfg.interval_hours)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            if self.due():
                self.start_job()
            await asyncio.sleep(self.check_every)


def start_radar_job(jobs: JobManager, db: Database, client_factory: Callable[[], PNCPClient]) -> Job:
    running = jobs.running("radar")
    if running:
        return running
    cfg = RadarConfig.load(db)

    async def runner(job: Job) -> None:
        async with client_factory() as client:
            await run_radar_scan(job, client, db, cfg)

    return jobs.start("radar", asdict(cfg), runner)
