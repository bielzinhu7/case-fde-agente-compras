"""O laço do agente com um modelo roteirizado e um servidor MCP de mentira: sem chave e sem rede."""

import json
from typing import Any

import anyio
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
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "teto_unitario": 2000}],
                mensagem="Escolhi o Lenovo Yoga 920, que custa US$ 999."),
    ])

    r = await agente.atender("Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.")

    assert [(i.produto_id, i.quantidade, str(i.preco_unitario)) for i in r.itens] == [(81, 3, "1099.99")]
    assert str(r.total) == "3299.97"
    assert [c["ferramenta"] for c in r.ferramentas] == [
        "buscar_produtos", "buscar_produtos", "detalhar_produto", "responder"
    ]


async def test_produto_inventado_volta_para_o_modelo_corrigir(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("responder", itens=[{"produto_id": 555, "quantidade": 3}], mensagem="ok"),
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 78, "quantidade": 3}], mensagem="ok"),
    ])

    r = await agente.atender("3 laptops")

    assert "recusada pela conferência" in recebidas[1][-1]["content"]
    assert [i.produto_id for i in r.itens] == [78]
    # a recusa fica registrada, para aparecer no chat
    assert r.ferramentas[0]["ferramenta"] == "responder" and r.ferramentas[0]["erro"]
    assert "555" in r.ferramentas[0]["detalhe"]


async def test_argumento_malformado_volta_para_o_modelo_em_vez_de_virar_erro_500(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": "três", "teto_unitario": "US$ 2.000"}],
                mensagem="ok"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "teto_unitario": 2000}], mensagem="ok"),
    ])

    r = await agente.atender("3 laptops até US$ 2.000 cada")

    assert "argumentos inválidos" in recebidas[2][-1]["content"]
    assert [(i.produto_id, i.quantidade) for i in r.itens] == [(81, 3)]


async def test_nada_atende_devolve_sem_itens(monkeypatch, catalogo):
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="tractor"),
        chamada("responder", itens=[], mensagem="O catálogo não tem tratores."),
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


async def test_mcp_que_cai_depois_de_conectar_sai_como_erro_claro(monkeypatch):
    class ClienteQueFalhaAoListar:
        """Cliente MCP de verdade (com o TaskGroup aberto) cujo list_tools falha."""

        def __init__(self):
            self._cliente = Client(catalogo_falso())

        async def __aenter__(self):
            await self._cliente.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._cliente.__aexit__(*exc)

        async def list_tools(self):
            raise RuntimeError("conexão perdida no meio do protocolo")

    monkeypatch.setattr(agente, "Client", lambda url: ClienteQueFalhaAoListar())

    with pytest.raises(agente.CatalogoIndisponivel):
        await agente.atender("3 laptops")


async def test_pedido_que_passa_do_prazo_sai_como_erro_claro(monkeypatch, catalogo):
    async def completar(mensagens, ferramentas):
        await anyio.sleep(5)

    monkeypatch.setattr(llm, "completar", completar)
    monkeypatch.setattr(agente, "PRAZO", 0.2)

    with pytest.raises(agente.AgenteDemorou):
        await agente.atender("3 laptops")
