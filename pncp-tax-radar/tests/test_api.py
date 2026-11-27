import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_pipeline import FakeClient


@pytest.fixture
def api(fx, tmp_path):
    settings = Settings(data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox", watch_inbox=False, radar_auto=False)
    app = create_app(settings, client_factory=lambda: FakeClient(fx))
    with TestClient(app) as client:
        yield client, settings


def test_index_and_meta(api):
    client, _ = api
    r = client.get("/")
    assert r.status_code == 200 and "PNCP Tax Radar" in r.text
    meta = client.get("/api/meta").json()
    assert "SP" in meta["ufs"] and meta["modalidades"]["6"] == "Pregão - Eletrônico"


def test_upload_audit_list_export_delete(api, fx):
    client, _ = api
    with open(fx["limpeza_simples_sem_cpp"], "rb") as f:
        r = client.post("/api/audit/upload", files=[("files", ("proposta.xlsx", f))], data={"regime": "auto", "categoria": "auto"})
    assert r.status_code == 200, r.text
    audit = r.json()
    assert audit["max_severity"] == "ALTA"
    assert audit["alerts"][0]["rule_id"] == "R05"
    assert audit["documents"][0]["name"] == "proposta.xlsx"
    assert audit["alerts"][0]["location"].startswith("proposta.xlsx › Posto 1 › linha")

    listed = client.get("/api/audits", params={"min_severity": "ALTA"}).json()
    assert [a["id"] for a in listed] == [audit["id"]]
    assert client.get(f"/api/audits/{audit['id']}").json()["recurso"].startswith("RASCUNHO")

    csv_resp = client.get("/api/export", params={"format": "csv"})
    assert csv_resp.text.startswith("﻿auditoria_id;caso;")
    assert "R05" in csv_resp.text
    rows = client.get("/api/export", params={"format": "json", "min_severity": "ALTA"}).json()
    assert rows and rows[0]["regra"] == "R05"

    assert client.delete(f"/api/audits/{audit['id']}").status_code == 200
    assert client.get(f"/api/audits/{audit['id']}").status_code == 404


def test_upload_with_manual_override(api, fx):
    client, _ = api
    with open(fx["limpeza_simples_sem_cpp"], "rb") as f:
        r = client.post("/api/audit/upload", files=[("files", ("p.xlsx", f))], data={"categoria": "cessao_outros"})
    assert [a["rule_id"] for a in r.json()["alerts"]] == ["R06"]


def test_upload_rejects_invalid_regime(api, fx):
    client, _ = api
    with open(fx["limpeza_simples_sem_cpp"], "rb") as f:
        r = client.post("/api/audit/upload", files=[("files", ("p.xlsx", f))], data={"regime": "xyz"})
    assert r.status_code == 422


def test_pncp_scan_streams_progress_over_sse(api):
    client, _ = api
    r = client.post("/api/pncp/scan", json={"data_inicial": "2026-01-01", "data_final": "2026-01-31", "keywords": "limpeza"})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    events = []
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert events[-1]["type"] == "done"
    assert any("Baixando" in e["stage"] for e in events)
    status = client.get(f"/api/jobs/{job_id}").json()
    assert (status["total"], status["failures"]) == (2, 1)
    assert len(client.get("/api/audits", params={"source": "pncp"}).json()) == 1


def test_pncp_scan_validates_period(api):
    client, _ = api
    r = client.post("/api/pncp/scan", json={"data_inicial": "2026-02-01", "data_final": "2026-01-01"})
    assert r.status_code == 422


def test_inbox_scan(api, fx):
    client, settings = api
    target = shutil.copy(fx["recepcao_simples"], settings.inbox_dir / "recepcao.xlsx")
    old = time.time() - 60
    os.utime(target, (old, old))
    assert client.get("/api/inbox").json()["entries"][0]["status"] == "novo"
    job = client.post("/api/inbox/scan").json()
    for _ in range(100):
        status = client.get(f"/api/jobs/{job['id']}").json()
        if status["status"] != "running":
            break
        time.sleep(0.05)
    assert status["status"] == "done" and status["total"] == 1
    entry = client.get("/api/inbox").json()["entries"][0]
    assert entry["status"] == "auditado" and entry["audit_id"]
    audit = client.get(f"/api/audits/{entry['audit_id']}").json()
    assert audit["alerts"][0]["rule_id"] == "R06"
