"""Modo assistido: baixa os anexos da proposta vencedora de uma licitação do Compras.gov.

Abre a licitação numa janela de navegador no seu computador. Se aparecer o "não sou um robô", você resolve
na janela: o script não resolve captcha. Depois ele tenta abrir a proposta vencedora e os anexos sozinho; se
não conseguir algum clique, avisa e você clica. Todo arquivo baixado na janela vai para
inbox_editais/compra_<número>/, e o robô (python run.py) audita sozinho.

Uso:
    python tools/baixar_vencedora.py "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=..."

Instalação (uma vez):
    pip install playwright
    python -m playwright install chromium

Um caso por vez, com você presente.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
WINNER = re.compile(r"adjudicad|aceit[ao]|habilitad|vencedor", re.I)
ANEXO = re.compile(r"anexo", re.I)
PROPOSTAS_TXT = re.compile(r"detalhes poder[aã]o ser visualizados por fornecedor|Propostas", re.I)


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    return re.sub(r"[^\w.\- ()]", "_", name).strip(" .")[:150] or "anexo"


async def any_visible(locator) -> bool:
    for i in range(await locator.count()):
        if await locator.nth(i).is_visible():
            return True
    return False


async def open_winner_attachments(page: Page, timeout_ms: int) -> None:
    """Tenta abrir a lista de propostas, o card da vencedora e os anexos. Cada passo que falhar vira instrução."""
    if not await any_visible(page.get_by_text(PROPOSTAS_TXT)):
        buttons = page.locator("button[title*='ropost'], button[aria-label*='ropost'], [title*='ropost'], [aria-label*='ropost']")
        if await buttons.count():
            await buttons.first.click()
        else:
            print("-> Toque no ícone de lista (☰) do item/grupo para abrir as propostas.")
    await page.get_by_text(PROPOSTAS_TXT).locator("visible=true").first.wait_for(timeout=timeout_ms)

    winner = page.get_by_text(WINNER).locator("visible=true").first
    await winner.wait_for(timeout=timeout_ms)
    card = winner.locator("xpath=ancestor::*[.//button][1]")
    await card.locator("button").last.click()
    print("Proposta vencedora aberta.")

    links = page.get_by_text(ANEXO).locator("visible=true")
    await links.first.wait_for(timeout=timeout_ms)
    for i in range(await links.count()):
        link = links.nth(i)
        if await link.is_visible():
            await link.click()
            await page.wait_for_timeout(1500)
    # numa lista de arquivos, cada link/botão de arquivo dispara um download
    files = page.locator("a[download], a[href*='download'], a[href$='.pdf'], a[href$='.xlsx'], a[href$='.xls'], "
                         "a[href$='.zip'], button[title*='aixar'], button[aria-label*='aixar']")
    for i in range(await files.count()):
        f = files.nth(i)
        if await f.is_visible():
            await f.click()
            await page.wait_for_timeout(1000)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Baixa os anexos da proposta vencedora (modo assistido)")
    ap.add_argument("url", help="link 'Acompanhar contratação' do Compras.gov")
    ap.add_argument("--pasta", default=str(ROOT / "inbox_editais"), help="pasta monitorada pelo robô")
    ap.add_argument("--minutos", type=int, default=15, help="tempo máximo com a janela aberta")
    ap.add_argument("--espera", type=int, default=120, help="segundos de espera por cada passo da página")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)  # só para testes automáticos
    ap.add_argument("--fechar-ao-terminar", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    compra = (re.search(r"compra=(\d+)", args.url) or re.search(r"(\d{10,})", args.url))
    dest = Path(args.pasta) / f"compra_{compra.group(1) if compra else 'sem_numero'}"
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    async def on_download(download) -> None:
        target = dest / safe_name(download.suggested_filename)
        n = 1
        while target.exists():
            target = target.with_name(f"{target.stem}_{n}{target.suffix}")
            n += 1
        await download.save_as(target)
        saved.append(target)
        print(f"   baixado: {target.name}")

    def watch(page: Page) -> None:
        page.on("download", lambda d: asyncio.ensure_future(on_download(d)))

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(ROOT / "data" / "navegador"), headless=args.headless, accept_downloads=True,
            executable_path=os.environ.get("RADAR_CHROMIUM") or None,
        )
        context.on("page", watch)
        page = context.pages[0] if context.pages else await context.new_page()
        watch(page)
        closed = asyncio.Event()
        context.on("close", lambda _: closed.set())

        print("Abrindo a licitação. Se aparecer o 'não sou um robô', resolva na janela.")
        await page.goto(args.url)
        try:
            await open_winner_attachments(page, timeout_ms=args.espera * 1000)
        except Exception as exc:  # noqa: BLE001 - o site muda; a pessoa termina os cliques
            print(f"-> Não consegui fazer todos os cliques sozinho ({type(exc).__name__}).")
            print("   Na janela: abra a proposta da vencedora e clique nos anexos. Tudo que baixar vai para a pasta.")
        print(f"Arquivos vão para: {dest}")
        if args.fechar_ao_terminar:
            await page.wait_for_timeout(2000)
            await context.close()
        print("Quando terminar, feche a janela do navegador.")
        try:
            await asyncio.wait_for(closed.wait(), timeout=args.minutos * 60)
        except TimeoutError:
            await context.close()
        await asyncio.sleep(0.5)

    print(f"\n{len(saved)} arquivo(s) salvo(s) em {dest}")
    if saved:
        print("Com o robô aberto (python run.py), a auditoria aparece sozinha na aba Resultados.")


if __name__ == "__main__":
    asyncio.run(main())
