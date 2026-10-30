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


async def _medir(verificacao) -> dict:
    inicio = time.monotonic()
    try:
        await verificacao()
        resultado = {"ok": True}
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
        raise RuntimeError(resp.json().get("dummyjson", {}).get("erro") or "a DummyJSON não respondeu")


async def verificar() -> tuple[bool, dict]:
    partes = {
        "banco": await _medir(_banco),
        "catalogo": await _medir(_catalogo),
        "modelo": {"ok": bool(os.getenv("OPENROUTER_API_KEY", "").strip()), "modelo": llm.MODELO},
    }
    if not partes["modelo"]["ok"]:
        partes["modelo"]["erro"] = "a OPENROUTER_API_KEY não está configurada no .env"
    return all(p["ok"] for p in partes.values()), partes
