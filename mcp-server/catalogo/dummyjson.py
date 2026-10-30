"""Cliente da DummyJSON: a única parte do servidor que fala HTTP com o catálogo."""

import os

import httpx

BASE_URL = os.getenv("DUMMYJSON_URL", "https://dummyjson.com")
TIMEOUT = float(os.getenv("DUMMYJSON_TIMEOUT", "5"))

# Só o que o agente precisa para escolher; o detalhe completo vem de detalhar().
CAMPOS_BUSCA = "title,category,brand,price,stock,availabilityStatus,minimumOrderQuantity"


class CatalogoIndisponivel(Exception):
    """A DummyJSON não respondeu a tempo, recusou a conexão ou devolveu erro."""


class ProdutoNaoEncontrado(Exception):
    """O id pedido não existe no catálogo."""


async def _get(caminho: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
            resp = await client.get(caminho, params=params)
    except httpx.TimeoutException as e:
        raise CatalogoIndisponivel(f"a DummyJSON não respondeu em {TIMEOUT:g}s") from e
    except httpx.HTTPError as e:
        raise CatalogoIndisponivel(f"não foi possível conectar à DummyJSON ({type(e).__name__})") from e

    if resp.status_code == 404:
        raise ProdutoNaoEncontrado(caminho)
    if resp.status_code >= 400:
        raise CatalogoIndisponivel(f"a DummyJSON respondeu HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise CatalogoIndisponivel("a DummyJSON devolveu uma resposta que não é JSON") from e


async def buscar(termo: str, limite: int) -> dict:
    return await _get("/products/search", {"q": termo, "limit": limite, "select": CAMPOS_BUSCA})


async def detalhar(produto_id: int) -> dict:
    return await _get(f"/products/{produto_id}")
