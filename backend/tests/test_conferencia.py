"""A conferência é o que impede o agente de inventar: preço e título vêm do catálogo, não do modelo."""

from decimal import Decimal

from app.agente import ItemProposto, conferir_itens, conferir_valores_citados, registrar_produtos

MACBOOK = {"id": 78, "titulo": "Apple MacBook Pro 14 Inch Space Grey", "categoria": "laptops", "preco": 1999.99,
           "estoque": 24, "quantidade_minima": 1}
HUAWEI = {"id": 80, "titulo": "Huawei Matebook X Pro", "categoria": "laptops", "preco": 1399.99, "estoque": 75,
          "quantidade_minima": 1}
LENOVO = {"id": 81, "titulo": "Lenovo Yoga 920", "categoria": "laptops", "preco": 1099.99, "estoque": 2,
          "quantidade_minima": 1}
REALME = {"id": 128, "titulo": "Realme C35", "categoria": "smartphones", "preco": 149.99, "estoque": 48,
          "quantidade_minima": 3}
VIVO = {"id": 134, "titulo": "Vivo S1", "categoria": "smartphones", "preco": 249.99, "estoque": 50,
        "quantidade_minima": 1}
PAU_DE_SELFIE = {"id": 111, "titulo": "Selfie Stick Monopod", "categoria": "mobile-accessories", "preco": 12.99,
                 "estoque": 11, "quantidade_minima": 1}


def vistos(*produtos):
    v = {}
    registrar_produtos(v, "buscar_produtos", {"produtos": list(produtos)})
    return v


def item(produto_id, quantidade, teto=None, motivo=None, pedida=None):
    return ItemProposto(produto_id=produto_id, quantidade=quantidade, quantidade_pedida=pedida or quantidade,
                        teto_unitario=teto, motivo_escolha=motivo)


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


# --- regra de escolha: o mais barato que atende ------------------------------------------------------


def test_recusa_quando_ha_opcao_mais_barata_da_mesma_categoria():
    """O agente propôs o Huawei dizendo ser o mais barato; o Lenovo custa menos e atende."""
    lenovo_com_estoque = {**LENOVO, "estoque": 40}
    itens, problemas = conferir_itens([item(80, 3, teto=2000)], vistos(MACBOOK, HUAWEI, lenovo_com_estoque))

    assert itens == []
    assert "Lenovo Yoga 920" in problemas[0] and "mais barato" in problemas[0]


def test_motivo_escolha_libera_produto_mais_caro():
    lenovo_com_estoque = {**LENOVO, "estoque": 40}
    itens, problemas = conferir_itens(
        [item(80, 3, teto=2000, motivo="o pedido pediu Huawei")], vistos(HUAWEI, lenovo_com_estoque)
    )

    assert problemas == []
    assert [i.produto_id for i in itens] == [80]


def test_produto_de_outra_categoria_nao_conta_como_opcao():
    """A busca por "smartphone" traz um pau de selfie mais barato; ele não é alternativa a um celular."""
    _, problemas = conferir_itens([item(128, 4, teto=300)], vistos(REALME, PAU_DE_SELFIE))

    assert problemas == []


def test_opcao_mais_barata_sem_estoque_ou_acima_do_teto_nao_conta():
    # Lenovo tem só 2 em estoque: não atende 3 unidades, então o Huawei é o mais barato que atende.
    _, problemas = conferir_itens([item(80, 3, teto=2000)], vistos(HUAWEI, LENOVO))
    assert problemas == []

    # Com teto de US$ 1.000, nenhum laptop atende; o problema é o teto, não a escolha.
    _, problemas = conferir_itens([item(80, 3, teto=1000)], vistos(HUAWEI, {**LENOVO, "estoque": 40}))
    assert len(problemas) == 1 and "teto" in problemas[0]


# --- quantidade pedida ----------------------------------------------------------------------------


def test_recusa_aumentar_a_quantidade_por_causa_do_minimo():
    """Pediu 2 celulares; o Realme exige no mínimo 3. Comprar 3 não é atender o pedido."""
    _, problemas = conferir_itens([item(128, 3, pedida=2)], vistos(REALME))

    assert "pediu 2" in problemas[0]


def test_recusa_reduzir_a_quantidade_por_falta_de_estoque():
    _, problemas = conferir_itens([item(81, 2, pedida=3)], vistos(LENOVO))

    assert "pediu 3" in problemas[0]


# --- valores citados no texto ---------------------------------------------------------------------


def test_valor_citado_tem_que_ser_conhecido():
    conhecidos = {Decimal("1099.99"), Decimal(2000), Decimal("3299.97")}

    assert conferir_valores_citados(["custa US$ 1.099,99, abaixo do teto de US$ 2.000"], conhecidos) == []
    assert conferir_valores_citados(["total de US$ 3,299.97"], conhecidos) == []
    assert conferir_valores_citados(["40 unidades, 6 meses de garantia"], conhecidos) == []
    assert "999" in conferir_valores_citados(["custa US$ 999"], conhecidos)[0]
    assert len(conferir_valores_citados(["sai por R$ 5.500,00"], conhecidos)) == 1


def test_recusa_aponta_a_opcao_que_atende():
    """Sem a sugestão, o modelo chegou a desistir dizendo que nenhum celular aceitava 2 unidades."""
    _, problemas = conferir_itens([item(128, 2, teto=300)], vistos(REALME, VIVO, PAU_DE_SELFIE))

    assert "mínimo por pedido é 3" in problemas[0]
    assert "Opção que atende: Vivo S1 (id 134" in problemas[0]
