"""Servidor MCP do catálogo: expõe buscar_produtos e detalhar_produto sobre a DummyJSON."""

import os
import time
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from catalogo import dummyjson
from catalogo.dummyjson import CatalogoIndisponivel, ProdutoNaoEncontrado

# O catálogo tem perto de 200 produtos e a busca devolve só campos curtos: 50 cabem com folga e evitam que
# um termo amplo ("phone") traga só acessórios baratos.
LIMITE_BUSCA = int(os.getenv("LIMITE_BUSCA", "50"))

mcp = MCPServer(
    name="catalogo",
    instructions="Catálogo de produtos da DummyJSON. Preços em dólares (US$).",
)


class ProdutoResumo(BaseModel):
    id: int
    titulo: str
    categoria: str
    marca: str | None = None
    preco: float = Field(description="Preço de lista, em US$")
    estoque: int = Field(description="Unidades disponíveis")
    disponibilidade: str
    quantidade_minima: int = Field(description="Quantidade mínima por pedido")


class ResultadoBusca(BaseModel):
    termo: str
    total: int = Field(description="Quantos produtos do catálogo casam com o termo")
    produtos: list[ProdutoResumo] = Field(description=f"Os primeiros {LIMITE_BUSCA}, no máximo")


class ProdutoDetalhe(ProdutoResumo):
    descricao: str
    desconto_percentual: float = Field(description="Desconto informado pelo catálogo; o preço acima não o aplica")
    garantia: str | None = None
    envio: str | None = None
    politica_devolucao: str | None = None


def _resumo(p: dict) -> dict:
    return {
        "id": p["id"],
        "titulo": p["title"],
        "categoria": p["category"],
        "marca": p.get("brand"),
        "preco": p["price"],
        "estoque": p["stock"],
        "disponibilidade": p["availabilityStatus"],
        "quantidade_minima": p["minimumOrderQuantity"],
    }


@mcp.tool(
    description=(
        "Busca produtos no catálogo pelo texto do título e da descrição. A busca é literal "
        "(trecho de texto, sem diferenciar maiúsculas) e o catálogo está em inglês: use um termo "
        'curto, em inglês e no singular — "laptop", não "laptops" nem "notebook". Se vier vazio, '
        "tente o singular ou um sinônimo antes de concluir que o catálogo não tem o produto. "
        f"Devolve até {LIMITE_BUSCA} produtos, do mais barato para o mais caro, e o total encontrado."
    )
)
async def buscar_produtos(termo: Annotated[str, Field(description="Termo de busca, em inglês")]) -> ResultadoBusca:
    termo = termo.strip()
    if not termo:
        raise ToolError("TERMO_INVALIDO: informe um termo de busca")
    try:
        dados = await dummyjson.buscar(termo, LIMITE_BUSCA)
    except CatalogoIndisponivel as e:
        raise ToolError(f"CATALOGO_INDISPONIVEL: {e}") from e
    return ResultadoBusca(termo=termo, total=dados["total"], produtos=[_resumo(p) for p in dados["products"]])


@mcp.tool(
    description=(
        "Devolve os detalhes de um produto pelo id: preço, estoque, quantidade mínima, garantia e "
        "envio. Use antes de incluir um produto na requisição, para confirmar preço e estoque."
    )
)
async def detalhar_produto(id: Annotated[int, Field(gt=0, description="id do produto no catálogo")]) -> ProdutoDetalhe:
    try:
        p = await dummyjson.detalhar(id)
    except ProdutoNaoEncontrado as e:
        raise ToolError(f"PRODUTO_NAO_ENCONTRADO: não existe produto com id {id}") from e
    except CatalogoIndisponivel as e:
        raise ToolError(f"CATALOGO_INDISPONIVEL: {e}") from e
    return ProdutoDetalhe(
        **_resumo(p),
        descricao=p["description"],
        desconto_percentual=p["discountPercentage"],
        garantia=p.get("warrantyInformation"),
        envio=p.get("shippingInformation"),
        politica_devolucao=p.get("returnPolicy"),
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Só diz que o processo está de pé; é o healthcheck do compose."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/saude", methods=["GET"])
async def saude(request: Request) -> JSONResponse:
    """Confere a dependência de verdade: a DummyJSON responde?"""
    inicio = time.monotonic()
    try:
        await dummyjson.verificar()
        dummy = {"ok": True}
    except (CatalogoIndisponivel, ProdutoNaoEncontrado) as e:
        dummy = {"ok": False, "erro": str(e)}
    dummy["ms"] = round((time.monotonic() - inicio) * 1000)
    return JSONResponse({"status": "ok" if dummy["ok"] else "falha", "dummyjson": dummy},
                        status_code=200 if dummy["ok"] else 503)


def main() -> None:
    # Fora do localhost a proteção contra DNS rebinding não liga sozinha; liga aqui e libera
    # só os nomes pelos quais o backend chama o servidor (o nome do serviço no compose).
    hosts = os.getenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,mcp:*")
    mcp.run(
        "streamable-http",
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8000")),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        ),
    )


if __name__ == "__main__":
    main()
