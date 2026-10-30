# Agente de compras

A pessoa escreve o que precisa num chat; um agente consulta o catálogo da
[DummyJSON](https://dummyjson.com/docs/products) por um servidor MCP e devolve uma requisição de
compra salva no Postgres.

## Como rodar

Precisa de Docker com Compose.

```bash
cp .env.example .env        # coloque a chave da OpenRouter em OPENROUTER_API_KEY
docker compose up --build
```

Abra <http://localhost:3000> e escolha uma das sugestões ou escreva um pedido. O botão "Requisições" lista
as salvas, e a conversa continua lá se a página for recarregada. A API também responde direto:

```bash
curl -X POST localhost:8000/requisicoes -H "Content-Type: application/json" \
     -d '{"pedido": "Preciso de 3 laptops para o time de vendas, ate US$ 2.000 cada."}'
curl localhost:8000/requisicoes
```

No PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri localhost:8000/requisicoes -ContentType "application/json" `
  -Body '{"pedido": "Preciso de 3 laptops para o time de vendas, ate US$ 2.000 cada."}'
```

O "ate" vai sem acento de propósito: alguns terminais do Windows corrompem acentos na linha de comando.
O servidor MCP também fica em `http://localhost:8001/mcp`, para chamar as ferramentas direto com qualquer
cliente MCP.

Testes, sem chave de LLM e sem rede:

```bash
docker compose run --rm --no-deps mcp pytest       # as duas ferramentas do MCP
docker compose run --rm --no-deps backend pytest   # conferência, laço do agente e erros da OpenRouter
```

Com tudo no ar, `docker compose exec backend python avaliacao.py` manda 5 pedidos ao modelo de verdade e
mostra quantos ele acertou. Para ver o erro de catálogo fora do ar no chat, suba o MCP com
`DUMMYJSON_TIMEOUT=0.01 docker compose up -d mcp` (no PowerShell: `$env:DUMMYJSON_TIMEOUT="0.01"` antes
do comando) e depois volte com `docker compose up -d mcp`, sem a variável.

## Como funciona

```
chat :3000 ──/api──▶ backend :8000 ──▶ OpenRouter (modelo)
React + nginx        FastAPI      ├──▶ mcp :8001 (Streamable HTTP) ──▶ DummyJSON
                                  └──▶ Postgres
```

O backend passa ao modelo o pedido e as ferramentas do MCP. O modelo decide o que chamar
(`buscar_produtos`, `detalhar_produto`) e termina chamando `responder` com os itens que escolheu. O
backend confere a proposta e grava.

## Decisões

- **O modelo propõe, o código confere** (`conferir_itens`, em `backend/app/agente.py`). O código garante
  que só entra produto devolvido por uma ferramenta naquela conversa, que preço e título vêm do catálogo,
  que estoque, quantidade mínima e o teto de cada item batem, que não havia opção mais barata da mesma
  categoria que atendesse, e calcula o total. Entender o pedido (o quê, quantos, qual teto) é do modelo.
  Proposta recusada volta para ele corrigir, e a recusa aparece no chat.
- **A busca da DummyJSON é literal:** "laptops" devolve 0 produtos e "laptop" devolve 5. A descrição de
  `buscar_produtos` e o prompt pedem termo curto, em inglês e no singular, e sinônimos antes de desistir.
- **Escolha:** entre os produtos que atendem, o de menor preço, o mesmo para todas as unidades. A busca
  já vem do mais barato para o mais caro, e o código confere a regra. Se o pedido exige um produto mais
  caro (marca, modelo), o modelo diz o porquê em `motivo_escolha`. Vale o preço de lista; o desconto que
  a DummyJSON informa não é aplicado.
- **Menos idas e vindas:** a busca já traz preço, estoque e quantidade mínima, então o agente só detalha
  o que vai propor. Consultas pedidas na mesma resposta rodam em paralelo.
- **Atendimento parcial:** item que não existe fica de fora e o resto é gravado, com `status: parcial` e
  a lista `faltou` (o que ficou de fora e o motivo); quantidade sem estoque não é reduzida. Sem nenhum
  item, nada é gravado (HTTP 200, `status: nao_atendida`); com item, HTTP 201 e o número da requisição.
- **Erros com mensagem clara no chat:** DummyJSON fora do ar ou lenta (5 s) → 503; OpenRouter → 502 com
  o motivo dela (chave, crédito, modelo); pedido que passa de 120 s → 504. Argumento malformado do
  modelo não vira erro: volta para ele corrigir.
- **Rede:** as portas ficam só em `127.0.0.1`, porque a API gasta a chave e não tem autenticação. O MCP
  roda em container próprio, por Streamable HTTP sem estado, com a proteção contra DNS rebinding ligada
  para os hosts de `MCP_ALLOWED_HOSTS` (o SDK só a liga sozinho quando escuta em localhost).
- **Banco:** as tabelas são criadas na subida do backend (`CREATE TABLE IF NOT EXISTS`). Dinheiro é
  `NUMERIC` no banco e texto na API.
- **Modelo:** `anthropic/claude-haiku-5.5`, barato e bom com ferramentas; troca em `OPENROUTER_MODEL`.
  Sem framework de agentes: o laço é curto e fica à vista.

Ficou de fora o diferencial `criar_requisicao`: o requisito pede que o backend grave, e o agente já decide
quando encerrar ao chamar `responder`. Passar a gravação para o MCP poria duas peças escrevendo no banco,
sem ganho na garantia de não inventar. Também não há conversa com várias mensagens (cada pedido é
atendido de uma vez), autenticação nem deploy. Diferenciais feitos: ferramentas chamadas visíveis no chat,
o script dos 5 pedidos e CI com lint e testes.

## Problemas comuns

- **"port is already allocated":** troque `CHAT_PORT`, `API_PORT` ou `MCP_PUBLIC_PORT` no `.env`.
- **"a OpenRouter recusou a chave" ou "sem crédito":** a chave do case vale 7 dias. Ponha outra em
  `OPENROUTER_API_KEY` e rode `docker compose up -d`.
- **"Confira o OPENROUTER_MODEL":** o modelo não existe ou não aceita ferramentas com
  `tool_choice=required`.

## Uso de IA

Trabalhei em par com o Claude Code (modelo Claude Opus 5.5, da Anthropic). O Claude escreveu o código, os
testes e a redação deste README, e propôs as soluções técnicas descritas acima; por isso os commits trazem
`Co-Authored-By: Claude`. Eu defini o escopo, escolhi a linguagem e testei o resultado.

Na prática: pedi primeiro uma análise do PDF e dos dados da DummyJSON, antes de qualquer código; escolhi
Python; aprovei a ordem de trabalho (as peças uma a uma até o pedido de exemplo funcionar de ponta a ponta,
depois os refinamentos); decidi seguir o documento no escopo (o `criar_requisicao` ficou de fora) e a regra
do atendimento parcial; e pedi uma avaliação pelos critérios do case, feita por outra sessão do Claude sem
acesso à nossa conversa. Os ajustes que saíram dela estão nos últimos commits.
