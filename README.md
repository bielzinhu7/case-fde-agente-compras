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

Abra <http://localhost:3000>: o chat mostra a requisição criada, com o número, e as ferramentas que o agente
chamou. Pela API, no bash ou no PowerShell:

```bash
curl -X POST localhost:8000/requisicoes -H "Content-Type: application/json" \
     -d '{"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."}'
```

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/requisicoes -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"pedido": "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."}'))
```

`GET /requisicoes` lista as gravadas; `GET /saude` mostra banco, catálogo e chave; `GET /atendimentos`, todo
pedido, com tokens e custo. Porta ocupada: troque `CHAT_PORT`, `API_PORT` ou `MCP_PUBLIC_PORT` no `.env`.

## Testes

```bash
docker compose run --rm --no-deps mcp pytest       # ferramentas do MCP, sem chave e sem rede
docker compose run --rm --no-deps backend pytest   # conferência, agente e API, com modelo roteirizado
docker run --rm -v "${PWD}:/src" -w /src python:3.13-slim sh -c "pip install -q ruff==0.17.3 && ruff check mcp-server backend"
```

No Git Bash, ponha `MSYS_NO_PATHCONV=1` antes do `docker run`. Com tudo no ar,
`docker compose exec backend python avaliacao.py` manda 5 pedidos ao modelo de verdade e mostra os acertos.
Catálogo fora do ar: `DUMMYJSON_TIMEOUT=0.01 docker compose up -d mcp`, e `docker compose up -d mcp` para voltar
(no PowerShell, `$env:DUMMYJSON_TIMEOUT="0.01"` antes e `Remove-Item Env:DUMMYJSON_TIMEOUT` depois).

## Decisões

- **O modelo propõe, o código confere.** O modelo decide quais ferramentas chamar e termina chamando `responder`
  com ids e quantidades. Preço, título e total vêm do catálogo, nunca do texto do modelo; estoque, mínimo, teto,
  orçamento e valores citados na mensagem são conferidos em `backend/app/agente.py`, e o que não passa volta ao
  modelo com o motivo.
- **Não inventa:** o que não existe ou não atende vai para `faltou` com o motivo, e o resto é gravado
  (`status: parcial`). A quantidade nunca muda; valor em reais ou euros volta pedindo dólares, porque não há câmbio.
- **Escolha:** o mais barato que atende, o mesmo para todas as unidades; marca ou modelo pedido vale mais que o
  preço.
- **Busca literal:** na DummyJSON, "laptops" devolve 0 e "laptop" devolve 5; a ferramenta pede termo curto, em
  inglês e no singular.
- **Erros:** catálogo ou banco fora → 503; OpenRouter → 502; acima de 120 s → 504. Sempre com mensagem clara no
  chat e um código que se acha no log (`docker compose logs backend | grep <código>`).
- **Ambiente:** sem framework de agentes (o laço está em `_conversar`), modelo `anthropic/claude-haiku-5.5`
  (`OPENROUTER_MODEL`). Portas só em `127.0.0.1`, porque a API gasta a chave e não tem autenticação.
- **Fora do escopo:** `criar_requisicao`, porque poria duas peças gravando no banco sem ganho na garantia de não
  inventar; conversa com várias mensagens; autenticação. Feitos: ferramentas no chat, script dos 5 pedidos e CI.
- **Limites conhecidos:** a busca da DummyJSON não olha a categoria ("car" não acha os carros), e um valor escrito
  no pedido pode sair na mensagem fora de contexto.

O detalhe de cada regra e os pedidos testados com o modelo de verdade estão em [DECISOES.md](DECISOES.md).

## Uso de IA

Fiz o case em par com o Claude Code (Claude Opus 5.5, da Anthropic). O Claude escreveu o código, os testes e a
documentação e propôs a maior parte das soluções técnicas; por isso os commits trazem `Co-Authored-By: Claude`.
Da minha parte vieram o escopo, a escolha entre as opções e as decisões: Python e Docker, a regra do atendimento
parcial, o que entrava no chat, logs e `/saude` para acompanhar falhas. Para revisar, pedi quatro avaliações a
sessões do Claude sem acesso à nossa conversa e duas baterias com o modelo de verdade (34 pedidos variados e 38
casos de canto, com gabarito tirado do catálogo), e escolhi o que corrigir de cada uma. O passo a passo está em
[DECISOES.md](DECISOES.md#uso-de-ia).
