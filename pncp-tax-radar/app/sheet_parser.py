"""Transforma linhas extraídas em blocos de planilha de custos (um por posto) e detecta o contexto do caso."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from statistics import median

from .models import Cell, Document, Row
from .numbers import normalize

TAX_FIELDS = ("pis", "cofins", "pis_cofins", "iss", "icms", "simples")
ENCARGO_FIELDS = ("inss", "rat", "sal_educ", "sesc_sesi", "senai_senac", "sebrae", "incra", "fgts")
SISTEMA_S = ("sal_educ", "sesc_sesi", "senai_senac", "sebrae", "incra")
RATE_FIELDS = set(TAX_FIELDS) | set(ENCARGO_FIELDS) | {"lucro", "ci"}

# Ordem importa: totais antes dos tributos (ex.: "Total de tributos (PIS, COFINS, ISS)").
FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [
    (f, re.compile(p))
    for f, p in [
        ("total_tributos", r"total.*tribut|subtotal.*tribut"),
        ("total_encargos", r"total.*(encargo|submodulo 2\.2|previdenc)"),
        ("total_remuneracao", r"total.*(remunerac|modulo 1\b)"),
        ("subtotal", r"\bsubtotal\b|total dos modulos|base (de calculo )?(para|dos) tributos"),
        (
            "total_geral",
            r"valor (global|total|mensal)|preco (total|mensal|final|global)|"
            r"total (geral|por (empregado|posto|funcionario)|do posto|mensal)",
        ),
        ("pis_cofins", r"\bpis\b.*\bcofins\b|\bcofins\b.*\bpis\b"),
        ("pis", r"\bpis\b|\bpasep\b"),
        ("cofins", r"\bcofins\b"),
        ("iss", r"\biss(qn)?\b|imposto sobre servic"),
        ("icms", r"\bicms\b"),
        ("simples", r"simples nacional|documento de arrecadacao do simples"),
        ("fgts", r"\bfgts\b|fundo de garantia"),
        ("rat", r"\b(rat|sat|gilrat)\b|riscos? ambientais|acidentes? (de|do) trabalho"),
        ("sal_educ", r"salario[- ]educac"),
        ("sesc_sesi", r"\bses[ci]\b"),
        ("senai_senac", r"\bsena[ic]\b"),
        ("sebrae", r"\bsebrae\b"),
        ("incra", r"\bincra\b"),
        ("inss", r"\binss\b|previdencia social"),
        ("lucro", r"\blucro\b(?! (presumido|real|arbitrado))"),
        ("ci", r"custos? indiretos?|despesas (administrativas|operacionais|indiretas)"),
    ]
]

TYPICAL_RATE = {
    "pis": 0.0065, "cofins": 0.03, "pis_cofins": 0.0365, "iss": 0.03, "icms": 0.12, "simples": 0.08,
    "inss": 0.20, "rat": 0.02, "sal_educ": 0.025, "sesc_sesi": 0.015, "senai_senac": 0.01, "sebrae": 0.006,
    "incra": 0.002, "fgts": 0.08, "lucro": 0.06, "ci": 0.04,
}
MAX_RATE = {
    "pis": 0.03, "cofins": 0.12, "pis_cofins": 0.15, "iss": 0.10, "icms": 0.35, "simples": 0.35,
    "inss": 0.30, "rat": 0.08, "sal_educ": 0.05, "sesc_sesi": 0.05, "senai_senac": 0.05, "sebrae": 0.05,
    "incra": 0.05, "fgts": 0.12, "lucro": 0.6, "ci": 0.6,
}

BLOCK_START = re.compile(r"\bmodulo\s*0?1\b|composicao da remunerac")
TITLE_RE = re.compile(r"^(?:[a-z0-9]{1,3}[.)]?\s+)?(posto|funcao|cargo|categoria profissional)(?:\s*/\s*\w+)?\s*[:\-–]\s*(.+)")

CATEGORY_PATTERNS: dict[str, re.Pattern] = {
    "limpeza": re.compile(r"\blimpeza\b|conservacao|\basseio\b|higienizac|servente(?! de obra)"),
    "vigilancia": re.compile(r"vigilancia(?! (sanitaria|epidemiologica|em saude|ambiental))|\bvigilantes?\b|seguranca patrimonial"),
    "obras": re.compile(r"\bobras?\b|construcao civil|\breforma (predial|de edificac)|engenharia civil|pedreiro|servente de obras"),
    "cessao_outros": re.compile(
        r"recepc|recepcionista|copeir|copeiragem|\bmotoristas?\b|\bportari|\bporteir|apoio administrativo|"
        r"auxiliar administrativo|assistente administrativo|telefonista|\bgarco|almoxarif|secretariad|"
        r"brigadista|maqueiro|ascensorista|carregador"
    ),
    "bens": re.compile(r"fornecimento de (bens|materiais|equipamentos|produtos)|aquisicao de (bens|materiais|equipamentos|produtos)"),
}
ANEXO_IV = {"limpeza", "vigilancia", "obras"}
DEDICACAO = re.compile(r"dedicacao exclusiva|cessao de mao de obra|postos? de trabalho|terceirizac|regime de empreitada")

REGIME_NEAR = re.compile(
    r"(?:optante|regime (?:de )?tributac\w*|regime tributario|enquadrad[ao]|tributad[ao] (?:pelo|no))[^.\n]{0,80}?"
    r"(?P<regime>simples nacional|lucro presumido|lucro real)"
)
REGIME_ANY = re.compile(r"simples nacional|lucro presumido|lucro real")
REGIME_MAP = {"simples nacional": "simples", "lucro presumido": "presumido", "lucro real": "real"}
PROPOSAL_NAME = re.compile(r"propost|planilha|custo|preco|composic", re.I)
ORGAO_DOC_NAME = re.compile(r"edital|termo[ _-]*de[ _-]*refer|\btr\b|minuta|estimativ|referencia|anexo[ _-]*i\b", re.I)


def doc_role(name: str) -> str:
    base = normalize(name.rsplit("›", 1)[-1])
    if re.search(r"propost", base):
        return "proposta"
    if ORGAO_DOC_NAME.search(base):
        return "orgao"
    return "desconhecido"

PREGAO_RE = re.compile(r"preg[aã]o\s+(?:eletr[oô]nico\s+)?(?:sp\s+)?(?:n[º°o.]*\s*)?(\d{1,6}\s*/\s*\d{4})", re.I)
CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
LICITANTE_RE = re.compile(r"(?:licitante|raz[aã]o social|proponente|empresa)\s*[:\-–]\s*([^\n|—–]{3,120})", re.I)
ORGAO_RE = re.compile(r"(?:[óo]rg[aã]o|contratante|uasg)\s*[:\-–]\s*([^\n|—–]{3,120})", re.I)


IGNORED_LABEL = re.compile(r"incidencia|multa|aviso previo|\bgps\b|substitut")
TOTAL_FIELDS = {"total_tributos", "total_encargos", "total_remuneracao", "subtotal", "total_geral"}


def detect_field(label: str) -> str | None:
    norm = normalize(label)
    if not norm or IGNORED_LABEL.search(norm):
        return None
    for name, pattern in FIELD_PATTERNS:
        if pattern.search(norm):
            # linhas de resumo ("Módulo 6 – Custos indiretos, tributos e lucro") não são a rubrica em si
            if "modulo" in norm and name not in TOTAL_FIELDS:
                return None
            return name
    return None


def interpret_rate(cell: Cell, field_name: str) -> float | None:
    v = cell.value
    if v is None:
        return None
    if cell.percent == "fraction":
        return v
    if cell.percent == "points":
        return v / 100
    cands = [x for x in (v, v / 100) if 0 <= x <= MAX_RATE.get(field_name, 1.0)]
    if not cands:
        return None
    typ = TYPICAL_RATE.get(field_name, 0.05)
    return min(cands, key=lambda x: abs(math.log(max(x, 1e-6) / typ)))


@dataclass
class FieldHit:
    field: str
    row: Row
    rate: float | None = None
    value: float | None = None
    rate_cell: Cell | None = None
    value_cell: Cell | None = None

    @property
    def formula(self) -> str | None:
        return self.value_cell.formula if self.value_cell else None


def read_hit(row: Row, field_name: str) -> FieldHit:
    hit = FieldHit(field=field_name, row=row)
    label_idx = next((i for i, c in enumerate(row.cells) if c.is_label), None)
    # números à esquerda do rótulo são códigos de item (1, 2.1...), não alíquota nem valor
    nums = [c for c in row.cells[(label_idx + 1 if label_idx is not None else 0):] if c.value is not None]
    if not nums:
        return hit
    if field_name in RATE_FIELDS:
        pct = [c for c in nums if c.percent]
        rate_cell = pct[0] if pct else None
        if rate_cell is None:
            plain = [c for c in nums if not c.money]
            if len(nums) >= 2 and plain:
                cand = plain[0]
                if interpret_rate(cand, field_name) is not None:
                    rate_cell = cand
            elif len(nums) == 1 and not nums[0].money and interpret_rate(nums[0], field_name) is not None:
                rate_cell = nums[0]
        if rate_cell is not None:
            hit.rate_cell = rate_cell
            hit.rate = interpret_rate(rate_cell, field_name)
        rest = [c for c in nums if c is not rate_cell and not c.percent]
        if rate_cell is not None:
            after = [c for c in rest if nums.index(c) > nums.index(rate_cell)]
            rest = after or rest
        money_first = [c for c in rest if c.money] or rest
        if money_first:
            hit.value_cell = money_first[0]
            hit.value = money_first[0].value
    else:
        money_first = [c for c in nums if c.money and not c.percent] or [c for c in nums if not c.percent]
        if money_first:
            hit.value_cell = money_first[0]
            hit.value = money_first[0].value
    return hit


@dataclass
class CostBlock:
    doc: str
    title: str
    rows: list[Row] = field(default_factory=list)
    hits: dict[str, list[FieldHit]] = field(default_factory=dict)
    posto: str | None = None

    def first(self, name: str) -> FieldHit | None:
        lst = self.hits.get(name)
        return lst[0] if lst else None

    def all_hits(self, names) -> list[FieldHit]:
        out = []
        for n in names:
            out.extend(self.hits.get(n, []))
        return out

    def row_pos(self, row: Row) -> int:
        for i, r in enumerate(self.rows):
            if r is row:
                return i
        return -1

    @property
    def text(self) -> str:
        return "\n".join(r.raw() for r in self.rows)

    def relevant_count(self) -> int:
        return sum(1 for f in self.hits if f in RATE_FIELDS)

    def valued_count(self) -> int:
        """Rubricas com valor diferente de zero: um modelo em branco (tudo R$ 0,00) não é planilha para auditar."""
        return sum(1 for f, hs in self.hits.items() if f in RATE_FIELDS for h in hs if h.value)

    def encargos_base(self, exclude: tuple[str, ...] = ()) -> float | None:
        """Base de incidência dos encargos (Módulo 1 + Submódulo 2.1) inferida de valor ÷ alíquota."""
        bases = []
        for name in ENCARGO_FIELDS:
            if name in exclude:
                continue
            for h in self.hits.get(name, []):
                if h.rate and h.value and h.rate > 0 and h.value > 0:
                    bases.append(h.value / h.rate)
        if bases:
            return median(bases)
        rem = self.first("total_remuneracao")
        return rem.value if rem and rem.value else None


def looks_like_blank_template(doc: Document) -> bool:
    """Tem rubricas de planilha de custos, mas todas zeradas: é o modelo do edital, não uma proposta."""
    hits = [read_hit(r, f) for r in doc.rows if r.numbers and (f := detect_field(r.label)) in RATE_FIELDS]
    if len({h.field for h in hits}) < 4:
        return False
    return all(not h.value and not h.rate for h in hits)


def parse_blocks(doc: Document) -> list[CostBlock]:
    blocks: list[CostBlock] = []
    current: CostBlock | None = None
    current_sheet: str | None = None
    n = 0

    def new_block(sheet: str) -> CostBlock:
        nonlocal n
        n += 1
        title = sheet if doc.kind in ("xlsx", "xls") else f"bloco {n}"
        return CostBlock(doc=doc.name, title=title)

    for row in doc.rows:
        label_norm = normalize(row.label)
        sheet_changed = doc.kind in ("xlsx", "xls") and row.sheet != current_sheet
        # só cabeçalho (linha sem números) abre bloco: o quadro resumo repete "Módulo 1 ... R$ x"
        starts = (bool(BLOCK_START.search(label_norm)) and not row.numbers) or bool(TITLE_RE.match(label_norm))
        if current is None or sheet_changed or (starts and _has_cost_content(current)):
            if current is not None:
                blocks.append(current)
            current = new_block(row.sheet)
            current_sheet = row.sheet
        current.rows.append(row)
        m = TITLE_RE.match(label_norm)
        if m and current.posto is None:
            original = row.label
            idx = original.find(":")
            current.posto = original[idx + 1:].strip() if idx >= 0 else original.strip()
            current.title = f"Posto: {current.posto}"
        name = detect_field(row.label)
        if name and row.numbers:
            current.hits.setdefault(name, []).append(read_hit(row, name))
    if current is not None:
        blocks.append(current)
    result = [b for b in blocks if b.relevant_count() >= 2 and b.valued_count() >= 2]
    for i, b in enumerate(result, start=1):
        if b.posto is None and doc.kind not in ("xlsx", "xls"):
            b.title = f"{doc.name} — bloco {i}"
    return result


def _has_cost_content(block: CostBlock) -> bool:
    return any(f in block.hits for f in TAX_FIELDS + ENCARGO_FIELDS)


# ---------------------------------------------------------------- contexto do caso


@dataclass
class CaseContext:
    regime: str | None = None
    regime_evidence: str = ""
    regime_inferred: bool = False
    category: str | None = None
    category_evidence: str = ""
    dedicacao_exclusiva: bool = False
    pregao: str | None = None
    orgao: str | None = None
    licitante: str | None = None
    cnpj: str | None = None
    overrides: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def detect_category(text: str) -> tuple[str | None, str]:
    norm = normalize(text)
    best, best_count, evidence = None, 0, ""
    for cat, pattern in CATEGORY_PATTERNS.items():
        matches = list(pattern.finditer(norm))
        if len(matches) > best_count:
            best, best_count = cat, len(matches)
            m = matches[0]
            evidence = norm[max(0, m.start() - 60): m.end() + 60]
    return best, evidence


def block_category(block: CostBlock, ctx: CaseContext) -> str | None:
    if "categoria" in ctx.overrides:
        return ctx.category
    if block.posto:
        cat, _ = detect_category(block.posto)
        if cat:
            return cat
    return ctx.category


def detect_regime(text: str, blocks: list[CostBlock]) -> tuple[str | None, str, bool]:
    norm = normalize(text)
    m = REGIME_NEAR.search(norm)
    if m:
        return REGIME_MAP[m.group("regime")], norm[max(0, m.start() - 20): m.end() + 20], False
    found = {REGIME_MAP[x.group(0)] for x in REGIME_ANY.finditer(norm)}
    if len(found) == 1:
        regime = found.pop()
        x = REGIME_ANY.search(norm)
        return regime, norm[max(0, x.start() - 40): x.end() + 40], False
    for b in blocks:
        for h in b.hits.get("simples", []):
            if h.rate and h.rate > 0:
                return "simples", f"linha de tributo do Simples na planilha: {h.row.raw()}", True
    for b in blocks:
        fgts = b.first("fgts")
        sist = [b.first(f) for f in SISTEMA_S]
        sist = [h for h in sist if h is not None]
        if fgts and fgts.rate and len(sist) >= 3 and all((h.rate or 0) == 0 and (h.value or 0) == 0 for h in sist):
            return "simples", "contribuições do Sistema S zeradas com FGTS presente (padrão de optante do Simples)", True
    return None, "", False


def detect_context(documents: list[Document], blocks: list[CostBlock], overrides: dict | None = None) -> CaseContext:
    overrides = {k: v for k, v in (overrides or {}).items() if v and v != "auto"}
    proposal_names = {b.doc for b in blocks if doc_role(b.doc) != "orgao"} | {
        d.name for d in documents if PROPOSAL_NAME.search(d.name.rsplit("›", 1)[-1]) and doc_role(d.name) != "orgao"
    }
    proposal_text = "\n".join(d.text for d in documents if d.name in proposal_names)
    all_text = "\n".join(d.text for d in documents)
    ctx = CaseContext(overrides=overrides)

    if "regime" in overrides:
        ctx.regime, ctx.regime_evidence = overrides["regime"], "informado manualmente"
    else:
        ctx.regime, ctx.regime_evidence, ctx.regime_inferred = detect_regime(proposal_text, blocks)

    if "categoria" in overrides:
        ctx.category, ctx.category_evidence = overrides["categoria"], "informado manualmente"
    else:
        ctx.category, ctx.category_evidence = detect_category(all_text)

    norm_all = normalize(all_text)
    ctx.dedicacao_exclusiva = bool(DEDICACAO.search(norm_all)) or any(b.first("fgts") for b in blocks)
    if m := PREGAO_RE.search(all_text):
        ctx.pregao = re.sub(r"\s+", "", m.group(1))
    if m := CNPJ_RE.search(proposal_text):
        ctx.cnpj = m.group(0)
    if m := LICITANTE_RE.search(proposal_text):
        ctx.licitante = _cut_name(m.group(1))
    if m := ORGAO_RE.search(all_text):
        ctx.orgao = _cut_name(m.group(1))
    return ctx


def _cut_name(text: str) -> str:
    return re.split(r"\s+[-–—]\s+|\s*,?\s+CNPJ\b|\s*,?\s+inscrit", text, maxsplit=1, flags=re.I)[0].strip(" .;,")
