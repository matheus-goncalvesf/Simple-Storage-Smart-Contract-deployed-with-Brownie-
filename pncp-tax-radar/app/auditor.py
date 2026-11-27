"""Orquestra um caso: documentos → blocos de planilha → contexto → regras → resultado serializável."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .draft import build_recurso, build_tese
from .extractor import extract_path
from .models import SEVERITY_RANK, VERDICT_RANK, Document
from .rules_engine import audit_blocks
from .sheet_parser import detect_context, doc_role, parse_blocks


def audit_documents(
    documents: list[Document],
    overrides: dict | None = None,
    *,
    title: str | None = None,
    source: str = "upload",
    ref: str | None = None,
    meta: dict | None = None,
    defaults: dict | None = None,
) -> dict:
    blocks_by_doc = {d.name: parse_blocks(d) for d in documents if not d.error}
    blocks = [b for bs in blocks_by_doc.values() for b in bs]
    ctx = detect_context(documents, blocks, overrides)
    for key, value in (defaults or {}).items():
        if value and not getattr(ctx, key, None):
            setattr(ctx, key, value)
    alerts, summaries = audit_blocks(blocks, ctx)

    alert_dicts = []
    for i, a in enumerate(alerts):
        d = a.to_dict()
        d["id"] = i
        d["tese"] = build_tese(a)
        alert_dicts.append(d)

    counts = {"ALTA": 0, "MEDIA": 0, "INFO": 0}
    for a in alerts:
        counts[a.severity] += 1
    max_sev = max((a.severity for a in alerts), key=lambda s: SEVERITY_RANK[s], default=None)
    max_verdict = max((a.verdict for a in alerts), key=lambda v: VERDICT_RANK[v], default=None)
    max_impact = max((a.impact for a in alerts if a.impact is not None and a.severity != "INFO"), default=None)

    meta = meta or {}
    return {
        "title": title or _default_title(documents, ctx),
        "source": source,
        "ref": ref,
        "pncp_url": meta.get("pncp_url"),
        "meta": meta,
        "context": ctx.to_dict(),
        "documents": [
            {
                "name": d.name,
                "kind": d.kind,
                "role": doc_role(d.name),
                "error": d.error,
                "warnings": d.warnings,
                "rows": len(d.rows),
                "blocks": len(blocks_by_doc.get(d.name, [])),
            }
            for d in documents
        ],
        "blocks": summaries,
        "alerts": alert_dicts,
        "counts": counts,
        "n_alerts": len(alerts),
        "max_severity": max_sev,
        "max_verdict": max_verdict,
        "max_impact": max_impact,
        "recurso": build_recurso(ctx, alerts),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def audit_paths(paths: list[Path], overrides: dict | None = None, **kwargs) -> dict:
    documents: list[Document] = []
    for p in paths:
        documents.extend(extract_path(p))
    return audit_documents(documents, overrides, **kwargs)


def _default_title(documents: list[Document], ctx) -> str:
    parts = []
    if ctx.pregao:
        parts.append(f"Pregão {ctx.pregao}")
    if ctx.licitante:
        parts.append(ctx.licitante)
    if parts:
        return " — ".join(parts)
    names = [d.name for d in documents if "›" not in d.name] or [d.name for d in documents]
    return ", ".join(dict.fromkeys(n.split(" › ")[0] for n in names))[:160] or "Caso sem nome"
