# Agente de compras

A pessoa escreve o que precisa num chat. Um modelo, pela OpenRouter, consulta o catálogo da
[DummyJSON](https://dummyjson.com/docs/products) por duas ferramentas MCP e propõe os itens; o backend confere a
proposta e grava a requisição no Postgres.

```text
chat :3000 ──/api──▶ backend :8000 ──▶ OpenRouter (modelo)
React + nginx        FastAPI      ├──▶ mcp :8000 (Streamable HTTP) ──▶ DummyJSON
                                  └──▶ Postgres
```

## Como rodar

Precisa de Docker com Compose; a primeira subida leva 1 a 2 minutos.

```bash
cp .env.example .env        # coloque a chave da OpenRouter em OPENROUTER_API_KEY
docker compose up --build
```

Abra <http://localhost:3000>. O chat mostra a requisição e as ferramentas que o agente chamou; o botão
"Requisições" lista as gravadas. Pela API, no bash ou no PowerShell:

```bash
curl -X POST localhost:8000/requisicoes -H "Content-Type: application/json" \
     -d '{"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."}'
```

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/requisicoes -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."}'))
```

`POST /requisicoes` devolve 201 com a requisição, ou 200 com `status: nao_atendida` e o que faltou.
`GET /requisicoes` lista as gravadas; `GET /atendimentos`, todo pedido, inclusive os que deram erro, com tokens e
custo; `GET /saude`, o estado do banco, do catálogo e da chave (validade e crédito). Dinheiro vai como texto
(`"3299.97"`). Porta ocupada: troque `CHAT_PORT`, `API_PORT` ou `MCP_PUBLIC_PORT` no `.env`.

## Testes

```bash
docker compose run --rm --no-deps mcp pytest       # ferramentas do MCP, sem chave e sem rede
docker compose run --rm --no-deps backend pytest   # conferência, agente e API, com modelo roteirizado
docker run --rm -v "${PWD}:/src" -w /src python:3.13-slim sh -c "pip install -q ruff==0.17.3 && ruff check mcp-server backend"
```

No Git Bash, ponha `MSYS_NO_PATHCONV=1` antes do `docker run` do lint. Com tudo no ar,
`docker compose exec backend python avaliacao.py` manda 5 pedidos ao modelo de verdade e mostra os acertos. Para
ver o catálogo fora do ar: `DUMMYJSON_TIMEOUT=0.01 docker compose up -d mcp` (no PowerShell,
`$env:DUMMYJSON_TIMEOUT="0.01"` antes); para voltar, `docker compose up -d mcp` (no PowerShell, depois de
`Remove-Item Env:DUMMYJSON_TIMEOUT`).

## Decisões

- **O modelo propõe, o código confere.** O modelo entende o pedido, decide quais ferramentas chamar e termina
  chamando `responder` com ids e quantidades. Preço, título e total vêm do que as ferramentas devolveram, nunca
  do texto do modelo. Produto não consultado, estoque, quantidade mínima, teto e orçamento são conferidos em
  `conferir_itens` (`backend/app/agente.py`); teto e quantidade, também contra o texto do pedido; os valores
  citados na mensagem, contra o catálogo, o pedido e o total. O que não passa volta ao modelo com o motivo.
- **Escolha:** o mais barato que atende, o mesmo para todas as unidades, comparado dentro da mesma busca e
  categoria. Se o pedido exige outro produto (marca, modelo), o modelo diz por quê em `motivo_escolha`, que não é
  conferido.
- **Busca literal:** na DummyJSON, "laptops" devolve 0 e "laptop" devolve 5. A ferramenta e o prompt pedem termo
  curto, em inglês e no singular.
- **Parcial e moeda:** item que não existe fica de fora e o resto é gravado (`status: parcial`); a quantidade nunca
  muda. O catálogo está em US$ e não há câmbio confiável: pedido em reais volta pedindo o valor em dólares.
- **Erros:** catálogo ou banco fora → 503; OpenRouter → 502 com o motivo; acima de 120 s → 504. Todo erro sai em
  JSON com um código, que se acha no log: `docker compose logs backend | grep <código>`. Para o laço não acabar
  sem resposta, consulta repetida não volta ao catálogo e, no último dos 12 passos, o modelo só pode encerrar.
- **Ambiente:** portas só em `127.0.0.1`, porque a API gasta a chave e não tem autenticação. O MCP usa Streamable
  HTTP sem estado, com a proteção contra DNS rebinding ligada para os hosts de `MCP_ALLOWED_HOSTS`, e também
  atende em `localhost:8001/mcp`. Modelo `anthropic/claude-haiku-5.5` (`OPENROUTER_MODEL`), sem framework de
  agentes: o laço está em `_conversar`.
- **Fora do escopo:** o diferencial `criar_requisicao`, porque passar a gravação para o MCP poria duas peças
  escrevendo no banco, sem ganho na garantia de não inventar; conversa com várias mensagens; autenticação; deploy.
  Diferenciais feitos: ferramentas chamadas visíveis no chat, o script dos 5 pedidos e o CI com lint e testes
  (`.github/workflows/ci.yml`), que rodou verde no GitHub Actions.

## Uso de IA

Fiz o case em par com o Claude Code (Claude Opus 5.5, da Anthropic). O Claude escreveu o código, os testes e este
README e propôs a maior parte das soluções técnicas; por isso os commits trazem `Co-Authored-By: Claude`. Da minha
parte vieram o escopo, a escolha entre as opções e estas decisões:

- antes de qualquer código, pedi uma análise do PDF e dos dados da DummyJSON; foi assim que apareceu a busca
  literal;
- escolhi Python e Docker e a regra do atendimento parcial;
- pedi sugestões de usabilidade e de fluxo e escolhi o que entrava: chat com cara de chat, painel de requisições,
  "Tentar de novo", conversa salva, consultas em paralelo e o status `parcial`;
- para acompanhar falhas, pedi logs e aviso de erro; ficaram os logs com o código do pedido, `/atendimentos` e
  `/saude`;
- pedi quatro avaliações pelos critérios do case, cada uma feita por uma sessão do Claude sem acesso à nossa
  conversa, e escolhi o que corrigir de cada uma;
- pedi para conferir se cada mudança continuava dentro do que o case pede; foi assim que vimos que a conferência
  de quantidade travava "meia dúzia".

Pedidos testados com o modelo e o catálogo de verdade:

| Pedido | Resultado |
|---|---|
| O exemplo do case | 201: 3 Lenovo Yoga 920, US$ 3.299,97 |
| "Meia dúzia de smartphones e 2 carregadores" | 201 parcial: 6 smartphones; o carregador exige no mínimo 14 |
| "3 notebooks até R$ 3.000 cada" | não atendida: pede o valor em dólares |
| "2 laptops acima de US$ 1.500 cada" | 201: o mais barato acima do piso |
| "30 MacBook Pro" | não atendida: há 24 em estoque |
| Catálogo fora do ar (`DUMMYJSON_TIMEOUT=0.01`) | 503: "Não consegui consultar o catálogo de produtos agora" |
| Banco parado | 503 nas listagens, com o código do pedido |
