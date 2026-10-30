"""Chamada à OpenRouter, que fala o formato de chat da OpenAI. Sem SDK: um POST e o tratamento de erro."""

import logging
import os

import anyio
import httpx

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/v1/chat/completions"
MODELO = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "30"))
TENTATIVAS = 3  # para erro temporário: limite de taxa (429) e falha do provedor (5xx)


class ModeloIndisponivel(Exception):
    """A OpenRouter não respondeu, recusou a chave ou o modelo, ou devolveu erro."""


def _motivo(resp: httpx.Response) -> str:
    """A mensagem de erro que a OpenRouter manda no corpo, que é o que explica a falha."""
    try:
        erro = resp.json().get("error")
    except ValueError:
        return resp.text[:200]
    if isinstance(erro, dict):
        return str(erro.get("message") or erro)[:200]
    return str(erro or "sem detalhe")[:200]


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
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for tentativa in range(1, TENTATIVAS + 1):
            try:
                resp = await client.post(URL, headers={"Authorization": f"Bearer {chave}"}, json=corpo)
            except httpx.TimeoutException as e:
                raise ModeloIndisponivel(f"a OpenRouter não respondeu em {TIMEOUT:g}s") from e
            except httpx.HTTPError as e:
                raise ModeloIndisponivel(f"não foi possível conectar à OpenRouter ({type(e).__name__})") from e
            if (resp.status_code == 429 or resp.status_code >= 500) and tentativa < TENTATIVAS:
                log.warning("OpenRouter respondeu HTTP %d; nova tentativa", resp.status_code)
                await anyio.sleep(2**tentativa)
                continue
            break

    if resp.status_code == 401:
        raise ModeloIndisponivel("a OpenRouter recusou a chave (OPENROUTER_API_KEY inválida ou expirada)")
    if resp.status_code == 402:
        raise ModeloIndisponivel("a chave da OpenRouter está sem crédito")
    if resp.status_code >= 400:
        motivo = _motivo(resp)
        log.error("OpenRouter HTTP %d com o modelo %s: %s", resp.status_code, MODELO, motivo)
        raise ModeloIndisponivel(
            f"a OpenRouter respondeu HTTP {resp.status_code}: {motivo}. Confira o OPENROUTER_MODEL ({MODELO})"
        )

    try:
        dados = resp.json()
    except ValueError as e:
        raise ModeloIndisponivel("a OpenRouter devolveu uma resposta que não é JSON") from e
    # A OpenRouter às vezes devolve 200 com o erro do provedor no corpo.
    if "error" in dados or not dados.get("choices"):
        raise ModeloIndisponivel(f"a OpenRouter devolveu erro: {_motivo(resp)}")
    msg = dados["choices"][0]["message"]
    # Só o que a próxima chamada precisa; campos extras de alguns provedores ficam de fora.
    return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
