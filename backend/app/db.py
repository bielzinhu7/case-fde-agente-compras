"""Postgres: cria as tabelas na subida e grava/lista as requisições."""

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
    mensagem   TEXT           NOT NULL,
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
-- Colunas que chegaram depois: ALTER também atualiza um volume criado por uma versão anterior.
ALTER TABLE requisicoes ADD COLUMN IF NOT EXISTS status TEXT  NOT NULL DEFAULT 'criada';
ALTER TABLE requisicoes ADD COLUMN IF NOT EXISTS faltou JSONB NOT NULL DEFAULT '[]';
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
            log.warning("Postgres ainda não aceita conexão (tentativa %d/%d)", n, tentativas)
            await asyncio.sleep(2)


async def salvar(pedido: str, resultado: Resultado, modelo: str) -> int:
    async with await _conectar() as con, con.transaction():
        cur = await con.execute(
            "INSERT INTO requisicoes (pedido, mensagem, total, modelo, status, faltou)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (pedido, resultado.mensagem, resultado.total.quantize(CENTAVO), modelo, resultado.status,
             Jsonb(resultado.faltou)),
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
