"""Chamada à OpenRouter, que fala o formato de chat da OpenAI. Sem SDK: um POST e o tratamento de erro."""

import os

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"
MODELO = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "60"))


class ModeloIndisponivel(Exception):
    """A OpenRouter não respondeu, recusou a chave ou devolveu erro."""


async def completar(mensagens: list[dict], ferramentas: list[dict]) -> dict:
    """Devolve a mensagem do assistente. `tool_choice=required`: o modelo sempre chama uma ferramenta."""
    chave = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise ModeloIndisponivel("a OPENROUTER_API_KEY não está configurada no .env")

    corpo = {
        "model": MODELO,
        "messages": mensagens,
        "tools": ferramentas,
        "tool_choice": "required",
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(URL, headers={"Authorization": f"Bearer {chave}"}, json=corpo)
    except httpx.TimeoutException as e:
        raise ModeloIndisponivel(f"a OpenRouter não respondeu em {TIMEOUT:g}s") from e
    except httpx.HTTPError as e:
        raise ModeloIndisponivel(f"não foi possível conectar à OpenRouter ({type(e).__name__})") from e

    if resp.status_code == 401:
        raise ModeloIndisponivel("a OpenRouter recusou a chave (OPENROUTER_API_KEY inválida ou expirada)")
    if resp.status_code == 402:
        raise ModeloIndisponivel("a chave da OpenRouter está sem crédito")
    if resp.status_code >= 400:
        raise ModeloIndisponivel(f"a OpenRouter respondeu HTTP {resp.status_code}")

    dados = resp.json()
    # A OpenRouter às vezes devolve 200 com o erro do provedor no corpo.
    if "error" in dados or not dados.get("choices"):
        raise ModeloIndisponivel(f"a OpenRouter devolveu erro: {dados.get('error', {}).get('message', 'sem resposta')}")
    msg = dados["choices"][0]["message"]
    # Só o que a próxima chamada precisa; campos extras de alguns provedores ficam de fora.
    return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
