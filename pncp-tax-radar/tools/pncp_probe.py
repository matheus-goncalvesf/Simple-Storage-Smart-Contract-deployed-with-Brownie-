"""Valida, contra a API real do PNCP, os endpoints e campos que o robô usa.

Uso (na sua máquina, com internet):  python tools/pncp_probe.py [--dias 3] [--modalidade 6]
Gera data/pncp_probe_report.json e imprime OK / DIVERGENTE para cada verificação.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pncp_client import CONSULTA_BASE, ENDPOINTS, PNCP_BASE, compra_keys  # noqa: E402

EXPECTED_ITEM_FIELDS = [
    "numeroControlePNCP", "anoCompra", "sequencialCompra", "objetoCompra", "modalidadeNome",
    "orgaoEntidade.cnpj", "orgaoEntidade.razaoSocial", "unidadeOrgao.ufSigla", "valorTotalEstimado",
    "dataPublicacaoPncp", "dataAberturaProposta", "situacaoCompraNome",
]
EXPECTED_PAGE_FIELDS = ["data", "totalPaginas", "paginasRestantes"]
EXPECTED_ARQUIVO_FIELDS = ["sequencialDocumento", "titulo", "url"]
EXPECTED_RESULTADO_FIELDS = ["nomeRazaoSocialFornecedor", "niFornecedor", "valorTotalHomologado"]

report: dict = {"checks": []}


def check(name: str, ok: bool, detail: str = "") -> None:
    report["checks"].append({"check": name, "ok": ok, "detail": detail})
    print(f"[{'OK' if ok else 'DIVERGENTE'}] {name}" + (f" — {detail}" if detail else ""))


def get_path(obj: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def check_fields(label: str, obj: dict, fields: list[str]) -> None:
    missing = [f for f in fields if get_path(obj, f) is None]
    check(f"{label}: campos esperados", not missing, f"ausentes: {missing}" if missing else f"{len(fields)} campos presentes")


def openapi(client: httpx.Client, base: str) -> None:
    url = f"{base}/v3/api-docs"
    try:
        r = client.get(url)
        r.raise_for_status()
        spec = r.json()
    except Exception as exc:  # noqa: BLE001
        check(f"OpenAPI {url}", False, str(exc))
        return
    paths = spec.get("paths", {})
    report.setdefault("openapi", {})[base] = sorted(paths)
    for key, path in ENDPOINTS.items():
        if (base == CONSULTA_BASE) != (key in ("publicacao", "atualizacao", "proposta")):
            continue
        found = [p for p in paths if p.rstrip("/") == path.rstrip("/")]
        if not found:
            check(f"OpenAPI: caminho {path}", False, "não encontrado; veja report['openapi'] para os caminhos reais")
            continue
        params = sorted({p.get("name") for op in paths[found[0]].values() if isinstance(op, dict) for p in op.get("parameters", [])})
        check(f"OpenAPI: caminho {path}", True, f"parâmetros: {params}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=3)
    ap.add_argument("--modalidade", type=int, default=6)
    args = ap.parse_args()
    fim = date.today()
    ini = fim - timedelta(days=args.dias)

    with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=True,
                      headers={"User-Agent": "pncp-tax-radar-probe/0.1"}) as client:
        openapi(client, CONSULTA_BASE)
        openapi(client, PNCP_BASE)

        params = {"dataInicial": ini.strftime("%Y%m%d"), "dataFinal": fim.strftime("%Y%m%d"),
                  "codigoModalidadeContratacao": args.modalidade, "pagina": 1, "tamanhoPagina": 10}
        url = CONSULTA_BASE + ENDPOINTS["publicacao"]
        try:
            r = client.get(url, params=params)
        except httpx.HTTPError as exc:
            check(f"GET {ENDPOINTS['publicacao']}", False, f"sem conexão com o PNCP: {type(exc).__name__}: {exc}")
            return finish()
        check(f"GET {ENDPOINTS['publicacao']}", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            report["publicacao_body"] = r.text[:2000]
            return finish()
        page = r.json()
        check_fields("página de contratações", page, EXPECTED_PAGE_FIELDS)
        items = page.get("data") or []
        check("contratações retornadas", bool(items), f"{len(items)} na primeira página")
        if not items:
            return finish()
        report["exemplo_contratacao"] = items[0]
        check_fields("contratação", items[0], EXPECTED_ITEM_FIELDS)

        arquivos, keys = [], None
        for it in items:
            keys = compra_keys(it)
            if not keys:
                continue
            cnpj, ano, seq = keys
            ra = client.get(PNCP_BASE + ENDPOINTS["arquivos"].format(cnpj=cnpj, ano=ano, sequencial=seq))
            if ra.status_code == 200 and ra.json():
                arquivos = ra.json()
                break
        check("compra_keys() lê CNPJ/ano/sequencial", keys is not None, str(keys))
        check(f"GET {ENDPOINTS['arquivos']}", bool(arquivos), f"{len(arquivos)} arquivo(s) em {keys}")
        if arquivos:
            report["exemplo_arquivo"] = arquivos[0]
            check_fields("arquivo", arquivos[0], EXPECTED_ARQUIVO_FIELDS)
            file_url = arquivos[0].get("url")
            if file_url:
                with client.stream("GET", file_url) as rf:
                    head = next(rf.iter_bytes(), b"")[:8]
                    check("download de arquivo", rf.status_code == 200,
                          f"HTTP {rf.status_code}, content-type {rf.headers.get('content-type')}, início {head!r}")

        if keys:
            cnpj, ano, seq = keys
            ri = client.get(PNCP_BASE + ENDPOINTS["itens"].format(cnpj=cnpj, ano=ano, sequencial=seq),
                            params={"pagina": 1, "tamanhoPagina": 10})
            check(f"GET {ENDPOINTS['itens']}", ri.status_code == 200, f"HTTP {ri.status_code}")
            if ri.status_code == 200 and ri.json():
                item = ri.json()[0]
                report["exemplo_item"] = item
                rr = client.get(PNCP_BASE + ENDPOINTS["resultados"].format(cnpj=cnpj, ano=ano, sequencial=seq, numeroItem=item.get("numeroItem", 1)))
                check(f"GET {ENDPOINTS['resultados']}", rr.status_code in (200, 204), f"HTTP {rr.status_code}")
                if rr.status_code == 200 and rr.json():
                    report["exemplo_resultado"] = rr.json()[0]
                    check_fields("resultado", rr.json()[0], EXPECTED_RESULTADO_FIELDS)
    finish()


def finish() -> None:
    out = ROOT / "data" / "pncp_probe_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [c for c in report["checks"] if not c["ok"]]
    print(f"\n{len(report['checks']) - len(bad)} OK, {len(bad)} divergente(s). Relatório: {out}")
    if bad:
        print("Ajuste app/pncp_client.py (ENDPOINTS e leitura de campos) conforme o relatório.")


if __name__ == "__main__":
    main()
