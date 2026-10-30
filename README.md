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

Abra <http://localhost:3000>. A API também responde direto:

```bash
curl -X POST localhost:8000/requisicoes -H "Content-Type: application/json" \
     -d '{"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."}'
```

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/requisicoes -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."}'))
```

| Endpoint | Para quê |
|---|---|
| `POST /requisicoes` | Atende um pedido: 201 com a requisição, ou 200 com `status: nao_atendida` |
| `GET /requisicoes` | Requisições salvas, com itens e o que faltou |
| `GET /atendimentos` | Rastro de todo pedido, inclusive os não atendidos e os com erro |
| `GET /saude` | Banco, MCP, DummyJSON e chave (validade e crédito): 200, ou 503 com o que caiu |

O MCP também fica em `http://localhost:8001/mcp`, para qualquer cliente MCP. Para ver o banco:
`docker compose exec postgres psql -U compras -c "select * from requisicoes"`. Para zerar: `docker compose down -v`.

## Testes e lint

```bash
docker compose run --rm --no-deps mcp pytest       # uma ou mais por ferramenta do MCP, sem chave e sem rede
docker compose run --rm --no-deps backend pytest   # conferência, agente, API, saúde e erros da OpenRouter
docker run --rm -v "${PWD}:/src" -w /src python:3.13-slim sh -c "pip install -q ruff==0.17.3 && ruff check mcp-server backend"
```

Com tudo no ar, `docker compose exec backend python avaliacao.py` manda 5 pedidos ao modelo de verdade e mostra
quantos ele acertou. Para ver o catálogo "fora do ar": `DUMMYJSON_TIMEOUT=0.01 docker compose up -d mcp`
(no PowerShell, `$env:DUMMYJSON_TIMEOUT="0.01"` antes). Para voltar: `docker compose up -d mcp`, depois de
`Remove-Item Env:DUMMYJSON_TIMEOUT` no PowerShell.

## O que o case pede e onde está

| Pedido no case | Onde |
|---|---|
| `docker compose up` sobe as quatro peças | `docker-compose.yml`: `mcp`, `backend`, `postgres`, `chat` |
| MCP com `buscar_produtos(termo)` e `detalhar_produto(id)` na DummyJSON | `mcp-server/catalogo/servidor.py` |
| `POST /requisicoes` (OpenRouter, MCP e Postgres) e `GET /requisicoes` | `backend/app/main.py`, `agente.py`, `db.py` |
| Tabelas criadas quando o ambiente sobe | `db.py`, chamado na subida do backend |
| O modelo decide quando chamar cada ferramenta | `_conversar` em `agente.py`: o código só executa o que o modelo pede |
| Não inventa; se nada atende, diz o que falta | conferência em `agente.py`; `status: nao_atendida` com a lista `faltou` |
| DummyJSON falha ou demora → mensagem clara no chat | timeout de 5 s no MCP → 503 com a mensagem, mostrada no chat |
| Um teste por ferramenta do MCP, sem chave de LLM | `mcp-server/tests/test_ferramentas.py` |
| Chat em React com o número da requisição | `chat/src/App.jsx` |

**Diferenciais:**
- feitos: ferramentas chamadas no chat, script dos 5 pedidos e CI com lint e testes (`.github/workflows/ci.yml`). Os
  passos do CI foram rodados localmente; o repositório ainda não tem remote.
- fora: o `criar_requisicao`. O requisito pede que o backend grave, e o agente já decide quando encerrar ao chamar
  `responder`.

**Além do pedido**, sem mudar nada do que foi pedido:
- conferências extras e o status `parcial`;
- logs com id do pedido, `/atendimentos` e `/saude`;
- no chat: painel de requisições, "Tentar de novo" e conversa salva.

## Decisões

- **O modelo propõe, o código confere.**
  - **O código garante:**
    - produto devolvido por uma ferramenta na conversa;
    - preço e título do catálogo;
    - estoque e quantidade mínima;
    - teto do item e orçamento total, com o teto conferido contra o valor escrito no pedido;
    - quantidade igual à pedida. A que não aparece escrita no pedido ("meia dúzia", "2 para cada um dos 3") é
      questionada uma vez e vale se o modelo a mantiver;
    - total calculado no código;
    - valores citados na mensagem iguais aos do catálogo, do pedido ou do total.
  - **Fica com o modelo:** entender o pedido e escolher.
- **Escolha:** o mais barato que atende, o mesmo para todas as unidades.
  - A busca vem ordenada por preço, e o código compara dentro da mesma busca e categoria.
  - Se o pedido exige outro produto, o modelo justifica em `motivo_escolha`, e essa justificativa não é conferida.
  - Vale o preço de lista.
- **Busca literal:** na DummyJSON, "laptops" devolve 0 e "laptop" devolve 5. A ferramenta e o prompt pedem termo
  curto, em inglês e no singular.
- **Moeda:** o catálogo está em US$. Valor em reais não é convertido, porque não há câmbio confiável: o agente pede
  o valor em dólares.
- **Parcial:** item que não existe fica de fora e o resto é gravado (`status: parcial`). A quantidade nunca muda.
- **Erros:** catálogo fora → 503; OpenRouter → 502 com o motivo; acima de 120 s → 504. Todo erro leva o código do
  pedido.
- **Rede:** portas só em `127.0.0.1`, porque a API gasta a chave e não tem autenticação. O MCP usa Streamable HTTP
  sem estado, com a proteção contra DNS rebinding ligada para os hosts de `MCP_ALLOWED_HOSTS`.
- **Modelo:** `anthropic/claude-haiku-5.5`, barato e bom com ferramentas (`OPENROUTER_MODEL`). Sem framework de
  agentes: o laço está em `_conversar`, em `agente.py`.

## Acompanhar falhas

- **Logs:** JSON, uma linha por evento, com o id do pedido. O id volta em `X-Pedido-Id` e aparece no chat quando
  dá erro. `docker compose logs backend | grep <id>` mostra modelo (tempo, tokens, custo), ferramentas, recusas e
  resultado.
- **`GET /atendimentos`:** guarda o mesmo id.
- **`GET /saude`:** diz o que caiu; o ponto no topo do chat fica verde, amarelo ou vermelho.

**Problemas comuns:**
- "port is already allocated": troque `CHAT_PORT`, `API_PORT` ou `MCP_PUBLIC_PORT` no `.env`.
- Chave recusada, expirada ou sem crédito: o `/saude` mostra. Troque no `.env` e rode `docker compose up -d`.
- "Confira o OPENROUTER_MODEL": o modelo não existe ou não aceita `tool_choice=required`.

## Uso de IA

Trabalhei em par com o Claude Code (Claude Opus 5.5, da Anthropic). O Claude escreveu o código, os testes e a
redação deste README, e propôs as soluções técnicas; por isso os commits trazem `Co-Authored-By: Claude`.

Da minha parte vieram o escopo, as decisões e várias ideias que mudaram o projeto:
- conferir o PDF atrás de armadilhas para IA e analisar os dados antes de qualquer código;
- seguir o documento no escopo e conferir se cada mudança continuava dentro do que o case pede;
- definir a regra do atendimento parcial;
- commits pequenos, conforme o trabalho avançava;
- avaliar o projeto pelos critérios do case, em três rodadas feitas por outra sessão do Claude sem acesso à nossa
  conversa;
- o chat com cara de chat;
- melhorias no fluxo;
- ter como acompanhar falhas.

Testei o resultado no chat.
