"""Saúde de cada dependência: banco, servidor MCP com a DummyJSON por trás, e a chave da OpenRouter.

Diferente do /health (o processo está de pé?), aqui se confere se um pedido conseguiria ser atendido agora.
"""

import os
import time

import httpx

from app import db, llm
from app.agente import MCP_URL

# O servidor MCP expõe /saude ao lado de /mcp; é ele quem fala com a DummyJSON.
MCP_SAUDE = MCP_URL.rsplit("/mcp", 1)[0] + "/saude"
# Endpoint da OpenRouter que diz se a chave vale, quanto crédito resta e quando expira (não gasta crédito).
OPENROUTER_CHAVE = "https://openrouter.ai/api/v1/key"
CACHE_CHAVE_S = 60  # o chat consulta a cada 30 s; a chave não muda tão rápido

_cache_chave: tuple[float, dict] | None = None


async def _medir(verificacao) -> dict:
    inicio = time.monotonic()
    try:
        resultado = {"ok": True, **(await verificacao() or {})}
    except Exception as e:  # noqa: BLE001 - qualquer falha aqui é informação, não erro do /saude
        resultado = {"ok": False, "erro": str(e) or type(e).__name__}
    resultado["ms"] = round((time.monotonic() - inicio) * 1000)
    return resultado


async def _banco() -> None:
    await db.ping()


async def _catalogo() -> None:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(MCP_SAUDE)
    except httpx.HTTPError as e:
        raise RuntimeError(f"o servidor MCP não respondeu ({type(e).__name__})") from e
    if resp.status_code != 200:
        try:
            erro = resp.json().get("dummyjson", {}).get("erro")
        except ValueError:
            erro = None
        raise RuntimeError(erro or f"o servidor MCP respondeu HTTP {resp.status_code}")


async def _modelo() -> dict:
    """A chave existe, vale e tem crédito? Consulta a OpenRouter no máximo uma vez por minuto."""
    global _cache_chave
    chave = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise RuntimeError("a OPENROUTER_API_KEY não está configurada no .env")
    if _cache_chave and time.monotonic() - _cache_chave[0] < CACHE_CHAVE_S:
        return _resultado_da_chave(_cache_chave[1])

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(OPENROUTER_CHAVE, headers={"Authorization": f"Bearer {chave}"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"a OpenRouter não respondeu ({type(e).__name__})") from e
    if resp.status_code == 401:
        raise RuntimeError("a OpenRouter recusou a chave (inválida ou expirada)")
    if resp.status_code != 200:
        raise RuntimeError(f"a OpenRouter respondeu HTTP {resp.status_code} ao conferir a chave")
    dados = resp.json().get("data", {})
    _cache_chave = (time.monotonic(), dados)
    return _resultado_da_chave(dados)


def _resultado_da_chave(dados: dict) -> dict:
    restante = dados.get("limit_remaining")
    if restante is not None and restante <= 0:
        raise RuntimeError("a chave da OpenRouter está sem crédito")
    return {"modelo": llm.MODELO, "credito_restante_usd": restante, "expira_em": dados.get("expires_at")}


async def verificar() -> tuple[bool, dict]:
    partes = {
        "banco": await _medir(_banco),
        "catalogo": await _medir(_catalogo),
        "modelo": await _medir(_modelo),
    }
    return all(p["ok"] for p in partes.values()), partes
