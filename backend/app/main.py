"""API do agente de compras: POST /requisicoes atende um pedido; GET /requisicoes lista as gravadas."""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import agente, db, llm, registro

registro.configurar()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.criar_tabelas()
    yield


app = FastAPI(title="Agente de compras", lifespan=lifespan)


@app.middleware("http")
async def id_do_pedido(request: Request, call_next):
    """Dá um id a cada chamada; ele vai em todo log, no cabeçalho X-Pedido-Id e nas mensagens de erro."""
    token = registro.pedido_id.set(registro.novo_id())
    inicio = time.monotonic()
    try:
        resposta = await call_next(request)
        resposta.headers["X-Pedido-Id"] = registro.pedido_id.get()
        if request.url.path != "/health":  # o healthcheck do compose roda a cada 5 s
            log.info("http", extra={"metodo": request.method, "caminho": request.url.path,
                                    "status_http": resposta.status_code, "duracao_ms": _ms(inicio)})
        return resposta
    finally:
        registro.pedido_id.reset(token)


@app.exception_handler(StarletteHTTPException)
async def erro_com_codigo(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # O código aparece no chat; com ele se acha o pedido no log.
    return JSONResponse({"detail": exc.detail, "codigo": registro.pedido_id.get()}, status_code=exc.status_code)


class Pedido(BaseModel):
    pedido: str = Field(
        min_length=3, max_length=2000, examples=["Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."]
    )


@app.post("/requisicoes")
async def criar_requisicao(entrada: Pedido, response: Response) -> dict:
    pedido = entrada.pedido.strip()
    log.info("pedido_recebido", extra={"pedido": pedido[:300]})
    inicio = time.monotonic()
    try:
        resultado = await agente.atender(pedido)
    except agente.CatalogoIndisponivel as e:
        log.error("catalogo_indisponivel", extra={"motivo": str(e)})
        raise HTTPException(
            503, f"Não consegui consultar o catálogo de produtos agora ({e}). Tente de novo em alguns instantes."
        ) from e
    except llm.ModeloIndisponivel as e:
        log.error("modelo_indisponivel", extra={"motivo": str(e)})
        raise HTTPException(502, f"O modelo de linguagem não respondeu ({e}).") from e
    except agente.AgenteSemResposta as e:
        log.error("agente_sem_resposta", extra={"motivo": str(e)})
        raise HTTPException(502, f"Não consegui montar a requisição ({e}). Tente reformular o pedido.") from e
    except agente.AgenteDemorou as e:
        log.error("agente_demorou", extra={"motivo": str(e)})
        raise HTTPException(504, f"O pedido demorou demais e foi interrompido ({e}). Tente de novo.") from e
    except Exception as e:
        # Bug ou falha não prevista: o log guarda o rastro e o chat recebe uma frase, não "Internal Server Error".
        log.exception("erro_inesperado")
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
    log.info("pedido_concluido", extra={"status": resultado.status, "numero": corpo["numero"],
                                        "total": str(resultado.total), "itens": len(resultado.itens),
                                        "ferramentas": len(resultado.ferramentas), "duracao_ms": _ms(inicio)})
    return corpo


@app.get("/requisicoes")
async def listar_requisicoes() -> list[dict]:
    return await db.listar()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _ms(inicio: float) -> int:
    return round((time.monotonic() - inicio) * 1000)
