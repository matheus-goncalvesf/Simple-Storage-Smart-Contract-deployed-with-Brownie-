"""Base legal e teses de cada regra.

Textos redigidos automaticamente a partir da legislação citada. Antes de protocolar qualquer peça,
revise com advogado/contador: confirme a redação vigente dos dispositivos e acrescente a jurisprudência
que o escritório usa. Nenhum acórdão é citado aqui de propósito, para evitar referência não conferida.
"""

REVIEW_NOTICE = "Base legal redigida automaticamente: revise com advogado/contador antes de protocolar."

RECURSO_BASIS = (
    "Lei nº 14.133/2021, art. 59, III (desclassificação de proposta inexequível) e § 2º (diligência para aferir "
    "exequibilidade); art. 165, I, 'b', e § 1º, I (recurso contra o julgamento das propostas: intenção imediata e "
    "razões em 3 dias úteis); art. 170, § 4º (representação ao tribunal de contas). IN SEGES/MP nº 5/2017, "
    "Anexo VII-A, item 7.9 (o ajuste da planilha só é admitido sem majoração do preço proposto)."
)

RULES: dict[str, dict[str, str]] = {
    "R01": {
        "title": "PIS e COFINS de regimes diferentes na mesma planilha",
        "legal_basis": (
            "Lei nº 9.715/1998, art. 8º, I (PIS cumulativo 0,65%); Lei nº 9.718/1998, art. 8º (COFINS cumulativa 3%); "
            "Lei nº 10.637/2002, art. 2º (PIS não cumulativo 1,65%); Lei nº 10.833/2003, art. 2º (COFINS não "
            "cumulativa 7,6%)."
        ),
        "explanation": (
            "PIS e COFINS seguem o mesmo regime da empresa: cumulativo (Lucro Presumido: 0,65% + 3%) ou não "
            "cumulativo (Lucro Real: 1,65% + 7,6%). Nos dois casos a proporção PIS/COFINS fica perto de 0,217, "
            "inclusive com alíquota efetiva, porque os créditos de PIS e de COFINS são calculados sobre a mesma base. "
            "Proporção muito diferente indica mistura de regimes."
        ),
        "solution": (
            "Pedir diligência para a vencedora comprovar o regime (ECF e EFD-Contribuições) e reapresentar a planilha "
            "com as alíquotas desse regime. Se a correção exigir aumentar o preço ou consumir mais que o lucro "
            "declarado, sustentar a desclassificação por inexequibilidade."
        ),
    },
    "R02": {
        "title": "PIS/COFINS abaixo do mínimo do regime ou ausente",
        "legal_basis": (
            "Lei nº 9.715/1998, art. 8º, I, e Lei nº 9.718/1998, art. 8º (regime cumulativo: 0,65% + 3% sobre a "
            "receita bruta, sem créditos); Leis nº 10.637/2002 e 10.833/2003, art. 2º (regime não cumulativo)."
        ),
        "explanation": (
            "No Lucro Presumido não há créditos: PIS e COFINS são exatamente 0,65% e 3% sobre o faturamento. Empresa "
            "fora do Simples sempre recolhe PIS e COFINS sobre a receita do contrato."
        ),
        "solution": (
            "Demonstrar que a alíquota correta (mínimo de 3,65% no cumulativo) aplicada ao preço ofertado gera custo "
            "maior que o declarado e comparar com o lucro da planilha. Pedir a desclassificação se a diferença não "
            "couber na margem, ou diligência para a vencedora provar o regime."
        ),
    },
    "R03": {
        "title": "ISS fora dos limites legais (2% a 5%)",
        "legal_basis": (
            "LC nº 116/2003, art. 8º-A, caput e § 1º (alíquota mínima de 2%, vedado benefício que reduza a carga "
            "abaixo disso, salvo subitens 7.02, 7.05 e 16.01 da lista); art. 8º, II (alíquota máxima de 5%); art. 7º "
            "(base de cálculo é o preço do serviço)."
        ),
        "explanation": (
            "Nenhum município pode cobrar ISS efetivo abaixo de 2% sobre o preço do serviço (exceto construção civil "
            "dos subitens 7.02/7.05 e transporte coletivo municipal), nem acima de 5%."
        ),
        "solution": (
            "Mostrar que, mesmo na alíquota mínima legal de 2%, o ISS devido sobre o preço ofertado supera o "
            "declarado, e comparar a diferença com o lucro. Confirmar a alíquota do município da prestação no "
            "código tributário municipal e anexar ao recurso."
        ),
    },
    "R04": {
        "title": "ISS cobrado em fornecimento de bens",
        "legal_basis": (
            "LC nº 116/2003, art. 1º e lista anexa (o ISS incide sobre os serviços da lista); LC nº 87/1996 "
            "(circulação de mercadorias sujeita ao ICMS)."
        ),
        "explanation": (
            "Fornecimento de mercadorias não é serviço da lista da LC 116/2003. ISS na planilha de um fornecimento "
            "puro indica erro de enquadramento, e o ICMS pode ter ficado de fora."
        ),
        "solution": (
            "Pedir esclarecimento sobre o enquadramento tributário e verificar se o ICMS devido foi considerado no "
            "preço."
        ),
    },
    "R05": {
        "title": "INSS patronal (CPP) omitido ou abaixo de 20%",
        "legal_basis": (
            "Lei nº 8.212/1991, art. 22, I (contribuição patronal de 20% sobre a remuneração) e II (RAT); LC nº "
            "123/2006, art. 13, VI, e art. 18, § 5º-C (no Anexo IV — construção, vigilância, limpeza e conservação — "
            "a CPP não está no DAS e deve ser recolhida à parte)."
        ),
        "explanation": (
            "Empresa do Simples no Anexo IV (limpeza, conservação, vigilância, obras) paga a contribuição patronal "
            "de 20% fora do DAS. Empresa fora do Simples também paga 20%, salvo desoneração da folha comprovada. "
            "Sem essa linha, o custo de mão de obra da planilha está subdimensionado."
        ),
        "solution": (
            "Calcular os 20% sobre a base de encargos da própria planilha (derivada do FGTS de 8%) e comparar com o "
            "lucro declarado. Se a diferença não couber, pedir a desclassificação por inexequibilidade, porque o "
            "ajuste da planilha exigiria aumentar o preço."
        ),
    },
    "R06": {
        "title": "Optante do Simples em cessão de mão de obra vedada",
        "legal_basis": (
            "LC nº 123/2006, art. 17, XII (vedação ao Simples para quem realiza cessão ou locação de mão de obra); "
            "art. 18, § 5º-H (a vedação não alcança as atividades do § 5º-C — Anexo IV); art. 30, II (exclusão "
            "obrigatória do Simples)."
        ),
        "explanation": (
            "Recepção, copeiragem, portaria, motorista, apoio administrativo e serviços parecidos prestados por cessão "
            "de mão de obra não estão no Anexo IV. A empresa que assume esse contrato fica impedida de continuar no "
            "Simples e passa a recolher encargos e tributos do regime normal. A planilha cotada com os benefícios do "
            "Simples não cobre o custo real do contrato."
        ),
        "solution": (
            "Pedir a desclassificação porque o preço foi formado com benefício do Simples que a empresa não pode "
            "usar nesse contrato. Mostrar o custo do INSS patronal de 20% (mais RAT e terceiros) que passa a ser "
            "devido e comparar com o lucro declarado. Subsidiariamente, pedir diligência para a vencedora demonstrar "
            "que o preço suporta o regime normal."
        ),
    },
    "R07": {
        "title": "FGTS diferente de 8%",
        "legal_basis": "Lei nº 8.036/1990, art. 15 (depósito mensal de 8% da remuneração, inclusive para optantes do Simples).",
        "explanation": "O FGTS é de 8% da remuneração para qualquer empregador, inclusive optantes do Simples Nacional.",
        "solution": (
            "Mostrar a diferença entre 8% e o percentual cotado sobre a base de encargos da planilha e comparar com o "
            "lucro declarado. Diferença maior que o lucro sustenta a desclassificação por inexequibilidade."
        ),
    },
    "R08": {
        "title": "Tributos calculados 'por fora' (sem gross-up)",
        "legal_basis": (
            "Lei nº 9.718/1998, art. 3º, e Leis nº 10.637/2002 e 10.833/2003, art. 1º (PIS/COFINS sobre a receita "
            "bruta/faturamento); LC nº 116/2003, art. 7º (ISS sobre o preço do serviço). Como esses tributos incidem "
            "sobre o preço final, que os inclui, o cálculo correto é 'por dentro': Preço = Base ÷ (1 − soma das alíquotas)."
        ),
        "explanation": (
            "A planilha aplicou as alíquotas sobre o subtotal antes dos tributos. O tributo efetivamente devido incide "
            "sobre o preço final, então o valor cotado é menor que o devido."
        ),
        "solution": (
            "Demonstrar o valor correto dos tributos sobre o preço ofertado e a diferença por posto/mês. Sozinho, esse "
            "erro costuma caber no lucro (é sanável); use-o somado aos demais achados da mesma planilha."
        ),
    },
    "R09": {
        "title": "Lucro Real com alíquota efetiva de PIS/COFINS muito baixa",
        "legal_basis": (
            "Leis nº 10.637/2002 e 10.833/2003 (regime não cumulativo com créditos, alíquotas nominais 1,65% e 7,6%). "
            "Alíquota efetiva menor que a nominal só se sustenta se comprovada pela escrituração (EFD-Contribuições)."
        ),
        "explanation": (
            "No Lucro Real a empresa pode cotar alíquota efetiva menor por causa dos créditos, mas precisa comprovar. "
            "Alíquota efetiva abaixo até do regime cumulativo (3,65%) é incomum em serviços de mão de obra, que geram "
            "poucos créditos."
        ),
        "solution": (
            "Pedir diligência para a vencedora apresentar a EFD-Contribuições dos últimos meses e a memória de cálculo "
            "da alíquota efetiva. Sem comprovação, aplicar as alíquotas nominais e comparar a diferença com o lucro."
        ),
    },
    "R99": {
        "title": "Erros somados superam o lucro declarado",
        "legal_basis": RECURSO_BASIS,
        "explanation": (
            "Cada erro desta planilha, isolado, caberia no lucro. Somados, a correção é maior que o lucro declarado, "
            "o que impede o ajuste da planilha sem aumentar o preço."
        ),
        "solution": (
            "Apresentar todos os achados juntos no recurso, com a soma das diferenças por posto/mês, e pedir a "
            "desclassificação por inexequibilidade."
        ),
    },
}
