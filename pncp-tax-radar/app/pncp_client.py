"""Cliente assíncrono e resiliente da API pública do PNCP.

ATENÇÃO: os caminhos e nomes de campo abaixo seguem a documentação pública do PNCP conhecida até o
desenvolvimento, mas NÃO foram validados contra a API real (o ambiente de desenvolvimento não tinha
acesso a pncp.gov.br). Rode `python tools/pncp_probe.py` na sua máquina para conferir e ajuste
ENDPOINTS se algo tiver mudado. Os campos são lidos de forma defensiva (dict.get).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import unquote

import httpx

CONSULTA_BASE = "https://pncp.gov.br/api/consulta"
PNCP_BASE = "https://pncp.gov.br/api/pncp"
PORTAL_BASE = "https://pncp.gov.br/app/editais"

ENDPOINTS = {
    "publicacao": "/v1/contratacoes/publicacao",  # contratações publicadas no período
    "atualizacao": "/v1/contratacoes/atualizacao",  # contratações alteradas no período (ex.: resultado)
    "proposta": "/v1/contratacoes/proposta",  # recebendo propostas até dataFinal
    "arquivos": "/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos",
    "itens": "/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens",
    "resultados": "/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens/{numeroItem}/resultados",
}

MODALIDADES = {
    1: "Leilão - Eletrônico",
    2: "Diálogo Competitivo",
    3: "Concurso",
    4: "Concorrência - Eletrônica",
    5: "Concorrência - Presencial",
    6: "Pregão - Eletrônico",
    7: "Pregão - Presencial",
    8: "Dispensa de Licitação",
    9: "Inexigibilidade",
    10: "Manifestação de Interesse",
    11: "Pré-qualificação",
    12: "Credenciamento",
    13: "Leilão - Presencial",
}

RETRY_STATUS = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class PNCPError(Exception):
    pass


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def matches_keywords(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    norm = _norm(text)
    return any(_norm(k).strip() and _norm(k).strip() in norm for k in keywords)


def compra_keys(item: dict) -> tuple[str, int, int] | None:
    """(cnpj, ano, sequencial) de uma contratação, lendo os campos conhecidos ou o numeroControlePNCP."""
    org = item.get("orgaoEntidade") or {}
    cnpj = org.get("cnpj") or item.get("cnpj")
    ano = item.get("anoCompra")
    seq = item.get("sequencialCompra")
    if cnpj and ano and seq:
        return str(cnpj), int(ano), int(seq)
    ctrl = item.get("numeroControlePNCP") or ""
    m = re.match(r"(\d{14})-\d-(\d+)/(\d{4})", ctrl)
    if m:
        return m.group(1), int(m.group(3)), int(m.group(2))
    return None


def portal_url(item: dict) -> str | None:
    keys = compra_keys(item)
    if not keys:
        return None
    cnpj, ano, seq = keys
    return f"{PORTAL_BASE}/{cnpj}/{ano}/{seq}"


def sniff_extension(data: bytes, fallback: str = ".bin") -> str:
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK"):
        head = data[:4096]
        if b"xl/" in head:
            return ".xlsx"
        if b"word/" in head:
            return ".docx"
        return ".zip"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return ".xls" if fallback not in (".doc",) else ".doc"
    if data.startswith(b"Rar!"):
        return ".rar"
    if data.startswith(b"7z\xbc\xaf"):
        return ".7z"
    return fallback


def _filename_from_headers(headers: httpx.Headers) -> str | None:
    cd = headers.get("content-disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else None


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.\- ()]", "_", name).strip(" .")
    return name[:150] or "arquivo"


class PNCPClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        concurrency: int = 4,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        consulta_base: str = CONSULTA_BASE,
        pncp_base: str = PNCP_BASE,
    ):
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "pncp-tax-radar/0.1", "Accept": "application/json, */*"},
        )
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self.consulta_base = consulta_base.rstrip("/")
        self.pncp_base = pncp_base.rstrip("/")

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # ------------------------------------------------------------ núcleo com retry

    async def _with_retry(self, label: str, op: Callable[[], Awaitable[Any]]) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self.semaphore:
                    return await op()
            except _Retryable as exc:
                last, retry_after = exc, exc.retry_after
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last, retry_after = exc, None
            if attempt == self.max_retries:
                break
            delay = retry_after if retry_after is not None else self.backoff_base * (2**attempt)
            await self._sleep(min(delay, 60.0))
        raise PNCPError(f"{label}: falhou após {self.max_retries + 1} tentativas ({type(last).__name__}: {last})")

    async def get_json(self, url: str, params: dict | None = None) -> Any:
        async def op():
            resp = await self._client.get(url, params=params)
            if resp.status_code in RETRY_STATUS:
                ra = resp.headers.get("retry-after")
                raise _Retryable(f"HTTP {resp.status_code}", float(ra) if ra and ra.isdigit() else None)
            if resp.status_code == 204 or not resp.content:
                return None
            if resp.status_code >= 400:
                raise PNCPError(f"GET {url}: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json()

        return await self._with_retry(f"GET {url}", op)

    async def download(self, url: str, dest_dir: Path, *, suggested_name: str | None = None, max_bytes: int = 60 * 2**20) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)

        async def op():
            async with self._client.stream("GET", url) as resp:
                if resp.status_code in RETRY_STATUS:
                    raise _Retryable(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise PNCPError(f"download {url}: HTTP {resp.status_code}")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise PNCPError(f"download {url}: maior que {max_bytes // 2**20} MB")
                    chunks.append(chunk)
                return b"".join(chunks), resp.headers

        data, headers = await self._with_retry(f"download {url}", op)
        name = _filename_from_headers(headers) or suggested_name or "arquivo"
        name = safe_filename(name)
        ext = Path(name).suffix.lower()
        sniffed = sniff_extension(data, ext or ".bin")
        if not ext or (ext not in (".xlsx", ".xls", ".pdf", ".docx", ".zip", ".csv", ".txt", ".rar", ".7z", ".doc", ".xlsm") and sniffed != ".bin"):
            name = f"{Path(name).stem}{sniffed}"
        path = dest_dir / name
        i = 1
        while path.exists():
            path = dest_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        path.write_bytes(data)
        return path

    # ------------------------------------------------------------ endpoints

    async def search_contratacoes(
        self,
        data_inicial: date,
        data_final: date,
        modalidade: int,
        *,
        uf: str | None = None,
        pagina: int = 1,
        tamanho: int = 50,
        endpoint: str = "publicacao",
    ) -> dict:
        params: dict[str, Any] = {
            "dataFinal": data_final.strftime("%Y%m%d"),
            "codigoModalidadeContratacao": modalidade,
            "pagina": pagina,
            "tamanhoPagina": tamanho,
        }
        if endpoint != "proposta":
            params["dataInicial"] = data_inicial.strftime("%Y%m%d")
        if uf:
            params["uf"] = uf.upper()
        data = await self.get_json(self.consulta_base + ENDPOINTS[endpoint], params)
        return data or {"data": [], "totalPaginas": 0, "paginasRestantes": 0}

    async def iter_contratacoes(
        self,
        data_inicial: date,
        data_final: date,
        modalidade: int,
        *,
        uf: str | None = None,
        endpoint: str = "publicacao",
        max_pages: int = 20,
        tamanho: int = 50,
    ) -> AsyncIterator[list[dict]]:
        for pagina in range(1, max_pages + 1):
            page = await self.search_contratacoes(
                data_inicial, data_final, modalidade, uf=uf, pagina=pagina, tamanho=tamanho, endpoint=endpoint
            )
            items = page.get("data") or []
            yield items
            total = page.get("totalPaginas")
            restantes = page.get("paginasRestantes")
            if not items or (restantes is not None and restantes <= 0) or (total is not None and pagina >= total):
                return

    async def list_arquivos(self, cnpj: str, ano: int, sequencial: int) -> list[dict]:
        url = self.pncp_base + ENDPOINTS["arquivos"].format(cnpj=cnpj, ano=ano, sequencial=sequencial)
        data = await self.get_json(url)
        return data if isinstance(data, list) else (data or {}).get("data", []) if isinstance(data, dict) else []

    def arquivo_url(self, cnpj: str, ano: int, sequencial: int, arquivo: dict) -> str | None:
        url = arquivo.get("url") or arquivo.get("uri")
        if url:
            return url
        seq_doc = arquivo.get("sequencialDocumento")
        if seq_doc is None:
            return None
        return self.pncp_base + ENDPOINTS["arquivos"].format(cnpj=cnpj, ano=ano, sequencial=sequencial) + f"/{seq_doc}"

    async def list_itens(self, cnpj: str, ano: int, sequencial: int) -> list[dict]:
        url = self.pncp_base + ENDPOINTS["itens"].format(cnpj=cnpj, ano=ano, sequencial=sequencial)
        data = await self.get_json(url, {"pagina": 1, "tamanhoPagina": 100})
        return data if isinstance(data, list) else (data or {}).get("data", []) if isinstance(data, dict) else []

    async def list_resultados(self, cnpj: str, ano: int, sequencial: int, item: int) -> list[dict]:
        url = self.pncp_base + ENDPOINTS["resultados"].format(cnpj=cnpj, ano=ano, sequencial=sequencial, numeroItem=item)
        data = await self.get_json(url)
        return data if isinstance(data, list) else []
