"""Extração de texto e linhas de planilhas a partir de xlsx, xls, pdf, docx, csv/txt e zip."""

from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .formula import WorkbookEvaluator
from .models import Cell, Document, Row
from .numbers import fmt_money, fmt_num, fmt_pct, parse_number, split_line

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_PDF_PAGES = 300
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 60

ZIP_MAX_MEMBERS = 300
ZIP_MAX_MEMBER_BYTES = 100 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 300 * 1024 * 1024
ZIP_MAX_RATIO = 200
ZIP_MAX_DEPTH = 2

SUPPORTED_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".pdf", ".docx", ".csv", ".txt", ".zip"}
UNSUPPORTED_HINT = {
    ".rar": "arquivo .rar não suportado: extraia manualmente e envie os arquivos",
    ".7z": "arquivo .7z não suportado: extraia manualmente e envie os arquivos",
    ".doc": "arquivo .doc (Word antigo) não suportado: salve como .docx ou PDF",
    ".ods": "arquivo .ods não suportado: salve como .xlsx",
    ".odt": "arquivo .odt não suportado: salve como .docx ou PDF",
}


def extract_path(path: Path, display_name: str | None = None, _depth: int = 0) -> list[Document]:
    """Extrai um arquivo. Um .zip vira vários documentos. Nunca levanta exceção: erros ficam no Document."""
    name = display_name or path.name
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [Document(name=name, kind=ext.lstrip("."), error=f"arquivo inacessível: {exc}")]
    if size > MAX_FILE_BYTES:
        return [Document(name=name, kind=ext.lstrip("."), error=f"arquivo maior que {MAX_FILE_BYTES // 2**20} MB")]
    if ext in UNSUPPORTED_HINT:
        return [Document(name=name, kind=ext.lstrip("."), error=UNSUPPORTED_HINT[ext])]
    try:
        if ext == ".zip":
            return _extract_zip(path, name, _depth)
        if ext in (".xlsx", ".xlsm"):
            return [_extract_xlsx(path, name)]
        if ext == ".xls":
            return [_extract_xls(path, name)]
        if ext == ".pdf":
            return [_extract_pdf(path, name)]
        if ext == ".docx":
            return [_extract_docx(path, name)]
        if ext in (".csv", ".txt"):
            return [_extract_text(path, name, ext)]
    except Exception as exc:  # noqa: BLE001 - isolamento de falha por arquivo
        log.warning("falha ao extrair %s: %s", name, exc)
        return [Document(name=name, kind=ext.lstrip("."), error=f"{type(exc).__name__}: {exc}"[:500])]
    return [Document(name=name, kind=ext.lstrip(".") or "desconhecido", error="formato não suportado")]


# ---------------------------------------------------------------- xlsx


def _xlsx_cell(c, evaluator: WorkbookEvaluator, sheet: str) -> Cell | None:
    raw = c.value
    if raw is None:
        return None
    fmt = (c.number_format or "").lower()
    percent = "fraction" if "%" in fmt else None
    money = "r$" in fmt
    if isinstance(raw, str) and raw.startswith("="):
        value = evaluator.try_value(sheet, c.coordinate)
        text = _display(value, percent, money) if value is not None else raw
        return Cell(text=text, value=value, formula=raw, coord=c.coordinate, percent=percent, money=money)
    if isinstance(raw, bool):
        return Cell(text=str(raw), coord=c.coordinate)
    if isinstance(raw, (int, float)):
        value = float(raw)
        return Cell(text=_display(value, percent, money), value=value, coord=c.coordinate, percent=percent, money=money)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        parsed = parse_number(s)
        if parsed is not None:
            parsed.coord = c.coordinate
            return parsed
        return Cell(text=s, coord=c.coordinate)
    if hasattr(raw, "text"):  # ArrayFormula / DataTableFormula
        return Cell(text=str(getattr(raw, "text", "")), formula=str(getattr(raw, "text", "")), coord=c.coordinate)
    return Cell(text=str(raw), coord=c.coordinate)


def _display(value: float, percent: str | None, money: bool) -> str:
    if percent == "fraction":
        return fmt_pct(value)
    if money:
        return fmt_money(value)
    return fmt_num(value)


def _extract_xlsx(path: Path, name: str) -> Document:
    import openpyxl

    fwb = openpyxl.load_workbook(path, data_only=False, read_only=False)
    try:
        vwb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception:  # noqa: BLE001
        vwb = None
    evaluator = WorkbookEvaluator(fwb, vwb)
    doc = Document(name=name, kind="xlsx")
    texts: list[str] = []
    has_formula_without_cache = False
    for ws in fwb.worksheets:
        if ws.sheet_state != "visible":
            doc.warnings.append(f"aba oculta analisada: {ws.title}")
        max_row = min(ws.max_row or 0, MAX_SHEET_ROWS)
        max_col = min(ws.max_column or 0, MAX_SHEET_COLS)
        if (ws.max_row or 0) > MAX_SHEET_ROWS:
            doc.warnings.append(f"aba {ws.title}: só as primeiras {MAX_SHEET_ROWS} linhas foram lidas")
        for xl_row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
            cells = []
            for c in xl_row:
                cell = _xlsx_cell(c, evaluator, ws.title)
                if cell is None:
                    continue
                if cell.formula and vwb is not None and vwb[ws.title][c.coordinate].value is None:
                    has_formula_without_cache = True
                cells.append(cell)
            if not cells:
                continue
            row = Row(doc=name, sheet=ws.title, index=xl_row[0].row, cells=cells)
            doc.rows.append(row)
            texts.append(" ".join(c.text for c in cells))
    if has_formula_without_cache:
        doc.warnings.append("fórmulas sem valor salvo: valores recalculados pelo avaliador interno")
    doc.text = "\n".join(texts)
    return doc


# ---------------------------------------------------------------- xls


def _extract_xls(path: Path, name: str) -> Document:
    import xlrd

    try:
        book = xlrd.open_workbook(str(path), formatting_info=True)
        formatting = True
    except (NotImplementedError, xlrd.XLRDError):
        book = xlrd.open_workbook(str(path))
        formatting = False
    doc = Document(name=name, kind="xls", warnings=["arquivo .xls: só valores (fórmulas não disponíveis)"])
    texts: list[str] = []
    for sh in book.sheets():
        for r in range(min(sh.nrows, MAX_SHEET_ROWS)):
            cells = []
            for col in range(min(sh.ncols, MAX_SHEET_COLS)):
                xc = sh.cell(r, col)
                coord = f"{_col_letter(col)}{r + 1}"
                if xc.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    continue
                if xc.ctype == xlrd.XL_CELL_NUMBER:
                    percent = None
                    money = False
                    if formatting:
                        try:
                            fmt = book.format_map[book.xf_list[xc.xf_index].format_key].format_str.lower()
                            percent = "fraction" if "%" in fmt else None
                            money = "r$" in fmt
                        except (KeyError, IndexError, AttributeError):
                            pass
                    v = float(xc.value)
                    cells.append(Cell(text=_display(v, percent, money), value=v, coord=coord, percent=percent, money=money))
                else:
                    s = str(xc.value).strip()
                    if not s:
                        continue
                    parsed = parse_number(s)
                    if parsed is not None:
                        parsed.coord = coord
                        cells.append(parsed)
                    else:
                        cells.append(Cell(text=s, coord=coord))
            if cells:
                doc.rows.append(Row(doc=name, sheet=sh.name, index=r + 1, cells=cells))
                texts.append(" ".join(c.text for c in cells))
    doc.text = "\n".join(texts)
    return doc


def _col_letter(idx: int) -> str:
    from openpyxl.utils.cell import get_column_letter

    return get_column_letter(idx + 1)


# ---------------------------------------------------------------- pdf


def _extract_pdf(path: Path, name: str) -> Document:
    doc = Document(name=name, kind="pdf")
    pages_text: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            n_pages = len(pdf.pages)
            for page in pdf.pages[:MAX_PDF_PAGES]:
                pages_text.append(page.extract_text() or "")
    except Exception as exc:  # noqa: BLE001 - tenta o leitor alternativo
        from pypdf import PdfReader

        pages_text = []
        doc.warnings.append(f"pdfplumber falhou ({type(exc).__name__}); usado pypdf")
        reader = PdfReader(str(path))
        n_pages = len(reader.pages)
        pages_text = [(p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES]]
    if n_pages > MAX_PDF_PAGES:
        doc.warnings.append(f"só as primeiras {MAX_PDF_PAGES} páginas foram lidas")
    total_chars = sum(len(t.strip()) for t in pages_text)
    if total_chars < 30 * max(1, len(pages_text)):
        doc.warnings.append("PDF quase sem texto (provavelmente digitalizado/imagem): OCR não incluído")
    for pno, text in enumerate(pages_text, start=1):
        for lno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            cells = split_line(line)
            if cells:
                doc.rows.append(Row(doc=name, sheet=f"pág. {pno}", index=lno, cells=cells))
    doc.text = "\n".join(pages_text)
    return doc


# ---------------------------------------------------------------- docx


def _extract_docx(path: Path, name: str) -> Document:
    import docx

    d = docx.Document(str(path))
    doc = Document(name=name, kind="docx")
    texts: list[str] = []
    lno = 0
    for para in d.paragraphs:
        line = para.text.strip()
        if not line:
            continue
        lno += 1
        texts.append(line)
        cells = split_line(line)
        if cells:
            doc.rows.append(Row(doc=name, sheet="texto", index=lno, cells=cells))
    for tno, table in enumerate(d.tables, start=1):
        for rno, trow in enumerate(table.rows, start=1):
            cells: list[Cell] = []
            seen: set[int] = set()
            for tc in trow.cells:
                if id(tc._tc) in seen:  # células mescladas se repetem
                    continue
                seen.add(id(tc._tc))
                s = tc.text.strip()
                if not s:
                    continue
                parsed = parse_number(s)
                cells.append(parsed if parsed is not None else Cell(text=s))
            if cells:
                doc.rows.append(Row(doc=name, sheet=f"tabela {tno}", index=rno, cells=cells))
                texts.append(" ".join(c.text for c in cells))
    doc.text = "\n".join(texts)
    return doc


# ---------------------------------------------------------------- csv / txt


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_text(path: Path, name: str, ext: str) -> Document:
    content = _read_text(path)
    doc = Document(name=name, kind=ext.lstrip("."), text=content)
    if ext == ".csv":
        sample = content[:4096]
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        for i, rec in enumerate(csv.reader(io.StringIO(content), delimiter=delim), start=1):
            cells = []
            for field_ in rec:
                s = field_.strip()
                if not s:
                    continue
                parsed = parse_number(s)
                cells.append(parsed if parsed is not None else Cell(text=s))
            if cells:
                doc.rows.append(Row(doc=name, sheet="csv", index=i, cells=cells))
    else:
        for i, line in enumerate(content.splitlines(), start=1):
            if line.strip():
                cells = split_line(line)
                if cells:
                    doc.rows.append(Row(doc=name, sheet="texto", index=i, cells=cells))
    return doc


# ---------------------------------------------------------------- zip


def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:  # nome não marcado como UTF-8: zips do Windows costumam vir em cp850
        try:
            raw = name.encode("cp437")
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                name = raw.decode("cp850")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def safe_extract_zip(zip_path: Path, dest: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """Extrai com limites de tamanho/quantidade, bloqueio de path traversal e de zip bomb."""
    extracted: list[tuple[Path, str]] = []
    warnings: list[str] = []
    dest = dest.resolve()
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_MEMBERS:
            warnings.append(f"zip com {len(infos)} arquivos: só os primeiros {ZIP_MAX_MEMBERS} foram lidos")
            infos = infos[:ZIP_MAX_MEMBERS]
        for idx, info in enumerate(infos):
            name = _member_name(info)
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                warnings.append(f"ignorado (caminho inseguro): {name}")
                continue
            if info.flag_bits & 0x1:
                warnings.append(f"ignorado (protegido por senha): {name}")
                continue
            if info.file_size > ZIP_MAX_MEMBER_BYTES:
                warnings.append(f"ignorado (grande demais): {name}")
                continue
            if info.compress_size and info.file_size / info.compress_size > ZIP_MAX_RATIO:
                warnings.append(f"ignorado (taxa de compressão suspeita, possível zip bomb): {name}")
                continue
            if total + info.file_size > ZIP_MAX_TOTAL_BYTES:
                warnings.append("limite total de descompactação atingido; restante ignorado")
                break
            safe_name = re.sub(r"[^\w.\- ()]", "_", PurePosixPath(name).name) or f"arquivo_{idx}"
            target = (dest / f"{idx:03d}_{safe_name}").resolve()
            if dest not in target.parents:
                warnings.append(f"ignorado (caminho inseguro): {name}")
                continue
            written = 0
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    while chunk := src.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size or written > ZIP_MAX_MEMBER_BYTES:
                            break
                        out.write(chunk)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as exc:
                target.unlink(missing_ok=True)
                warnings.append(f"ignorado (arquivo corrompido no zip: {type(exc).__name__}): {name}")
                continue
            if written > info.file_size:
                target.unlink(missing_ok=True)
                warnings.append(f"ignorado (tamanho declarado inconsistente): {name}")
                continue
            total += written
            extracted.append((target, name))
    return extracted, warnings


def _extract_zip(path: Path, name: str, depth: int) -> list[Document]:
    if depth >= ZIP_MAX_DEPTH:
        return [Document(name=name, kind="zip", error="zip dentro de zip além do limite de profundidade")]
    tmp = Path(tempfile.mkdtemp(prefix="radar_zip_"))
    try:
        members, warnings = safe_extract_zip(path, tmp)
        docs: list[Document] = []
        if warnings:
            docs.append(Document(name=name, kind="zip", warnings=warnings))
        for member_path, member_name in members:
            ext = Path(member_name).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS and ext not in UNSUPPORTED_HINT:
                continue
            docs.extend(extract_path(member_path, f"{name} › {member_name}", depth + 1))
        if not docs:
            docs.append(Document(name=name, kind="zip", warnings=["zip sem arquivos suportados"]))
        return docs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
