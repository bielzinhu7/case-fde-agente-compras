"""Postgres: cria as tabelas na subida, grava as requisições e o rastro de cada atendimento."""

import asyncio
import logging
import os
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.agente import Resultado

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://compras:compras@postgres:5432/compras")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS requisicoes (
    id         SERIAL PRIMARY KEY,
    pedido     TEXT           NOT NULL,
    status     TEXT           NOT NULL CHECK (status IN ('criada', 'parcial')),
    mensagem   TEXT           NOT NULL,
    faltou     JSONB          NOT NULL DEFAULT '[]',
    total      NUMERIC(12, 2) NOT NULL,
    modelo     TEXT           NOT NULL,
    criada_em  TIMESTAMPTZ    NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS itens_requisicao (
    id              SERIAL PRIMARY KEY,
    requisicao_id   INTEGER        NOT NULL REFERENCES requisicoes (id) ON DELETE CASCADE,
    produto_id      INTEGER        NOT NULL,
    titulo          TEXT           NOT NULL,
    quantidade      INTEGER        NOT NULL CHECK (quantidade > 0),
    preco_unitario  NUMERIC(12, 2) NOT NULL,
    subtotal        NUMERIC(12, 2) NOT NULL
);
-- Um registro por pedido feito ao agente, inclusive os não atendidos e os que deram erro.
-- pedido_id é o mesmo id dos logs e do código de erro mostrado no chat.
CREATE TABLE IF NOT EXISTS atendimentos (
    id              SERIAL PRIMARY KEY,
    pedido_id       TEXT           NOT NULL,
    pedido          TEXT           NOT NULL,
    status          TEXT           NOT NULL CHECK (status IN ('criada', 'parcial', 'nao_atendida', 'erro')),
    status_http     INTEGER        NOT NULL,
    mensagem        TEXT           NOT NULL,
    requisicao_id   INTEGER        REFERENCES requisicoes (id),
    ferramentas     JSONB          NOT NULL DEFAULT '[]',
    tokens_entrada  INTEGER        NOT NULL DEFAULT 0,
    tokens_saida    INTEGER        NOT NULL DEFAULT 0,
    custo_usd       NUMERIC(12, 6) NOT NULL DEFAULT 0,
    duracao_ms      INTEGER        NOT NULL,
    modelo          TEXT           NOT NULL,
    criado_em       TIMESTAMPTZ    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS atendimentos_pedido_id ON atendimentos (pedido_id);
"""

CENTAVO = Decimal("0.01")


async def _conectar() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row)


async def criar_tabelas(tentativas: int = 15) -> None:
    """Roda na subida do backend; espera o Postgres aceitar conexão."""
    for n in range(1, tentativas + 1):
        try:
            async with await _conectar() as con:
                await con.execute(ESQUEMA)
            return
        except psycopg.OperationalError:
            if n == tentativas:
                raise
            log.warning("postgres_aguardando", extra={"tentativa": n, "de": tentativas})
            await asyncio.sleep(2)


async def salvar(pedido: str, resultado: Resultado, modelo: str) -> int:
    async with await _conectar() as con, con.transaction():
        cur = await con.execute(
            "INSERT INTO requisicoes (pedido, status, mensagem, faltou, total, modelo)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (pedido, resultado.status, resultado.mensagem, Jsonb(resultado.faltou),
             resultado.total.quantize(CENTAVO), modelo),
        )
        numero = (await cur.fetchone())["id"]
        async with con.cursor() as c:
            await c.executemany(
                "INSERT INTO itens_requisicao (requisicao_id, produto_id, titulo, quantidade, preco_unitario, subtotal)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (numero, i.produto_id, i.titulo, i.quantidade, i.preco_unitario, i.subtotal.quantize(CENTAVO))
                    for i in resultado.itens
                ],
            )
    return numero


async def listar(limite: int = 100) -> list[dict]:
    async with await _conectar() as con:
        cur = await con.execute(
            """
            SELECT r.id AS numero, r.status, r.pedido, r.mensagem, r.faltou, r.total, r.modelo, r.criada_em,
                   -- dinheiro como texto, igual ao POST: número em JSON perderia a precisão do NUMERIC
                   json_agg(json_build_object(
                       'produto_id', i.produto_id, 'titulo', i.titulo, 'quantidade', i.quantidade,
                       'preco_unitario', i.preco_unitario::text, 'subtotal', i.subtotal::text
                   ) ORDER BY i.id) AS itens
            FROM requisicoes r
            JOIN itens_requisicao i ON i.requisicao_id = r.id
            GROUP BY r.id
            ORDER BY r.id DESC
            LIMIT %s
            """,
            (limite,),
        )
        return await cur.fetchall()


async def registrar_atendimento(rastro: dict) -> None:
    async with await _conectar() as con:
        await con.execute(
            """
            INSERT INTO atendimentos (pedido_id, pedido, status, status_http, mensagem, requisicao_id, ferramentas,
                                      tokens_entrada, tokens_saida, custo_usd, duracao_ms, modelo)
            VALUES (%(pedido_id)s, %(pedido)s, %(status)s, %(status_http)s, %(mensagem)s, %(requisicao_id)s,
                    %(ferramentas)s, %(tokens_entrada)s, %(tokens_saida)s, %(custo_usd)s, %(duracao_ms)s, %(modelo)s)
            """,
            {**rastro, "ferramentas": Jsonb(rastro["ferramentas"])},
        )


async def listar_atendimentos(limite: int = 100) -> list[dict]:
    async with await _conectar() as con:
        cur = await con.execute(
            """
            SELECT pedido_id, criado_em, pedido, status, status_http, mensagem, requisicao_id AS numero,
                   ferramentas, tokens_entrada, tokens_saida, custo_usd::text AS custo_usd, duracao_ms, modelo
            FROM atendimentos
            ORDER BY id DESC
            LIMIT %s
            """,
            (limite,),
        )
        return await cur.fetchall()
