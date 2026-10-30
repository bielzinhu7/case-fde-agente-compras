"""O laço do agente com um modelo roteirizado e um servidor MCP de mentira: sem chave e sem rede."""

import json
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app import agente, llm

pytestmark = pytest.mark.anyio

CATALOGO = {
    78: {"id": 78, "titulo": "Apple MacBook Pro 14 Inch Space Grey", "categoria": "laptops", "marca": "Apple",
         "preco": 1999.99, "estoque": 24, "disponibilidade": "In Stock", "quantidade_minima": 1},
    81: {"id": 81, "titulo": "Lenovo Yoga 920", "categoria": "laptops", "marca": "Lenovo",
         "preco": 1099.99, "estoque": 40, "disponibilidade": "In Stock", "quantidade_minima": 1},
}


def catalogo_falso(fora_do_ar: bool = False) -> MCPServer:
    srv = MCPServer("catalogo-falso")

    @srv.tool(structured_output=True)
    def buscar_produtos(termo: str) -> dict[str, Any]:
        if fora_do_ar:
            raise ToolError("CATALOGO_INDISPONIVEL: a DummyJSON não respondeu em 5s")
        achados = [p for p in CATALOGO.values() if termo == "laptop"]
        return {"termo": termo, "total": len(achados), "produtos": achados}

    @srv.tool(structured_output=True)
    def detalhar_produto(id: int) -> dict[str, Any]:
        if id not in CATALOGO:
            raise ToolError(f"PRODUTO_NAO_ENCONTRADO: não existe produto com id {id}")
        return CATALOGO[id]

    return srv


def chamada(nome: str, **args) -> dict:
    return {"id": f"c-{nome}", "type": "function", "function": {"name": nome, "arguments": json.dumps(args)}}


def modelo_roteirizado(monkeypatch, passos: list[dict]) -> list[list[dict]]:
    """Troca a OpenRouter por respostas fixas; devolve as conversas que o modelo recebeu."""
    recebidas = []

    async def completar(mensagens, ferramentas):
        recebidas.append(list(mensagens))
        return {"role": "assistant", "content": "", "tool_calls": [passos.pop(0)]}

    monkeypatch.setattr(llm, "completar", completar)
    return recebidas


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def catalogo(monkeypatch):
    def usar(fora_do_ar: bool = False):
        srv = catalogo_falso(fora_do_ar)
        monkeypatch.setattr(agente, "Client", lambda url: Client(srv))
    usar()
    return usar


async def test_pedido_de_exemplo_vira_requisicao_com_precos_do_catalogo(monkeypatch, catalogo):
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptops"),
        chamada("buscar_produtos", termo="laptop"),
        chamada("detalhar_produto", id=81),
        # o modelo erra o preço no texto; não importa: o valor vem do catálogo
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3}], preco_unitario_maximo=2000,
                mensagem="Escolhi o Lenovo Yoga 920, que custa US$ 999."),
    ])

    r = await agente.atender("Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.")

    assert [(i.produto_id, i.quantidade, str(i.preco_unitario)) for i in r.itens] == [(81, 3, "1099.99")]
    assert str(r.total) == "3299.97"
    assert [c["argumentos"] for c in r.ferramentas] == [{"termo": "laptops"}, {"termo": "laptop"}, {"id": 81}]


async def test_produto_inventado_volta_para_o_modelo_corrigir(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("responder", itens=[{"produto_id": 555, "quantidade": 3}], preco_unitario_maximo=None, mensagem="ok"),
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 78, "quantidade": 3}], preco_unitario_maximo=None, mensagem="ok"),
    ])

    r = await agente.atender("3 laptops")

    assert "recusada pela conferência" in recebidas[1][-1]["content"]
    assert [i.produto_id for i in r.itens] == [78]


async def test_nada_atende_devolve_sem_itens(monkeypatch, catalogo):
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="tractor"),
        chamada("responder", itens=[], preco_unitario_maximo=None, mensagem="O catálogo não tem tratores."),
    ])

    r = await agente.atender("Preciso de 2 tratores")

    assert r.itens == []
    assert r.mensagem == "O catálogo não tem tratores."


async def test_catalogo_fora_do_ar_sai_como_erro_claro_e_nao_como_exception_group(monkeypatch, catalogo):
    catalogo(fora_do_ar=True)
    modelo_roteirizado(monkeypatch, [chamada("buscar_produtos", termo="laptop")])

    with pytest.raises(agente.CatalogoIndisponivel, match="não respondeu em 5s"):
        await agente.atender("3 laptops")


async def test_falha_do_modelo_sai_como_erro_claro_e_nao_como_exception_group(monkeypatch, catalogo):
    async def completar(mensagens, ferramentas):
        raise llm.ModeloIndisponivel("a OPENROUTER_API_KEY não está configurada no .env")

    monkeypatch.setattr(llm, "completar", completar)

    with pytest.raises(llm.ModeloIndisponivel):
        await agente.atender("3 laptops")
