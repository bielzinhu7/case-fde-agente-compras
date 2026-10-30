"""A conferência é o que impede o agente de inventar: preço e título vêm do catálogo, não do modelo."""

from decimal import Decimal

from app.agente import ItemProposto, conferir_itens, registrar_produtos

MACBOOK = {"id": 78, "titulo": "Apple MacBook Pro 14 Inch Space Grey", "preco": 1999.99, "estoque": 24,
           "quantidade_minima": 1}
LENOVO = {"id": 81, "titulo": "Lenovo Yoga 920", "preco": 1099.99, "estoque": 2, "quantidade_minima": 1}
REALME = {"id": 128, "titulo": "Realme C35", "preco": 149.99, "estoque": 48, "quantidade_minima": 3}


def vistos(*produtos):
    v = {}
    registrar_produtos(v, "buscar_produtos", {"produtos": list(produtos)})
    return v


def item(produto_id, quantidade, teto=None):
    return ItemProposto(produto_id=produto_id, quantidade=quantidade, teto_unitario=teto)


def test_monta_itens_com_preco_do_catalogo():
    itens, problemas = conferir_itens([item(78, 3, teto=2000)], vistos(MACBOOK))

    assert problemas == []
    assert [(i.produto_id, i.titulo, i.quantidade, i.preco_unitario) for i in itens] == [
        (78, "Apple MacBook Pro 14 Inch Space Grey", 3, Decimal("1999.99"))
    ]
    assert itens[0].subtotal == Decimal("5999.97")


def test_preco_igual_ao_teto_atende():
    _, problemas = conferir_itens([item(78, 1, teto=Decimal("1999.99"))], vistos(MACBOOK))

    assert problemas == []


def test_cada_item_e_conferido_contra_o_proprio_teto():
    """ "3 laptops até US$ 2.000 e 4 celulares até US$ 300": um teto só para o pedido não serviria."""
    itens, problemas = conferir_itens([item(78, 3, teto=2000), item(128, 4, teto=300)], vistos(MACBOOK, REALME))

    assert problemas == []
    assert [i.produto_id for i in itens] == [78, 128]


def test_recusa_produto_que_nenhuma_ferramenta_devolveu():
    itens, problemas = conferir_itens([item(999, 1)], vistos(MACBOOK))

    assert itens == []
    assert "999" in problemas[0]


def test_recusa_quantidade_acima_do_estoque():
    itens, problemas = conferir_itens([item(81, 3)], vistos(LENOVO))

    assert itens == []
    assert "2 em estoque" in problemas[0]


def test_recusa_quantidade_abaixo_do_minimo():
    itens, problemas = conferir_itens([item(128, 2)], vistos(REALME))

    assert itens == []
    assert "mínimo por pedido é 3" in problemas[0]


def test_recusa_preco_acima_do_teto_do_item():
    itens, problemas = conferir_itens([item(78, 1, teto=1500)], vistos(MACBOOK))

    assert itens == []
    assert "teto" in problemas[0]


def test_soma_linhas_repetidas_do_mesmo_produto_antes_de_conferir_estoque():
    itens, problemas = conferir_itens([item(81, 1), item(81, 2)], vistos(LENOVO))

    assert itens == []
    assert "3 unidades pedidas" in problemas[0]


def test_detalhe_mais_recente_prevalece_sobre_a_busca():
    v = vistos(MACBOOK)
    registrar_produtos(v, "detalhar_produto", {**MACBOOK, "estoque": 2})

    _, problemas = conferir_itens([item(78, 3)], v)

    assert "2 em estoque" in problemas[0]
