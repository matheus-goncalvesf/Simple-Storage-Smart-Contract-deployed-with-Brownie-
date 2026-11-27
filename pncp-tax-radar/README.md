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

1. **Oportunidades**: o radar busca sozinho no PNCP, a cada 6 horas (configurável), licitações dos segmentos
   escolhidos (limpeza, vigilância, recepção/portaria/copeiragem, terceirização, obras) e mostra situação, prazo das
   propostas, órgão, valor, vencedor (quando já publicado) e o link da disputa.
2. **Antes da sessão**: use a lista "Recebendo propostas" para oferecer o serviço às empresas que vão disputar.
3. **Depois da sessão**: o cliente (2º colocado) baixa a proposta e a planilha da vencedora no portal da disputa.
   Na licitação, clique em **Auditar planilha da vencedora** e envie os arquivos. O resultado fica ligado à
   licitação, e o recurso já sai com órgão, número e vencedora preenchidos.
4. Em **Resultados**, linhas vermelhas são erros que provavelmente derrubam a proposta; âmbar são erros sanáveis
   ou que precisam de confirmação. **Ver erro e evidência** mostra o que está na planilha × o correto, o impacto em
   R$ contra o lucro declarado e o trecho exato do arquivo.
5. **Ver tese / solução** e **Rascunho de recurso** trazem os textos para revisar e protocolar. O prazo é a intenção
   imediata na sessão e as razões em 3 dias úteis (Lei 14.133/2021, art. 165).

Também dá para auditar arquivos soltos (aba **Auditar arquivos**) ou jogá-los na pasta `inbox_editais/`.

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

## Radar e dados públicos

Os endpoints do PNCP foram conferidos contra a API real em 27/11/2026 com `tools/pncp_probe.py`. Rode o probe de
novo se algo parar de funcionar.

| Uso | Endpoint |
|---|---|
| Licitações recebendo propostas | `GET https://pncp.gov.br/api/consulta/v1/contratacoes/proposta?dataFinal=AAAAMMDD&codigoModalidadeContratacao=6&pagina=1&tamanhoPagina=50` |
| Licitações atualizadas no período | `GET .../api/consulta/v1/contratacoes/atualizacao?dataInicial=...&dataFinal=...` (mesmos parâmetros) |
| Itens e vencedores | `GET https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens` e `.../itens/{numeroItem}/resultados` |
| Anexos da compra | `GET .../api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/arquivos` |

Resiliência: no máximo 4 requisições simultâneas, timeout de 10 s para conexão e 30 s para leitura, até 4 novas
tentativas com espera de 1, 2, 4 e 8 s em HTTP 429/5xx e falha de rede (respeita `Retry-After`). Cada consulta falha
de forma isolada. O radar descarta licitações encerradas há mais de 90 dias.

### Por que a planilha da vencedora entra por upload

Medição feita em 27/11/2026:

- Em **100 contratos de terceirização** publicados no PNCP no último mês, só **1** trazia a planilha da vencedora
  anexada. Os demais arquivos com "proposta" no nome eram modelos em branco do edital ou pareceres.
- **33 de 43** pregões de terceirização da semana correram no **Compras.gov**, cuja página pública de
  acompanhamento exige **hCaptcha**. O robô não burla captcha.

Por isso a planilha da vencedora chega pelo cliente (o 2º colocado tem acesso a ela no portal da disputa), e todo o
resto (achar a licitação, identificar a vencedora, auditar e redigir o recurso) é automático.

A busca antiga por anexos (`POST /api/pncp/scan`) continua disponível na API para quem quiser baixar os anexos
publicados de um conjunto de licitações.

## Modo assistido (no computador)

`tools/baixar_vencedora.py` abre a licitação do Compras.gov numa janela de navegador, tenta abrir a proposta
vencedora e os anexos, e salva tudo que for baixado em `inbox_editais/compra_<número>/`, onde o robô audita
sozinho. Se aparecer o "não sou um robô", você resolve na janela: o script não resolve captcha. Se algum clique
automático falhar (o site muda), ele avisa e você clica; os downloads vão para a pasta do mesmo jeito.

```bash
.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium   # uma vez
.venv/bin/python tools/baixar_vencedora.py "https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra=..."
```

Testado numa página que imita a do Compras.gov; no site real os cliques automáticos ainda não foram validados.

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
| `RADAR_AUTO_SCAN` | `1` (use `0` para desligar a busca automática do radar) |
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
│   ├── radar.py           # radar de oportunidades (busca agendada, segmentos, vencedores)
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
