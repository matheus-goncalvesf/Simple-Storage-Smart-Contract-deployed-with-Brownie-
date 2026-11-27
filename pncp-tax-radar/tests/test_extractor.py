import zipfile

import pytest

from app.extractor import ZIP_MAX_DEPTH, extract_path, safe_extract_zip
from app.sheet_parser import parse_blocks


def _row(doc, label_part):
    return next(r for r in doc.rows if label_part.lower() in r.label.lower() and r.numbers)


def test_xlsx_reads_values_and_formulas(fx):
    [doc] = extract_path(fx["limpeza_grossup_por_fora"])
    assert doc.error is None
    pis = _row(doc, "PIS")
    rate, value = pis.numbers
    assert rate.percent == "fraction" and rate.value == pytest.approx(0.0065)
    assert value.formula.startswith("=D") and "*C" in value.formula
    assert value.value == pytest.approx(26.3174, abs=0.001)


def test_xlsx_values_only_matches_formula_version(fx):
    [a] = extract_path(fx["limpeza_presumido_correta"])
    [b] = extract_path(fx["limpeza_presumido_correta_valores"])
    va = _row(a, "VALOR TOTAL").numbers[-1].value
    vb = _row(b, "VALOR TOTAL").numbers[-1].value
    assert va == pytest.approx(vb, abs=0.001)


def test_pdf_lines_become_rows(fx):
    [doc] = extract_path(fx["limpeza_fgts_errado_por_fora_pdf"])
    inss = _row(doc, "INSS")
    rate, value = inss.numbers
    assert rate.value == 20.0 and rate.percent == "points"
    assert value.value == pytest.approx(416.48) and value.money
    assert inss.location.startswith("limpeza_fgts_errado_por_fora.pdf › pág. 1")


def test_docx_table_rows(fx):
    [doc] = extract_path(fx["limpeza_simples_sem_cpp_docx"])
    fgts = _row(doc, "FGTS")
    assert [c.value for c in fgts.numbers] == [8.0, pytest.approx(166.59)]
    assert len(parse_blocks(doc)) == 1


def test_corrupted_file_is_isolated(fx):
    [doc] = extract_path(fx["corrompido"])
    assert doc.error and "zip" in doc.error.lower()


def test_scanned_pdf_warns(fx):
    [doc] = extract_path(fx["digitalizado_pdf"])
    assert doc.error is None
    assert any("OCR" in w for w in doc.warnings)


def test_unsupported_extension(tmp_path):
    p = tmp_path / "anexos.rar"
    p.write_bytes(b"Rar!\x1a\x07\x00")
    [doc] = extract_path(p)
    assert ".rar" in doc.error


def test_zip_blocks_traversal(fx):
    docs = extract_path(fx["lote_copeiragem"])
    names = [d.name for d in docs]
    assert "lote_copeiragem.zip › proposta_vencedora.xlsx" in names
    assert "lote_copeiragem.zip › edital.docx" in names
    warnings = [w for d in docs for w in d.warnings]
    assert any("caminho inseguro" in w for w in warnings)


def test_zip_bomb_member_is_skipped(tmp_path):
    bomb = tmp_path / "bomba.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.txt", b"0" * (30 * 1024 * 1024))
        zf.writestr("ok.txt", "PIS 0,65% R$ 10,00")
    dest = tmp_path / "out"
    dest.mkdir()
    extracted, warnings = safe_extract_zip(bomb, dest)
    assert [name for _, name in extracted] == ["ok.txt"]
    assert any("zip bomb" in w for w in warnings)
    assert all(dest in p.parents for p, _ in extracted)


def test_nested_zip_depth_limit(tmp_path):
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("a.txt", "texto")
    current = inner
    for level in range(ZIP_MAX_DEPTH + 1):
        outer = tmp_path / f"level{level}.zip"
        with zipfile.ZipFile(outer, "w") as zf:
            zf.write(current, current.name)
        current = outer
    docs = extract_path(current)
    assert any(d.error and "profundidade" in d.error for d in docs)
