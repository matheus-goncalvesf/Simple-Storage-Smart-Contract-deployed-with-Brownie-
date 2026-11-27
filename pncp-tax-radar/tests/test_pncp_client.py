import asyncio
from datetime import date

import httpx
import pytest

from app.pncp_client import PNCPClient, PNCPError, compra_keys, matches_keywords, portal_url, sniff_extension


def make_client(handler, **kwargs):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    client = PNCPClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=fake_sleep, **kwargs)
    return client, sleeps


def run(coro):
    return asyncio.run(coro)


def test_search_sends_expected_params():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [], "totalPaginas": 0})

    client, _ = make_client(handler)
    run(client.search_contratacoes(date(2026, 1, 1), date(2026, 1, 31), 6, uf="sp"))
    assert seen["path"] == "/api/consulta/v1/contratacoes/publicacao"
    assert seen["params"] == {
        "dataInicial": "20260101", "dataFinal": "20260131", "codigoModalidadeContratacao": "6",
        "pagina": "1", "tamanhoPagina": "50", "uf": "SP",
    }


def test_retry_after_429_is_respected():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"data": [{"numeroControlePNCP": "x"}], "totalPaginas": 1})

    client, sleeps = make_client(handler)
    data = run(client.search_contratacoes(date(2026, 1, 1), date(2026, 1, 2), 6))
    assert data["data"][0]["numeroControlePNCP"] == "x"
    assert calls["n"] == 3 and sleeps == [2.0, 2.0]


def test_exponential_backoff_then_gives_up_on_5xx():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    client, sleeps = make_client(handler, max_retries=3)
    with pytest.raises(PNCPError, match="4 tentativas"):
        run(client.search_contratacoes(date(2026, 1, 1), date(2026, 1, 2), 6))
    assert calls["n"] == 4 and sleeps == [1.0, 2.0, 4.0]


def test_timeouts_are_retried_and_reported():
    def handler(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    client, sleeps = make_client(handler, max_retries=2)
    with pytest.raises(PNCPError, match="ConnectTimeout"):
        run(client.list_arquivos("00000000000191", 2026, 1))
    assert len(sleeps) == 2


def test_client_errors_are_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="parâmetro inválido")

    client, sleeps = make_client(handler)
    with pytest.raises(PNCPError, match="HTTP 400"):
        run(client.search_contratacoes(date(2026, 1, 1), date(2026, 1, 2), 6))
    assert calls["n"] == 1 and sleeps == []


def test_204_means_empty_page():
    client, _ = make_client(lambda request: httpx.Response(204))
    assert run(client.search_contratacoes(date(2026, 1, 1), date(2026, 1, 2), 6))["data"] == []


def test_pagination_follows_total_pages():
    def handler(request):
        page = int(request.url.params["pagina"])
        return httpx.Response(200, json={"data": [{"p": page}], "totalPaginas": 2, "paginasRestantes": 2 - page})

    client, _ = make_client(handler)

    async def collect():
        return [items async for items in client.iter_contratacoes(date(2026, 1, 1), date(2026, 1, 2), 6)]

    assert run(collect()) == [[{"p": 1}], [{"p": 2}]]


def test_download_uses_header_name_and_sniffs_extension(tmp_path):
    def handler(request):
        if request.url.path.endswith("/1"):
            return httpx.Response(200, content=b"PK\x03\x04....xl/workbook.xml",
                                  headers={"Content-Disposition": 'attachment; filename="Planilha de custos.xlsx"'})
        return httpx.Response(200, content=b"%PDF-1.4 conteudo")

    client, _ = make_client(handler)
    p1 = run(client.download("https://pncp.gov.br/x/1", tmp_path))
    p2 = run(client.download("https://pncp.gov.br/x/2", tmp_path, suggested_name="Edital"))
    assert p1.name == "Planilha de custos.xlsx"
    assert p2.name == "Edital.pdf" and p2.read_bytes().startswith(b"%PDF")


def test_download_size_limit(tmp_path):
    client, _ = make_client(lambda request: httpx.Response(200, content=b"x" * 100))
    with pytest.raises(PNCPError, match="maior que"):
        run(client.download("https://pncp.gov.br/x", tmp_path, max_bytes=10))


def test_download_name_cannot_escape_folder(tmp_path):
    client, _ = make_client(lambda request: httpx.Response(
        200, content=b"%PDF", headers={"Content-Disposition": 'attachment; filename="../../etc/passwd.pdf"'}))
    p = run(client.download("https://pncp.gov.br/x", tmp_path))
    assert p.parent == tmp_path and p.name == "passwd.pdf"


def test_helpers():
    item = {"numeroControlePNCP": "00394460005887-1-000123/2024"}
    assert compra_keys(item) == ("00394460005887", 2024, 123)
    assert portal_url(item) == "https://pncp.gov.br/app/editais/00394460005887/2024/123"
    assert compra_keys({"orgaoEntidade": {"cnpj": "1"}, "anoCompra": 2026, "sequencialCompra": 7}) == ("1", 2026, 7)
    assert matches_keywords("Serviços de VIGILÂNCIA armada", ["vigilancia"])
    assert not matches_keywords("Aquisição de computadores", ["limpeza", "vigilância"])
    assert sniff_extension(b"\xd0\xcf\x11\xe0rest") == ".xls"
    assert sniff_extension(b"Rar!\x1a") == ".rar"
