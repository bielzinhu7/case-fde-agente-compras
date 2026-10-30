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

Abra <http://localhost:3000> — o botão "Experimente" manda o pedido de exemplo. A API também responde direto:

```bash
curl -X POST localhost:8000/requisicoes -H "Content-Type: application/json" \
     -d '{"pedido": "Preciso de 3 laptops para o time de vendas, ate US$ 2.000 cada."}'
curl localhost:8000/requisicoes
```

Testes, sem chave de LLM e sem rede:

```bash
docker compose run --rm --no-deps mcp pytest       # as duas ferramentas do MCP
docker compose run --rm --no-deps backend pytest   # conferência e laço do agente, com modelo roteirizado
```

Com tudo no ar, `docker compose exec backend python avaliacao.py` manda 5 pedidos ao modelo de verdade e
mostra quantos ele acertou. Para ver o erro de catálogo fora do ar no chat:
`DUMMYJSON_TIMEOUT=0.01 docker compose up -d mcp` (e `docker compose up -d mcp` para voltar).

## Como funciona

```
chat :3000 ──/api──▶ backend :8000 ──▶ OpenRouter (modelo)
React + nginx        FastAPI      ├──▶ mcp :8000 (Streamable HTTP) ──▶ DummyJSON
                                  └──▶ Postgres
```

O backend passa ao modelo o pedido e as ferramentas do MCP. O modelo decide o que chamar
(`buscar_produtos`, `detalhar_produto`) e termina chamando `responder` com os ids e as quantidades. O
backend confere a proposta e grava.

## Decisões

- **O modelo propõe, o código confere.** Preço, título e estoque de cada item vêm do que as ferramentas
  devolveram naquela conversa, nunca do texto do modelo, e o total é calculado no código. Proposta com
  produto não consultado, sem estoque ou acima do teto volta para o modelo corrigir
  (`conferir_itens`, em `backend/app/agente.py`).
- **A busca da DummyJSON é literal:** "laptops" devolve 0 produtos e "laptop" devolve 5. A descrição de
  `buscar_produtos` e o prompt pedem termo curto, em inglês e no singular, e sinônimos antes de desistir.
- **Escolha:** entre os produtos que atendem, o de menor preço, o mesmo para todas as unidades. Vale o
  preço de lista; o desconto que a DummyJSON informa não é aplicado.
- **Atendimento parcial:** item que não existe fica de fora e o resto é gravado; quantidade sem estoque
  não é reduzida. Sem nenhum item, nada é gravado (HTTP 200, `status: nao_atendida`); com item, HTTP 201
  e o número da requisição.
- **Erros:** DummyJSON fora do ar ou lenta (timeout de 5 s) → 503 com mensagem clara no chat; OpenRouter
  ou chave → 502. As ferramentas devolvem códigos estáveis (`CATALOGO_INDISPONIVEL`,
  `PRODUTO_NAO_ENCONTRADO`) para o backend não depender do texto do erro.
- **MCP em container próprio**, por Streamable HTTP sem estado. O SDK só liga a proteção contra DNS
  rebinding quando escuta em localhost; aqui ela fica ligada e aceita só os hosts de `MCP_ALLOWED_HOSTS`.
- **Banco:** as tabelas são criadas na subida do backend (`CREATE TABLE IF NOT EXISTS`). Dinheiro é
  `NUMERIC` no banco e texto na API.
- **Modelo:** `anthropic/claude-haiku-5.5`, barato e bom com ferramentas; troca em `OPENROUTER_MODEL`.
  Sem framework de agentes: o laço é curto e fica à vista.

Ficou de fora o diferencial `criar_requisicao`: o requisito pede que o backend grave, e o agente já decide
quando encerrar ao chamar `responder`. Passar a gravação para o MCP poria duas peças escrevendo no banco,
sem ganho na garantia de não inventar. Também não há conversa com várias mensagens (cada pedido é
atendido de uma vez), autenticação nem deploy. Diferenciais feitos: ferramentas chamadas visíveis no chat,
o script dos 5 pedidos e CI com lint e testes.

## Uso de IA

Fiz este case em par com o Claude Code (modelo Claude Opus 5.5, da Anthropic). Ele escreveu o código, os
testes e este README; por isso os commits trazem `Co-Authored-By: Claude`.

As decisões foram minhas: pedi primeiro uma análise do PDF e dos dados da DummyJSON antes de qualquer
código, escolhi Python, defini a ordem de trabalho (uma versão mínima de ponta a ponta antes de refinar),
decidi o escopo seguindo o documento (o `criar_requisicao` ficou de fora) e a regra do atendimento
parcial. Cada etapa foi testada de ponta a ponta antes do commit; os problemas que apareceram no caminho,
como a busca literal da DummyJSON e erros que só surgiram com tudo rodando junto, estão nos commits.
