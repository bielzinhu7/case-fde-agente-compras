"""A conferência é o que impede o agente de inventar: preço e título vêm do catálogo, não do modelo."""

from decimal import Decimal

from app.agente import conferir_itens, registrar_produtos

MACBOOK = {"id": 78, "titulo": "Apple MacBook Pro 14 Inch Space Grey", "preco": 1999.99, "estoque": 24, "quantidade_minima": 1}
LENOVO = {"id": 81, "titulo": "Lenovo Yoga 920", "preco": 1099.99, "estoque": 2, "quantidade_minima": 1}


def vistos(*produtos):
    v = {}
    registrar_produtos(v, "buscar_produtos", {"produtos": list(produtos)})
    return v


def test_monta_itens_com_preco_do_catalogo():
    itens, problemas = conferir_itens([{"produto_id": 78, "quantidade": 3}], vistos(MACBOOK), teto=2000)

    assert problemas == []
    assert [(i.produto_id, i.titulo, i.quantidade, i.preco_unitario) for i in itens] == [
        (78, "Apple MacBook Pro 14 Inch Space Grey", 3, Decimal("1999.99"))
    ]
    assert itens[0].subtotal == Decimal("5999.97")


def test_preco_igual_ao_teto_atende():
    _, problemas = conferir_itens([{"produto_id": 78, "quantidade": 1}], vistos(MACBOOK), teto=1999.99)

    assert problemas == []


def test_recusa_produto_que_nenhuma_ferramenta_devolveu():
    itens, problemas = conferir_itens([{"produto_id": 999, "quantidade": 1}], vistos(MACBOOK), teto=None)

    assert itens == []
    assert "999" in problemas[0]


def test_recusa_quantidade_acima_do_estoque():
    itens, problemas = conferir_itens([{"produto_id": 81, "quantidade": 3}], vistos(LENOVO), teto=None)

    assert itens == []
    assert "2 em estoque" in problemas[0]


def test_recusa_preco_acima_do_teto():
    itens, problemas = conferir_itens([{"produto_id": 78, "quantidade": 1}], vistos(MACBOOK), teto=1500)

    assert itens == []
    assert "teto" in problemas[0]


def test_soma_linhas_repetidas_do_mesmo_produto_antes_de_conferir_estoque():
    itens, problemas = conferir_itens(
        [{"produto_id": 81, "quantidade": 1}, {"produto_id": 81, "quantidade": 2}], vistos(LENOVO), teto=None
    )

    assert itens == []
    assert "3 unidades pedidas" in problemas[0]


def test_detalhe_mais_recente_prevalece_sobre_a_busca():
    v = vistos(MACBOOK)
    registrar_produtos(v, "detalhar_produto", {**MACBOOK, "estoque": 2})

    _, problemas = conferir_itens([{"produto_id": 78, "quantidade": 3}], v, teto=None)

    assert "2 em estoque" in problemas[0]
