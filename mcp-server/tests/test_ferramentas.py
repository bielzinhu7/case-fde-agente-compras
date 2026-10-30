"""Testes das duas ferramentas do servidor MCP.

Chamam as ferramentas pelo protocolo MCP (cliente em processo) com a DummyJSON simulada:
não precisam de chave de LLM nem de rede.
"""

import httpx
import pytest
import respx
from mcp import Client

from catalogo.dummyjson import BASE_URL
from catalogo.servidor import mcp

pytestmark = pytest.mark.anyio

MACBOOK = {
    "id": 78,
    "title": "Apple MacBook Pro 14 Inch Space Grey",
    "description": "The MacBook Pro 14 Inch in Space Grey is a powerful and sleek laptop.",
    "category": "laptops",
    "brand": "Apple",
    "price": 1999.99,
    "discountPercentage": 4.69,
    "stock": 24,
    "availabilityStatus": "In Stock",
    "minimumOrderQuantity": 1,
    "warrantyInformation": "3 year warranty",
    "shippingInformation": "Ships in 2 weeks",
    "returnPolicy": "7 days return policy",
}

RESUMO_MACBOOK = {
    "id": 78,
    "titulo": "Apple MacBook Pro 14 Inch Space Grey",
    "categoria": "laptops",
    "marca": "Apple",
    "preco": 1999.99,
    "estoque": 24,
    "disponibilidade": "In Stock",
    "quantidade_minima": 1,
}

# Como cada falha da DummyJSON é simulada (argumentos de respx Route.mock).
FALHAS = {
    "demora": {"side_effect": httpx.ReadTimeout("timeout")},
    "conexao_recusada": {"side_effect": httpx.ConnectError("connection refused")},
    "http_503": {"return_value": httpx.Response(503)},
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def chamar(ferramenta: str, argumentos: dict):
    async with Client(mcp) as cliente:
        return await cliente.call_tool(ferramenta, argumentos)


def texto(resultado) -> str:
    return " ".join(c.text for c in resultado.content)


async def test_expoe_exatamente_as_duas_ferramentas():
    async with Client(mcp) as cliente:
        ferramentas = {t.name: t for t in (await cliente.list_tools()).tools}
    assert set(ferramentas) == {"buscar_produtos", "detalhar_produto"}
    assert ferramentas["buscar_produtos"].input_schema["required"] == ["termo"]
    assert ferramentas["detalhar_produto"].input_schema["required"] == ["id"]
    assert ferramentas["buscar_produtos"].output_schema is not None


# --- buscar_produtos -------------------------------------------------------------------------


@respx.mock
async def test_buscar_produtos_devolve_os_produtos_resumidos():
    rota = respx.get(f"{BASE_URL}/products/search").mock(
        return_value=httpx.Response(200, json={"products": [MACBOOK], "total": 1, "skip": 0, "limit": 20})
    )
    r = await chamar("buscar_produtos", {"termo": " laptop "})

    assert not r.is_error
    assert r.structured_content == {"termo": "laptop", "total": 1, "produtos": [RESUMO_MACBOOK]}
    params = rota.calls.last.request.url.params
    assert params["q"] == "laptop"
    assert params["limit"] == "20"


@respx.mock
async def test_buscar_produtos_sem_resultado_nao_e_erro():
    respx.get(f"{BASE_URL}/products/search").mock(
        return_value=httpx.Response(200, json={"products": [], "total": 0, "skip": 0, "limit": 20})
    )
    r = await chamar("buscar_produtos", {"termo": "laptops"})

    assert not r.is_error
    assert r.structured_content == {"termo": "laptops", "total": 0, "produtos": []}


@respx.mock
async def test_buscar_produtos_recusa_termo_vazio_sem_chamar_o_catalogo():
    rota = respx.get(f"{BASE_URL}/products/search")
    r = await chamar("buscar_produtos", {"termo": "   "})

    assert r.is_error
    assert "TERMO_INVALIDO" in texto(r)
    assert not rota.called


@pytest.mark.parametrize("falha", FALHAS.values(), ids=FALHAS.keys())
@respx.mock
async def test_buscar_produtos_com_catalogo_fora_do_ar(falha):
    respx.get(f"{BASE_URL}/products/search").mock(**falha)
    r = await chamar("buscar_produtos", {"termo": "laptop"})

    assert r.is_error
    assert "CATALOGO_INDISPONIVEL" in texto(r)


# --- detalhar_produto ------------------------------------------------------------------------


@respx.mock
async def test_detalhar_produto_devolve_preco_estoque_e_condicoes():
    respx.get(f"{BASE_URL}/products/78").mock(return_value=httpx.Response(200, json=MACBOOK))
    r = await chamar("detalhar_produto", {"id": 78})

    assert not r.is_error
    assert r.structured_content == {
        **RESUMO_MACBOOK,
        "descricao": MACBOOK["description"],
        "desconto_percentual": 4.69,
        "garantia": "3 year warranty",
        "envio": "Ships in 2 weeks",
        "politica_devolucao": "7 days return policy",
    }


@respx.mock
async def test_detalhar_produto_inexistente():
    respx.get(f"{BASE_URL}/products/999").mock(
        return_value=httpx.Response(404, json={"message": "Product with id '999' not found"})
    )
    r = await chamar("detalhar_produto", {"id": 999})

    assert r.is_error
    assert "PRODUTO_NAO_ENCONTRADO" in texto(r)


async def test_detalhar_produto_recusa_id_invalido():
    r = await chamar("detalhar_produto", {"id": 0})

    assert r.is_error


@pytest.mark.parametrize("falha", FALHAS.values(), ids=FALHAS.keys())
@respx.mock
async def test_detalhar_produto_com_catalogo_fora_do_ar(falha):
    respx.get(f"{BASE_URL}/products/78").mock(**falha)
    r = await chamar("detalhar_produto", {"id": 78})

    assert r.is_error
    assert "CATALOGO_INDISPONIVEL" in texto(r)
