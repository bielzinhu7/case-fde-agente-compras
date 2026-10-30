"""A API por fora: códigos HTTP, o id do pedido no cabeçalho e no erro, e o formato do log.

O agente e o banco são trocados por versões de teste: sem chave, sem rede e sem Postgres.
"""

import json
import logging
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import agente, db, main, registro, saude

PEDIDO = {"pedido": "Preciso de 3 laptops até US$ 2.000 cada"}


@pytest.fixture
def cliente():
    return TestClient(main.app)  # sem "with": não roda o lifespan, então não precisa do Postgres


@pytest.fixture(autouse=True)
def rastros(monkeypatch):
    """O rastro vai para uma lista em vez do banco."""
    gravados = []

    async def registrar_atendimento(rastro):
        gravados.append(rastro)

    monkeypatch.setattr(db, "registrar_atendimento", registrar_atendimento)
    return gravados


def atender_com(monkeypatch, resultado=None, erro=None):
    async def atender(pedido):
        if erro:
            raise erro
        return resultado

    monkeypatch.setattr(agente, "atender", atender)


def test_requisicao_criada_devolve_201_numero_e_id(monkeypatch, cliente, rastros):
    item = agente.Item(81, "Lenovo Yoga 920", 3, Decimal("1099.99"))
    atender_com(monkeypatch, agente.Resultado([item], "ok", []))

    async def salvar(pedido, resultado, modelo):
        return 7

    monkeypatch.setattr(db, "salvar", salvar)

    r = cliente.post("/requisicoes", json=PEDIDO)

    assert r.status_code == 201
    assert r.json()["numero"] == 7 and r.json()["status"] == "criada"
    assert r.json()["total"] == "3299.97"
    assert len(r.headers["X-Pedido-Id"]) == 8
    assert [(x["status"], x["status_http"], x["requisicao_id"], x["pedido_id"]) for x in rastros] == [
        ("criada", 201, 7, r.headers["X-Pedido-Id"])
    ]


def test_nada_atende_devolve_200_sem_gravar_requisicao_mas_com_rastro(monkeypatch, cliente, rastros):
    atender_com(monkeypatch, agente.Resultado([], "Não há tratores no catálogo.", []))

    async def salvar(*_):
        raise AssertionError("não devia gravar")

    monkeypatch.setattr(db, "salvar", salvar)

    r = cliente.post("/requisicoes", json=PEDIDO)

    assert r.status_code == 200
    assert r.json()["numero"] is None and r.json()["status"] == "nao_atendida"
    assert [(x["status"], x["requisicao_id"]) for x in rastros] == [("nao_atendida", None)]


@pytest.mark.parametrize(
    "erro, status_http",
    [
        (agente.CatalogoIndisponivel("a DummyJSON não respondeu em 5s"), 503),
        (agente.AgenteDemorou("o pedido passou de 120 s"), 504),
        (agente.AgenteSemResposta("12 passos"), 502),
        (RuntimeError("bug"), 500),
    ],
)
def test_erro_vira_mensagem_clara_com_o_codigo_do_pedido(monkeypatch, cliente, rastros, erro, status_http):
    atender_com(monkeypatch, erro=erro)

    r = cliente.post("/requisicoes", json=PEDIDO)

    assert r.status_code == status_http
    assert r.json()["detail"]
    assert "bug" not in r.json()["detail"]  # detalhe interno não vaza para o chat
    assert r.json()["codigo"] == r.headers["X-Pedido-Id"]
    assert [(x["status"], x["status_http"], x["pedido_id"]) for x in rastros] == [
        ("erro", status_http, r.json()["codigo"])
    ]


def test_linha_de_log_e_json_com_o_id_do_pedido():
    registro_teste = logging.makeLogRecord(
        {"name": "app.agente", "levelno": logging.INFO, "levelname": "INFO", "msg": "ferramenta",
         "ferramenta": "buscar_produtos", "duracao_ms": 12}
    )
    token = registro.pedido_id.set("a1b2c3d4")
    try:
        linha = json.loads(registro.FormatoJson().format(registro_teste))
    finally:
        registro.pedido_id.reset(token)

    assert linha["pedido_id"] == "a1b2c3d4"
    assert linha["evento"] == "ferramenta"
    assert linha["ferramenta"] == "buscar_produtos" and linha["duracao_ms"] == 12


def test_falha_ao_gravar_o_rastro_nao_derruba_a_resposta(monkeypatch, cliente):
    atender_com(monkeypatch, agente.Resultado([], "nada", []))

    async def banco_fora(rastro):
        raise OSError("banco fora do ar")

    monkeypatch.setattr(db, "registrar_atendimento", banco_fora)

    assert cliente.post("/requisicoes", json=PEDIDO).status_code == 200


def test_atendimentos_lista_o_rastro(monkeypatch, cliente):
    async def listar_atendimentos():
        return [{"pedido_id": "a1b2c3d4", "status": "erro", "status_http": 503}]

    monkeypatch.setattr(db, "listar_atendimentos", listar_atendimentos)

    assert cliente.get("/atendimentos").json()[0]["pedido_id"] == "a1b2c3d4"


def test_saude_503_diz_qual_parte_falhou(monkeypatch, cliente):
    async def banco_ok():
        pass

    async def catalogo_fora():
        raise RuntimeError("a DummyJSON não respondeu: timeout")

    monkeypatch.setattr(saude, "_banco", banco_ok)
    monkeypatch.setattr(saude, "_catalogo", catalogo_fora)
    monkeypatch.setattr(saude, "_modelo", banco_ok)

    r = cliente.get("/saude")

    assert r.status_code == 503
    corpo = r.json()
    assert corpo["status"] == "degradado"
    assert corpo["banco"]["ok"] is True and corpo["modelo"]["ok"] is True
    assert corpo["catalogo"]["ok"] is False
    assert corpo["catalogo"]["erro"] == "a DummyJSON não respondeu: timeout"


def test_saude_200_com_tudo_de_pe(monkeypatch, cliente):
    async def ok():
        pass

    monkeypatch.setattr(saude, "_banco", ok)
    monkeypatch.setattr(saude, "_catalogo", ok)
    monkeypatch.setattr(saude, "_modelo", ok)

    assert cliente.get("/saude").status_code == 200


def test_banco_fora_ao_gravar_vira_503_com_codigo_e_rastro(monkeypatch, cliente, rastros):
    item = agente.Item(81, "Lenovo Yoga 920", 3, Decimal("1099.99"))
    atender_com(monkeypatch, agente.Resultado([item], "ok", []))

    async def banco_fora(*_):
        raise OSError("connection refused")

    monkeypatch.setattr(db, "salvar", banco_fora)

    r = cliente.post("/requisicoes", json=PEDIDO)

    assert r.status_code == 503
    assert "gravá-la no banco" in r.json()["detail"]
    assert r.json()["codigo"] == r.headers["X-Pedido-Id"]
    assert [(x["status"], x["status_http"]) for x in rastros] == [("erro", 503)]


def test_pedido_curto_recebe_mensagem_clara(cliente):
    r = cliente.post("/requisicoes", json={"pedido": "oi"})

    assert r.status_code == 422
    assert "pelo menos 3 caracteres" in r.json()["detail"]
    assert r.json()["codigo"] == r.headers["X-Pedido-Id"]


def test_erro_nao_previsto_sai_em_json_com_codigo(monkeypatch):
    async def quebra():
        raise RuntimeError("bug")

    monkeypatch.setattr(db, "listar", quebra)

    r = TestClient(main.app, raise_server_exceptions=False).get("/requisicoes")

    assert r.status_code == 500
    assert r.json()["codigo"] == r.headers["X-Pedido-Id"]
    assert "bug" not in r.json()["detail"]
