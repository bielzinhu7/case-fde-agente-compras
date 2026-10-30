# Agente de compras

A pessoa escreve o que precisa num chat. O backend (FastAPI) passa o pedido a um modelo, pela OpenRouter, que
consulta o catálogo da [DummyJSON](https://dummyjson.com/docs/products) por duas ferramentas MCP. O código
confere cada proposta antes de gravar a requisição no Postgres. Cada pedido leva de 6 a 10 s e custa de
US$ 0,001 a US$ 0,003 (medido com o modelo padrão).

## Como rodar

Precisa de Docker com Compose; a primeira subida leva 1 a 2 minutos.

```bash
cp .env.example .env        # coloque a chave da OpenRouter em OPENROUTER_API_KEY
docker compose up --build
```

Abra <http://localhost:3000>. A API também responde direto (no PowerShell, use `Invoke-RestMethod` com o
mesmo corpo; o "ate" vai sem acento porque alguns terminais do Windows corrompem acentos):

```bash
curl -X POST localhost:8000/requisicoes -H "Content-Type: application/json" \
     -d '{"pedido": "Preciso de 3 laptops para o time de vendas, ate US$ 2.000 cada."}'
```

| Endpoint | Para quê |
|---|---|
| `POST /requisicoes` | Atende um pedido: 201 com a requisição, ou 200 com `status: nao_atendida` |
| `GET /requisicoes` | Requisições salvas, com itens e o que faltou |
| `GET /atendimentos` | Rastro de todo pedido, inclusive os não atendidos e os com erro |
| `GET /saude` | Banco, MCP, DummyJSON e chave: 200 se tudo responde, 503 com o detalhe se não |

O servidor MCP também fica em `http://localhost:8001/mcp`, para qualquer cliente MCP.

## Testes e lint

```bash
docker compose run --rm --no-deps mcp pytest       # ferramentas do MCP, sem chave e sem rede
docker compose run --rm --no-deps backend pytest   # conferência, agente, API e erros da OpenRouter
pip install ruff==0.17.3 && ruff check mcp-server backend
```

Com tudo no ar, `docker compose exec backend python avaliacao.py` manda 5 pedidos ao modelo de verdade e
mostra quantos ele acertou. Para ver o catálogo "fora do ar": `DUMMYJSON_TIMEOUT=0.01 docker compose up -d mcp`
e, para voltar, `docker compose up -d mcp`. No PowerShell, defina antes `$env:DUMMYJSON_TIMEOUT="0.01"` e,
para voltar, rode `Remove-Item Env:DUMMYJSON_TIMEOUT` antes do mesmo comando.

## Como funciona

```
chat :3000 ──/api──▶ backend :8000 ──▶ OpenRouter (modelo)
React + nginx        FastAPI      ├──▶ mcp :8001 (Streamable HTTP) ──▶ DummyJSON
                                  └──▶ Postgres
```

O modelo decide o que chamar (`buscar_produtos`, `detalhar_produto`) e termina chamando `responder` com os
itens. O backend confere a proposta: se algo não bate, devolve o motivo ao modelo, que corrige.

## Decisões

- **O modelo propõe, o código confere** (`conferir_itens`, em `backend/app/agente.py`).
  - **O código garante:**
    - produto devolvido por uma ferramenta na conversa;
    - preço e título do catálogo;
    - estoque, quantidade mínima, teto do item e orçamento total;
    - quantidade igual à pedida;
    - total calculado;
    - valor citado na mensagem igual ao do catálogo, do pedido ou do total.
  - **Do modelo:** entender o pedido.
- **Escolha:** o mais barato que atende, o mesmo para todas as unidades. A busca vem ordenada por preço e o
  código confere, comparando dentro da categoria da DummyJSON, que é larga. Se o pedido exige outro produto,
  o modelo justifica em `motivo_escolha`, e essa justificativa não é conferida. Vale o preço de lista.
- **A busca da DummyJSON é literal:** "laptops" devolve 0 e "laptop" devolve 5. A ferramenta e o prompt pedem
  termo curto, em inglês e no singular.
- **Parcial:** item que não existe fica de fora e o resto é gravado (`status: parcial`, com a lista `faltou`).
  A quantidade nunca muda, nem por estoque nem pela quantidade mínima.
- **Erros viram mensagem clara:** catálogo fora → 503; OpenRouter → 502 com o motivo; acima de 120 s → 504.
  Consultas que o modelo pede juntas rodam em paralelo.
- **Rede:** portas só em `127.0.0.1`, porque a API gasta a chave e não tem autenticação. O MCP usa Streamable
  HTTP sem estado, com a proteção contra DNS rebinding ligada para os hosts de `MCP_ALLOWED_HOSTS`. O nginx
  resolve o backend a cada pedido.
- **Modelo:** `anthropic/claude-haiku-5.5`, barato e bom com ferramentas (`OPENROUTER_MODEL`). Sem framework
  de agentes: o laço é curto e fica à vista.

**Ficou de fora** o `criar_requisicao`: o requisito pede que o backend grave, e o agente já decide quando
encerrar ao chamar `responder`. Também ficaram de fora conversa com várias mensagens, autenticação e deploy.
**Diferenciais feitos:** ferramentas chamadas no chat, script dos 5 pedidos e CI com lint e testes.

## Acompanhar falhas

- **Logs:** JSON, uma linha por evento, todas com o id do pedido.
  - O id volta no cabeçalho `X-Pedido-Id` e aparece no chat quando dá erro.
  - `docker compose logs backend | grep <id>` conta a história inteira: modelo (tempo, tokens, custo),
    ferramentas, recusas e resultado.
- **`GET /atendimentos`:** guarda o mesmo id no banco.
- **`GET /saude`:** diz o que caiu; o ponto no topo do chat fica verde, amarelo ou vermelho.

**Problemas comuns:**
- "port is already allocated": troque `CHAT_PORT`, `API_PORT` ou `MCP_PUBLIC_PORT` no `.env`.
- Chave recusada ou sem crédito: a do case vale 7 dias. Troque no `.env` e rode `docker compose up -d`.
- "Confira o OPENROUTER_MODEL": o modelo não existe ou não aceita `tool_choice=required`.

## Uso de IA

Trabalhei em par com o Claude Code (Claude Opus 5.5, da Anthropic). O Claude escreveu o código, os testes e a
redação deste README, e propôs as soluções técnicas; por isso os commits trazem `Co-Authored-By: Claude`.

Da minha parte vieram o escopo, as decisões e várias ideias que mudaram o projeto:
- conferir o PDF atrás de armadilhas para IA e analisar os dados antes de qualquer código;
- escolher Python;
- seguir o documento no escopo e definir a regra do atendimento parcial;
- commits pequenos, um por tarefa;
- avaliar o projeto pelos critérios do case. Foram duas rodadas, feitas por outra sessão do Claude sem acesso
  à nossa conversa, e boa parte dos últimos commits saiu delas;
- o chat com cara de chat (painel de requisições, "Tentar de novo", conversa salva);
- melhorar o fluxo (status parcial, consultas em paralelo);
- ter como acompanhar falhas (logs com id, rastro no banco, `/saude`).

Testei o resultado no chat.
