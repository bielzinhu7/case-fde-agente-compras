"""API do agente de compras: POST /requisicoes atende um pedido; GET /requisicoes lista as gravadas."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from app import agente, db, llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.criar_tabelas()
    yield


app = FastAPI(title="Agente de compras", lifespan=lifespan)


class Pedido(BaseModel):
    pedido: str = Field(
        min_length=3, max_length=2000, examples=["Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."]
    )


@app.post("/requisicoes")
async def criar_requisicao(entrada: Pedido, response: Response) -> dict:
    pedido = entrada.pedido.strip()
    try:
        resultado = await agente.atender(pedido)
    except agente.CatalogoIndisponivel as e:
        raise HTTPException(
            503, f"Não consegui consultar o catálogo de produtos agora ({e}). Tente de novo em alguns instantes."
        ) from e
    except llm.ModeloIndisponivel as e:
        raise HTTPException(502, f"O modelo de linguagem não respondeu ({e}).") from e
    except agente.AgenteSemResposta as e:
        raise HTTPException(502, f"Não consegui montar a requisição ({e}). Tente reformular o pedido.") from e
    except agente.AgenteDemorou as e:
        raise HTTPException(504, f"O pedido demorou demais e foi interrompido ({e}). Tente de novo.") from e
    except Exception as e:
        # Bug ou falha não prevista: o log guarda o rastro e o chat recebe uma frase, não "Internal Server Error".
        log.exception("erro inesperado ao atender o pedido")
        raise HTTPException(500, "Erro inesperado ao montar a requisição. O detalhe está no log do backend.") from e

    corpo = {
        "numero": None,
        "status": resultado.status,  # criada, parcial ou nao_atendida
        "mensagem": resultado.mensagem,
        "itens": [
            {
                "produto_id": i.produto_id,
                "titulo": i.titulo,
                "quantidade": i.quantidade,
                "preco_unitario": i.preco_unitario,
                "subtotal": i.subtotal,
            }
            for i in resultado.itens
        ],
        "faltou": resultado.faltou,
        "total": resultado.total,
        "ferramentas": resultado.ferramentas,
    }
    if resultado.itens:
        corpo["numero"] = await db.salvar(pedido, resultado, llm.MODELO)
        response.status_code = 201
    return corpo


@app.get("/requisicoes")
async def listar_requisicoes() -> list[dict]:
    return await db.listar()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
