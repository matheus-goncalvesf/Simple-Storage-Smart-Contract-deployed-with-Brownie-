# PNCP Tax Radar

Robô local que audita a planilha de custos da **proposta vencedora** de uma licitação e aponta erros tributários
que podem derrubá-la, para o **segundo colocado** fundamentar o recurso. Todo o cálculo é feito por regras em
Python, sem IA, e cada achado mostra a linha, a célula e a fórmula de onde saiu.

## Como rodar

```bash
cd pncp-tax-radar
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/python run.py                       # Windows: .venv\Scripts\python run.py
```

O navegador abre em http://127.0.0.1:8000. Requer Python 3.11+.

## Fluxo de uso

1. Na sessão, o cliente (2º colocado) manifesta a intenção de recorrer e baixa a proposta e a planilha da
   vencedora no portal da disputa (Compras.gov, BLL, BEC etc.).
2. Arraste os arquivos na aba **Auditar arquivos** ou jogue-os na pasta `inbox_editais/`. O edital pode ir junto,
   como contexto (objeto, órgão, número do pregão).
3. Em **Resultados**, linhas vermelhas são erros que provavelmente derrubam a proposta; âmbar são erros sanáveis
   ou que precisam de confirmação.
4. **Ver erro e evidência**: o que está na planilha × o correto, o impacto em R$ comparado ao lucro declarado e
   o trecho exato do arquivo (aba, linha, fórmula).
5. **Ver tese / solução** e **Rascunho de recurso**: textos prontos para revisar e protocolar. O prazo é a
   intenção imediata na sessão e as razões em 3 dias úteis (Lei 14.133/2021, art. 165).

## Como o robô decide se o erro derruba a proposta

Erro de planilha, em regra, é corrigível: a vencedora ajusta a planilha mantendo o preço (IN SEGES/MP 5/2017,
Anexo VII-A, item 7.9). Por isso cada regra calcula **quanto custa corrigir o erro mantendo o preço ofertado** e
compara com a margem que a própria planilha declara:

| Veredito | Condição | Severidade |
|---|---|---|
| **Derruba** | correção > lucro + custos indiretos | ALTA |
| **Provável** | correção > lucro | ALTA |
| **Sanável** | correção cabe no lucro | MÉDIA |
| **Indeterminado** | impacto depende de dado ausente (ex.: regime não identificado) | MÉDIA |
| **Sem impacto** | erro a favor da Administração (sobrepreço) | INFO |

Se vários erros pequenos somados passam do lucro, o robô gera o achado combinado **R99**.

## Regras implementadas (`app/rules_engine.py`)

| Regra | O que verifica | Base principal |
|---|---|---|
| R01 | PIS e COFINS de regimes diferentes (proporção PIS/COFINS longe de 0,217) | Leis 9.715/98, 9.718/98, 10.637/02, 10.833/03 |
| R02 | PIS/COFINS ausente ou abaixo do cumulativo (3,65%) no Lucro Presumido | idem |
| R03 | ISS abaixo de 2% (exceto construção civil), acima de 5% ou ausente | LC 116/2003, arts. 7º, 8º e 8º-A |
| R04 | ISS em fornecimento de bens | LC 116/2003, art. 1º |
| R05 | INSS patronal (CPP) omitido ou < 20%; Simples no Anexo IV sem a CPP fora do DAS | Lei 8.212/91, art. 22; LC 123/2006, arts. 13 e 18, § 5º-C |
| R06 | Optante do Simples em cessão de mão de obra fora do Anexo IV (recepção, copeiragem, portaria...) | LC 123/2006, arts. 17, XII, 18, § 5º-H, e 30, II |
| R07 | FGTS diferente de 8% ou ausente | Lei 8.036/90, art. 15 |
| R08 | Tributos calculados "por fora" (sem gross-up), por fórmula ou por valores | Lei 9.718/98, art. 3º; LC 116/2003, art. 7º |
| R09 | Lucro Real com alíquota efetiva de PIS/COFINS sem comprovação | Leis 10.637/02 e 10.833/03 |
| R99 | Soma dos erros sanáveis supera o lucro | Lei 14.133/2021, art. 59, III |

A base legal fica em `app/legal.py`, separada do código. Ela foi redigida automaticamente e **não cita
jurisprudência**. Revise com advogado ou contador antes de protocolar e acrescente os acórdãos que o escritório usa.

## O que o robô lê

- `.xlsx`/`.xlsm`: valores e fórmulas. Quando a planilha não tem valor salvo, recalcula as fórmulas com um avaliador próprio.
- `.xls` (só valores), `.pdf` com texto, `.docx` (parágrafos e tabelas), `.csv`/`.txt`.
- `.zip`, com proteção contra zip bomb, path traversal e zip dentro de zip.
- Um arquivo corrompido ou ilegível é registrado no caso e não interrompe os outros.

### Limitações conhecidas

- PDF digitalizado (imagem): sem OCR, o robô só avisa.
- `.rar`, `.7z`, `.doc`, `.ods`: não suportados (a interface explica como converter).
- Vários postos em colunas lado a lado na mesma aba: só a primeira coluna de valores é lida. Uma aba ou um bloco por posto funciona.
- ISS: o cálculo usa o mínimo legal de 2%, não a tabela de cada município.
- Desoneração da folha (CPRB) não é modelada; por isso, INSS abaixo de 20% em obras fora do Simples não é apontado.
- O regime da vencedora é detectado pelo texto da proposta ou pela estrutura da planilha. Quando não for, informe-o manualmente no upload.

## Varredura no PNCP

**Atenção:** os endpoints abaixo seguem a documentação pública do PNCP, mas **não foram testados contra a API
real**, porque o ambiente de desenvolvimento não tinha acesso a `pncp.gov.br`. Antes do primeiro uso, rode na sua
máquina:

```bash
.venv/bin/python tools/pncp_probe.py
```

O script confere os caminhos no OpenAPI oficial, faz requisições reais e grava `data/pncp_probe_report.json`
com OK/DIVERGENTE por campo. Se algo divergir, ajuste `ENDPOINTS` em `app/pncp_client.py`.

| Uso | Endpoint |
|---|---|
| Contratações publicadas no período | `GET https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao?dataInicial=AAAAMMDD&dataFinal=AAAAMMDD&codigoModalidadeContratacao=6&uf=SP&pagina=1&tamanhoPagina=50` |
| Contratações atualizadas no período | `GET .../api/consulta/v1/contratacoes/atualizacao` (mesmos parâmetros) |
| Propostas em aberto | `GET .../api/consulta/v1/contratacoes/proposta?dataFinal=...&codigoModalidadeContratacao=...` |
| Anexos da compra | `GET https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos` |
| Itens e vencedores | `GET .../compras/{ano}/{sequencial}/itens` e `.../itens/{numero}/resultados` |

Resiliência: no máximo 4 requisições simultâneas, timeout de 10 s para conexão e 30 s para leitura, até 4 novas
tentativas com espera de 1, 2, 4 e 8 s em HTTP 429/5xx e falha de rede (respeita `Retry-After`). Cada licitação e
cada anexo falham de forma isolada, e a varredura pode ser parada pela interface. As listas de anexos ficam em
cache no SQLite por 6 horas.

A planilha da vencedora raramente aparece no PNCP dentro do prazo de recurso. Use a varredura principalmente
para **prospectar** licitações de terceirização e oferecer o serviço antes da sessão.

## Pasta monitorada

Cada arquivo ou subpasta de primeiro nível em `inbox_editais/` vira um caso. A pasta é verificada a cada 15 s;
arquivos ainda sendo copiados (alterados há menos de 3 s) ficam para a próxima verificação. Um caso alterado é
reauditado, e o resultado anterior é substituído.

## Configuração (variáveis de ambiente)

| Variável | Padrão |
|---|---|
| `RADAR_DATA_DIR` | `./data` (SQLite, uploads, downloads) |
| `RADAR_INBOX_DIR` | `./inbox_editais` |
| `RADAR_WATCH_INBOX` | `1` (use `0` para desligar) |
| `RADAR_WATCH_INTERVAL` | `15` segundos |
| `RADAR_PORT` | `8000` |

## Testes

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

`tests/fixtures/make_fixtures.py` gera planilhas sintéticas no modelo da IN 5/2017, com erros de propósito e
casos limpos de controle. Os arquivos gerados ficam em `tests/fixtures/` e podem ser usados para testar o upload.

## Estrutura

```text
pncp-tax-radar/
├── app/
│   ├── main.py            # FastAPI: REST, SSE, upload, exportação
│   ├── pncp_client.py     # cliente assíncrono do PNCP (semáforo, retry, timeouts)
│   ├── pipeline.py        # jobs com progresso, cancelamento e falha isolada
│   ├── extractor.py       # xlsx/xls/pdf/docx/csv/zip → linhas
│   ├── formula.py         # avaliador de fórmulas do Excel
│   ├── sheet_parser.py    # linhas → planilha por posto + contexto (regime, objeto, pregão)
│   ├── rules_engine.py    # regras, impacto e veredito
│   ├── legal.py           # base legal e teses (revisar)
│   ├── draft.py           # rascunhos de tese e de recurso
│   ├── auditor.py         # orquestra um caso
│   ├── database.py        # SQLite
│   ├── folder_watcher.py  # pasta monitorada
│   └── static/            # index.html + app.css (Tailwind gerado localmente)
├── tests/                 # pytest + fixtures sintéticas
├── tools/pncp_probe.py    # validação da API real do PNCP
├── inbox_editais/
├── run.py
└── requirements.txt
```

O CSS da interface já vem gerado em `app/static/app.css`. Só é preciso regenerá-lo se você alterar classes do
`index.html`: `npm install && npm run build:css`.
