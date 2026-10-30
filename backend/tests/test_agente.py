"""O laço do agente com um modelo roteirizado e um servidor MCP de mentira: sem chave e sem rede."""

import itertools
import json
import time
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


def catalogo_falso(fora_do_ar: bool = False, demora: float = 0) -> MCPServer:
    srv = MCPServer("catalogo-falso")

    @srv.tool(structured_output=True)
    def buscar_produtos(termo: str) -> dict[str, Any]:
        if fora_do_ar:
            raise ToolError("CATALOGO_INDISPONIVEL: a DummyJSON não respondeu em 5s")
        achados = [p for p in CATALOGO.values() if termo == "laptop"]
        return {"termo": termo, "total": len(achados), "produtos": achados}

    @srv.tool(structured_output=True)
    async def detalhar_produto(id: int) -> dict[str, Any]:
        await anyio.sleep(demora)
        if id not in CATALOGO:
            raise ToolError(f"PRODUTO_NAO_ENCONTRADO: não existe produto com id {id}")
        return CATALOGO[id]

    return srv


_ids = itertools.count()


def chamada(nome: str, **args) -> dict:
    return {"id": f"c{next(_ids)}", "type": "function", "function": {"name": nome, "arguments": json.dumps(args)}}


def modelo_roteirizado(monkeypatch, passos: list) -> list[list[dict]]:
    """Troca a OpenRouter por respostas fixas; devolve as conversas que o modelo recebeu.
    Cada passo é uma chamada, ou uma lista de chamadas pedidas na mesma resposta."""
    recebidas = []

    async def completar(mensagens, ferramentas):
        recebidas.append(list(mensagens))
        passo = passos.pop(0)
        return {"role": "assistant", "content": "", "tool_calls": passo if isinstance(passo, list) else [passo]}

    monkeypatch.setattr(llm, "completar", completar)
    return recebidas


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def catalogo(monkeypatch):
    def usar(fora_do_ar: bool = False, demora: float = 0):
        srv = catalogo_falso(fora_do_ar, demora)
        monkeypatch.setattr(agente, "Client", lambda url: Client(srv))
    usar()
    return usar


async def test_pedido_de_exemplo_vira_requisicao_com_precos_do_catalogo(monkeypatch, catalogo):
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptops"),
        chamada("buscar_produtos", termo="laptop"),
        chamada("detalhar_produto", id=81),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3, "teto_unitario": 2000}],
                mensagem="Escolhi o Lenovo Yoga 920, o mais barato dentro do teto de US$ 2.000."),
    ])

    r = await agente.atender("Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.")

    assert [(i.produto_id, i.quantidade, str(i.preco_unitario)) for i in r.itens] == [(81, 3, "1099.99")]
    assert str(r.total) == "3299.97"
    assert [c["ferramenta"] for c in r.ferramentas] == [
        "buscar_produtos", "buscar_produtos", "detalhar_produto", "responder"
    ]


async def test_produto_inventado_volta_para_o_modelo_corrigir(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("responder", itens=[{"produto_id": 555, "quantidade": 3, "quantidade_pedida": 3}], mensagem="ok"),
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}], mensagem="ok"),
    ])

    r = await agente.atender("3 laptops")

    assert "recusada pela conferência" in recebidas[1][-1]["content"]
    assert [i.produto_id for i in r.itens] == [81]
    # a recusa fica registrada, para aparecer no chat
    assert r.ferramentas[0]["ferramenta"] == "responder" and r.ferramentas[0]["erro"]
    assert "555" in r.ferramentas[0]["detalhe"]


async def test_argumento_malformado_volta_para_o_modelo_em_vez_de_virar_erro_500(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": "três", "teto_unitario": "US$ 2.000"}],
                mensagem="ok"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3, "teto_unitario": 2000}],
                mensagem="ok"),
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


async def test_consultas_pedidas_juntas_rodam_em_paralelo(monkeypatch, catalogo):
    catalogo(demora=0.5)
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        [chamada("detalhar_produto", id=78), chamada("detalhar_produto", id=81)],  # na mesma resposta
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}], mensagem="ok"),
    ])

    inicio = time.monotonic()
    r = await agente.atender("3 laptops")
    duracao = time.monotonic() - inicio

    # Uma depois da outra levariam 1 s ou mais; juntas, perto de 0,5 s.
    assert duracao < 0.9
    # O registro mantém a ordem pedida, não a ordem em que as respostas chegaram.
    assert [c["argumentos"] for c in r.ferramentas[1:3]] == [{"id": 78}, {"id": 81}]


async def test_erro_numa_consulta_paralela_sai_como_erro_claro(monkeypatch, catalogo):
    catalogo(fora_do_ar=True)
    modelo_roteirizado(monkeypatch, [[chamada("buscar_produtos", termo="laptop"), chamada("detalhar_produto", id=81)]])

    with pytest.raises(agente.CatalogoIndisponivel, match="não respondeu em 5s"):
        await agente.atender("3 laptops")


async def test_o_que_ficou_de_fora_vira_status_parcial(monkeypatch, catalogo):
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3, "teto_unitario": 2000}],
                faltou=[{"descricao": "2 tratores", "motivo": "não existe no catálogo"}],
                mensagem="Os laptops entraram; tratores não há no catálogo."),
    ])

    r = await agente.atender("3 laptops até US$ 2.000 cada e 2 tratores")

    assert r.status == "parcial"
    assert r.faltou == [{"descricao": "2 tratores", "motivo": "não existe no catálogo"}]
    assert [i.produto_id for i in r.itens] == [81]


async def test_status_sem_itens_e_nao_atendida_e_sem_falta_e_criada(monkeypatch, catalogo):
    modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}], mensagem="ok"),
    ])

    assert (await agente.atender("3 laptops")).status == "criada"
    assert agente.Resultado([], "nada", []).status == "nao_atendida"


async def test_escolha_mais_cara_sem_motivo_volta_para_o_modelo(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 78, "quantidade": 3, "quantidade_pedida": 3}],
                mensagem="Escolhi o mais barato."),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}],
                mensagem="Escolhi o mais barato."),
    ])

    r = await agente.atender("3 laptops")

    assert "Lenovo Yoga 920" in recebidas[2][-1]["content"]
    assert [i.produto_id for i in r.itens] == [81]


async def test_preco_inventado_na_mensagem_volta_para_o_modelo(monkeypatch, catalogo):
    """A tabela vem do catálogo, mas o texto também vai para a tela: um preço inventado nele é recusado."""
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}],
                mensagem="Escolhi o Lenovo Yoga 920, que custa US$ 999."),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}],
                mensagem="Escolhi o Lenovo Yoga 920, que custa US$ 1.099,99."),
    ])

    r = await agente.atender("3 laptops")

    assert "999" in recebidas[2][-1]["content"]
    assert r.mensagem.endswith("US$ 1.099,99.")


async def test_quantidade_mudada_por_causa_do_minimo_volta_para_o_modelo(monkeypatch, catalogo):
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 5, "quantidade_pedida": 3}], mensagem="ok"),
        chamada("responder", itens=[{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3}], mensagem="ok"),
    ])

    r = await agente.atender("3 laptops")

    assert "pediu 3" in recebidas[2][-1]["content"]
    assert [i.quantidade for i in r.itens] == [3]


async def test_quantidade_mantida_depois_de_questionada_vale(monkeypatch, catalogo):
    """ "Meia dúzia" com outro número no pedido: a conferência pergunta uma vez e aceita quando o modelo mantém."""
    seis = [{"produto_id": 81, "quantidade": 6, "quantidade_pedida": 6}]
    faltou = [{"descricao": "2 mouses", "motivo": "não existe no catálogo"}]
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=seis, faltou=faltou, mensagem="ok"),
        chamada("responder", itens=seis, faltou=faltou, mensagem="ok"),
    ])

    r = await agente.atender("Preciso de meia dúzia de laptops e 2 mouses")

    assert "não aparece escrita" in recebidas[2][-1]["content"]
    assert [i.quantidade for i in r.itens] == [6]
    assert r.status == "parcial"


async def test_pedido_em_reais_nao_e_convertido_para_dolar(monkeypatch, catalogo):
    tres = [{"produto_id": 81, "quantidade": 3, "quantidade_pedida": 3, "teto_unitario": 3000}]
    faltou = [{"descricao": "3 notebooks até R$ 3.000 cada", "motivo": "o catálogo está em dólares"}]
    recebidas = modelo_roteirizado(monkeypatch, [
        chamada("buscar_produtos", termo="laptop"),
        chamada("responder", itens=tres, mensagem="ok"),
        chamada("responder", itens=[], faltou=faltou, mensagem="Me diga o teto em dólares."),
    ])

    r = await agente.atender("Preciso de 3 notebooks até R$ 3.000 cada")

    assert "em reais" in recebidas[2][-1]["content"]
    assert r.status == "nao_atendida"
