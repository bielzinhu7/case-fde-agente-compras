"""API do agente de compras.

POST /requisicoes atende um pedido; GET /requisicoes lista as requisições gravadas; GET /atendimentos lista o
rastro de cada pedido feito ao agente, inclusive os não atendidos e os que deram erro.
"""

import logging
import time
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import agente, db, llm, registro, saude

registro.configurar()
log = logging.getLogger(__name__)

ERRO_INESPERADO = "Erro inesperado. O detalhe está no log do backend."
BANCO_FORA = "Não consegui ler o banco agora. Tente de novo em alguns instantes."

# Erro conhecido -> (status HTTP, evento no log, mensagem para o chat).
ERROS = [
    (agente.CatalogoIndisponivel, 503, "catalogo_indisponivel",
     "Não consegui consultar o catálogo de produtos agora ({}). Tente de novo em alguns instantes."),
    (llm.ModeloIndisponivel, 502, "modelo_indisponivel", "O modelo de linguagem não respondeu ({})."),
    (agente.AgenteSemResposta, 502, "agente_sem_resposta", "Não consegui montar a requisição ({}). Tente reformular."),
    (agente.AgenteDemorou, 504, "agente_demorou", "O pedido demorou demais e foi interrompido ({}). Tente de novo."),
]


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
        try:
            resposta = await call_next(request)
        except Exception:
            # Última rede: erro não previsto sai em JSON com o código, nunca como "Internal Server Error".
            log.exception("erro_inesperado")
            resposta = JSONResponse({"detail": ERRO_INESPERADO, "codigo": registro.pedido_id.get()}, status_code=500)
        resposta.headers["X-Pedido-Id"] = registro.pedido_id.get()
        # As checagens de saúde rodam a cada poucos segundos: só vão para o log quando falham.
        if request.url.path not in ("/health", "/saude") or resposta.status_code >= 400:
            log.info("http", extra={"metodo": request.method, "caminho": request.url.path,
                                    "status_http": resposta.status_code, "duracao_ms": _ms(inicio)})
        return resposta
    finally:
        registro.pedido_id.reset(token)


@app.exception_handler(StarletteHTTPException)
async def erro_com_codigo(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # O código aparece no chat; com ele se acha o pedido no log e em GET /atendimentos.
    return JSONResponse({"detail": exc.detail, "codigo": registro.pedido_id.get()}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def pedido_invalido(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Sem isto, o chat mostraria só "HTTP 422" para um "oi".
    return JSONResponse(
        {"detail": "Escreva o que precisa comprar, com pelo menos 3 caracteres (no máximo 2.000).",
         "codigo": registro.pedido_id.get()},
        status_code=422,
    )


@app.exception_handler(psycopg.OperationalError)
async def banco_fora(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    # Postgres fora ou recusando conexão nas leituras: é indisponibilidade, não bug. O POST trata a gravação à parte.
    log.error("banco_indisponivel", extra={"motivo": str(exc)})
    return JSONResponse({"detail": BANCO_FORA, "codigo": registro.pedido_id.get()}, status_code=503)


class Pedido(BaseModel):
    pedido: str = Field(
        min_length=3, max_length=2000, examples=["Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada."]
    )


@app.post("/requisicoes")
async def criar_requisicao(entrada: Pedido, response: Response) -> dict:
    pedido = entrada.pedido.strip()
    log.info("pedido_recebido", extra={"pedido": pedido[:300]})
    inicio = time.monotonic()
    uso = llm.medir_uso()
    try:
        resultado = await agente.atender(pedido)
    except Exception as e:
        status_http, _evento, mensagem = _traduzir(e)
        await _gravar_rastro(pedido, "erro", status_http, mensagem, None, [], uso, inicio)
        raise HTTPException(status_http, mensagem) from e

    try:
        numero = await db.salvar(pedido, resultado, llm.MODELO) if resultado.itens else None
    except Exception as e:
        log.exception("banco_indisponivel")
        mensagem = "Montei a requisição, mas não consegui gravá-la no banco agora. Tente de novo em alguns instantes."
        await _gravar_rastro(pedido, "erro", 503, mensagem, None, resultado.ferramentas, uso, inicio)
        raise HTTPException(503, mensagem) from e
    status_http = 201 if numero else 200
    response.status_code = status_http
    await _gravar_rastro(pedido, resultado.status, status_http, resultado.mensagem, numero, resultado.ferramentas,
                         uso, inicio)
    log.info("pedido_concluido", extra={"status": resultado.status, "numero": numero, "total": str(resultado.total),
                                        "itens": len(resultado.itens), "ferramentas": len(resultado.ferramentas),
                                        "custo_usd": round(uso["custo_usd"], 6), "duracao_ms": _ms(inicio)})
    return {
        "numero": numero,
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


@app.get("/requisicoes")
async def listar_requisicoes() -> list[dict]:
    return await db.listar()


@app.get("/atendimentos")
async def listar_atendimentos() -> list[dict]:
    return await db.listar_atendimentos()


@app.get("/health")
async def health() -> dict:
    """O processo está de pé (healthcheck do compose)."""
    return {"status": "ok"}


@app.get("/saude")
async def saude_completa(response: Response) -> dict:
    """Um pedido conseguiria ser atendido agora? 200 se sim; 503 com o detalhe de cada parte se não."""
    ok, partes = await saude.verificar()
    response.status_code = 200 if ok else 503
    return {"status": "ok" if ok else "degradado", **partes}


def _traduzir(erro: Exception) -> tuple[int, str, str]:
    for tipo, status_http, evento, mensagem in ERROS:
        if isinstance(erro, tipo):
            log.error(evento, extra={"motivo": str(erro)})
            return status_http, evento, mensagem.format(erro)
    # Bug ou falha não prevista: o log guarda o rastro e o chat recebe uma frase, não "Internal Server Error".
    log.exception("erro_inesperado")
    return 500, "erro_inesperado", ERRO_INESPERADO


async def _gravar_rastro(pedido: str, status: str, status_http: int, mensagem: str, numero: int | None,
                         ferramentas: list[dict], uso: dict, inicio: float) -> None:
    """Grava o atendimento no banco. Se falhar, o pedido segue: o rastro não pode derrubar a resposta."""
    try:
        await db.registrar_atendimento({
            "pedido_id": registro.pedido_id.get(), "pedido": pedido, "status": status, "status_http": status_http,
            "mensagem": mensagem, "requisicao_id": numero, "ferramentas": ferramentas,
            "tokens_entrada": uso["tokens_entrada"], "tokens_saida": uso["tokens_saida"],
            "custo_usd": Decimal(str(round(uso["custo_usd"], 6))), "duracao_ms": _ms(inicio), "modelo": llm.MODELO,
        })
    except Exception:
        log.exception("rastro_nao_gravado")


def _ms(inicio: float) -> int:
    return round((time.monotonic() - inicio) * 1000)
