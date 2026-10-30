"""Chamada à OpenRouter, que fala o formato de chat da OpenAI. Sem SDK: um POST e o tratamento de erro."""

import contextvars
import logging
import os
import time

import anyio
import httpx

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/v1/chat/completions"
MODELO = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
TIMEOUT = float(os.getenv("OPENROUTER_TIMEOUT", "30"))
TENTATIVAS = 3  # para erro temporário: limite de taxa (429) e falha do provedor (5xx)


# Soma de tokens e custo das chamadas ao modelo do pedido atual (para o rastro no banco).
_uso: contextvars.ContextVar[dict | None] = contextvars.ContextVar("uso_do_pedido", default=None)


def medir_uso() -> dict:
    """Começa a somar tokens e custo das chamadas ao modelo deste pedido; devolve o acumulador."""
    acumulado = {"tokens_entrada": 0, "tokens_saida": 0, "custo_usd": 0.0}
    _uso.set(acumulado)
    return acumulado


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


async def completar(mensagens: list[dict], ferramentas: list[dict], obrigatoria: str | None = None) -> dict:
    """Devolve a mensagem do assistente. `tool_choice=required`: o modelo sempre chama uma ferramenta; com
    `obrigatoria`, só pode chamar aquela."""
    chave = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise ModeloIndisponivel("a OPENROUTER_API_KEY não está configurada no .env")

    corpo = {
        "model": MODELO,
        "messages": mensagens,
        "tools": ferramentas,
        "tool_choice": {"type": "function", "function": {"name": obrigatoria}} if obrigatoria else "required",
        "temperature": 0,
        "usage": {"include": True},  # a OpenRouter devolve tokens e custo da chamada
    }
    inicio = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for tentativa in range(1, TENTATIVAS + 1):
            try:
                resp = await client.post(URL, headers={"Authorization": f"Bearer {chave}"}, json=corpo)
            except httpx.TimeoutException as e:
                raise ModeloIndisponivel(f"a OpenRouter não respondeu em {TIMEOUT:g}s") from e
            except httpx.HTTPError as e:
                raise ModeloIndisponivel(f"não foi possível conectar à OpenRouter ({type(e).__name__})") from e
            if (resp.status_code == 429 or resp.status_code >= 500) and tentativa < TENTATIVAS:
                log.warning("modelo_nova_tentativa", extra={"status_http": resp.status_code, "tentativa": tentativa})
                await anyio.sleep(2**tentativa)
                continue
            break

    if resp.status_code == 401:
        raise ModeloIndisponivel("a OpenRouter recusou a chave (OPENROUTER_API_KEY inválida ou expirada)")
    if resp.status_code == 402:
        raise ModeloIndisponivel("a chave da OpenRouter está sem crédito")
    if resp.status_code >= 400:
        motivo = _motivo(resp)
        log.error("modelo_recusou", extra={"status_http": resp.status_code, "modelo": MODELO, "motivo": motivo})
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
    uso = dados.get("usage") or {}
    acumulado = _uso.get()
    if acumulado is not None:
        acumulado["tokens_entrada"] += uso.get("prompt_tokens") or 0
        acumulado["tokens_saida"] += uso.get("completion_tokens") or 0
        acumulado["custo_usd"] += uso.get("cost") or 0
    log.info("modelo", extra={
        "modelo": MODELO,
        "duracao_ms": round((time.monotonic() - inicio) * 1000),
        "tokens_entrada": uso.get("prompt_tokens"),
        "tokens_saida": uso.get("completion_tokens"),
        "custo_usd": uso.get("cost"),
        "pediu": [c["function"]["name"] for c in msg.get("tool_calls") or []],
    })
    # Só o que a próxima chamada precisa; campos extras de alguns provedores ficam de fora.
    return {"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
