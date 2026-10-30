"""O agente: o modelo escolhe as ferramentas do MCP; o código confere a proposta e monta a requisição.

Preço, título e estoque de cada item vêm do que as ferramentas devolveram nesta conversa, nunca do
texto do modelo. Uma proposta com produto não consultado, sem estoque, abaixo do mínimo ou acima do
teto do item volta para o modelo corrigir.
"""

import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from decimal import Decimal

import anyio
from mcp import Client
from pydantic import BaseModel, Field, ValidationError

from app import llm

log = logging.getLogger(__name__)

MCP_URL = os.getenv("MCP_URL", "http://mcp:8000/mcp")
MAX_PASSOS = int(os.getenv("AGENTE_MAX_PASSOS", "12"))
# Abaixo dos 180 s do nginx: o pedido termina com mensagem clara em vez de ser cortado no meio.
PRAZO = float(os.getenv("AGENTE_PRAZO", "120"))

PROMPT = """Você é um agente de compras corporativas. A pessoa descreve o que precisa e você monta uma \
requisição de compra com produtos do catálogo.

Como trabalhar:
- Use buscar_produtos para achar candidatos. O catálogo está em inglês e a busca é literal: use termos \
curtos, em inglês e no singular ("laptop", não "laptops"). Se a busca vier vazia, tente o singular ou um \
sinônimo antes de concluir que o produto não existe.
- A busca já traz preço, estoque e quantidade mínima: use isso para comparar os candidatos. Chame \
detalhar_produto só para os produtos que você vai propor, para confirmar antes de fechar.
- Quando precisar de várias consultas que não dependem uma da outra (buscas de itens diferentes, detalhes \
de produtos diferentes), peça todas na mesma resposta: elas rodam juntas.
- Respeite a quantidade e o orçamento do pedido. Os preços estão em US$; "até US$ X cada" é um teto por \
unidade, e um preço igual ao teto atende.
- Se mais de um produto atender, escolha o de menor preço e use o mesmo produto para todas as unidades. O \
sistema confere: se houver opção mais barata da mesma categoria que atenda, a proposta volta. Se o pedido \
exige aquele produto (marca, modelo ou característica citada), diga isso em motivo_escolha.
- Nunca invente produto, preço ou estoque: use só o que as ferramentas devolveram.
- Se o pedido citar um produto ou uma marca específica, não troque por outro.
- Se um dos itens pedidos não existir no catálogo ou não couber no orçamento, proponha os outros e diga o \
que ficou de fora. Se nenhum produto adequado tiver estoque para a quantidade pedida, não reduza a \
quantidade: deixe esse item de fora e explique. Se nada atender, não proponha itens e diga com clareza o que \
faltou.

Para terminar, chame responder com os itens escolhidos (lista vazia se nada atende). Em cada item, ponha em \
teto_unitario o teto por unidade que o pedido deu para aquele item; omita se o pedido não deu teto. Em \
faltou, liste cada coisa pedida que ficou de fora e o motivo (não existe no catálogo, passa do teto, falta \
estoque); deixe vazio se nada faltou. A \
mensagem é em português, de no máximo duas frases: o que foi escolhido e por quê, ou o que faltou. Na \
mensagem, afirme só o que as ferramentas devolveram e não compare com produtos que você não detalhou. Não \
ofereça o que você não pode fazer: cada pedido é atendido de uma vez, sem continuação, e você não fala com \
fornecedores. Não escreva preços nem total: o sistema mostra a tabela com os valores conferidos."""

RESPONDER = {
    "type": "function",
    "function": {
        "name": "responder",
        "description": (
            "Encerra o atendimento com os itens escolhidos (vazio se nada atende) e a mensagem para a pessoa."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "produto_id": {"type": "integer"},
                            "quantidade": {"type": "integer", "minimum": 1},
                            "teto_unitario": {
                                "type": "number",
                                "description": "Teto por unidade que o pedido deu para este item, em US$",
                            },
                            "motivo_escolha": {
                                "type": "string",
                                "description": "Só se o pedido exige este produto em vez do mais barato: o que o "
                                "pedido pediu (marca, modelo, característica)",
                            },
                        },
                        "required": ["produto_id", "quantidade"],
                    },
                },
                "faltou": {
                    "type": "array",
                    "description": "O que o pedido pediu e ficou de fora; vazio se nada faltou",
                    "items": {
                        "type": "object",
                        "properties": {
                            "descricao": {"type": "string", "description": "O que foi pedido, ex.: 2 tratores"},
                            "motivo": {"type": "string", "description": "Por que ficou de fora"},
                        },
                        "required": ["descricao", "motivo"],
                    },
                },
                "mensagem": {"type": "string"},
            },
            "required": ["itens", "mensagem"],
        },
    },
}


class ItemProposto(BaseModel):
    produto_id: int
    quantidade: int = Field(ge=1)
    teto_unitario: Decimal | None = Field(default=None, gt=0)
    motivo_escolha: str | None = None


class Faltante(BaseModel):
    descricao: str
    motivo: str


class Proposta(BaseModel):
    itens: list[ItemProposto]
    faltou: list[Faltante] = []
    mensagem: str


class CatalogoIndisponivel(Exception):
    """O servidor MCP ou a DummyJSON, por trás dele, não respondeu."""


class AgenteSemResposta(Exception):
    """O modelo esgotou os passos sem chegar a uma resposta conferida."""


class AgenteDemorou(Exception):
    """O pedido passou do prazo total."""


@dataclass
class Item:
    produto_id: int
    titulo: str
    quantidade: int
    preco_unitario: Decimal

    @property
    def subtotal(self) -> Decimal:
        return self.preco_unitario * self.quantidade


@dataclass
class Resultado:
    itens: list[Item]
    mensagem: str
    ferramentas: list[dict]
    faltou: list[dict] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((i.subtotal for i in self.itens), Decimal(0))

    @property
    def status(self) -> str:
        if not self.itens:
            return "nao_atendida"
        return "parcial" if self.faltou else "criada"


def registrar_produtos(vistos: dict[int, dict], ferramenta: str, conteudo: dict) -> None:
    """Guarda, por id, o que o catálogo devolveu, para conferir a proposta do modelo."""
    if ferramenta == "buscar_produtos":
        for p in conteudo["produtos"]:
            vistos[p["id"]] = p
    elif ferramenta == "detalhar_produto":
        vistos[conteudo["id"]] = conteudo


def conferir_itens(propostos: list[ItemProposto], vistos: dict[int, dict]) -> tuple[list[Item], list[str]]:
    """Monta os itens a partir do catálogo. Devolve (itens, problemas); com problema, não devolve item."""
    # Linhas repetidas do mesmo produto somam a quantidade e ficam com o menor teto.
    quantidades: dict[int, int] = {}
    tetos: dict[int, Decimal] = {}
    motivos: dict[int, str] = {}
    for p in propostos:
        quantidades[p.produto_id] = quantidades.get(p.produto_id, 0) + p.quantidade
        if p.teto_unitario is not None:
            tetos[p.produto_id] = min(p.teto_unitario, tetos.get(p.produto_id, p.teto_unitario))
        if p.motivo_escolha:
            motivos[p.produto_id] = p.motivo_escolha

    itens, problemas = [], []
    for produto_id, qtd in quantidades.items():
        p = vistos.get(produto_id)
        if p is None:
            problemas.append(f"o produto {produto_id} não foi devolvido por nenhuma ferramenta nesta conversa")
            continue
        preco = Decimal(str(p["preco"]))
        if qtd > p["estoque"]:
            problemas.append(f"{p['titulo']}: {qtd} unidades pedidas, {p['estoque']} em estoque")
        if qtd < p["quantidade_minima"]:
            problemas.append(f"{p['titulo']}: o mínimo por pedido é {p['quantidade_minima']} unidades")
        teto = tetos.get(produto_id)
        if teto is not None and preco > teto:
            problemas.append(f"{p['titulo']}: US$ {preco} passa do teto de US$ {teto} por unidade")
        if produto_id not in motivos:
            barato = _mais_barato_que_atende(p, qtd, teto, vistos)
            if barato:
                problemas.append(
                    f"{p['titulo']}: {barato['titulo']} (US$ {barato['preco']}) também atende e é mais barato. "
                    "Escolha o mais barato ou, se o pedido exige este produto, diga o porquê em motivo_escolha"
                )
        itens.append(Item(produto_id, p["titulo"], qtd, preco))
    return ([] if problemas else itens), problemas


def _mais_barato_que_atende(escolhido: dict, qtd: int, teto: Decimal | None, vistos: dict[int, dict]) -> dict | None:
    """O produto mais barato, entre os vistos na conversa, da mesma categoria e que atende ao item."""
    preco = Decimal(str(escolhido["preco"]))
    candidatos = [
        v for v in vistos.values()
        if v["categoria"] == escolhido["categoria"]
        and Decimal(str(v["preco"])) < preco
        and v["estoque"] >= qtd
        and v["quantidade_minima"] <= qtd
        and (teto is None or Decimal(str(v["preco"])) <= teto)
    ]
    return min(candidatos, key=lambda v: Decimal(str(v["preco"])), default=None)


def _para_openai(ferramenta) -> dict:
    return {
        "type": "function",
        "function": {
            "name": ferramenta.name,
            "description": ferramenta.description or "",
            "parameters": ferramenta.input_schema,
        },
    }


async def atender(pedido: str) -> Resultado:
    # O cliente MCP roda num TaskGroup do anyio: exceção levantada com ele aberto sai embrulhada num
    # ExceptionGroup e escaparia do tratamento da API. Por isso todo erro conhecido é guardado e
    # levantado só depois de o cliente fechar.
    erro: Exception
    async with AsyncExitStack() as pilha:
        try:
            mcp = await pilha.enter_async_context(Client(MCP_URL))
            ferramentas = [_para_openai(t) for t in (await mcp.list_tools()).tools] + [RESPONDER]
        except Exception as e:
            log.exception("falha ao falar com o servidor MCP")
            erro = CatalogoIndisponivel("o servidor MCP do catálogo não respondeu")
            erro.__cause__ = e
        else:
            try:
                with anyio.fail_after(PRAZO):
                    return await _conversar(mcp, ferramentas, pedido)
            except TimeoutError:
                erro = AgenteDemorou(f"o pedido passou de {PRAZO:g} s")
            except (llm.ModeloIndisponivel, CatalogoIndisponivel, AgenteSemResposta) as e:
                erro = e
    raise erro


async def _conversar(mcp: Client, ferramentas: list[dict], pedido: str) -> Resultado:
    chamadas_log: list[dict] = []
    vistos: dict[int, dict] = {}
    mensagens = [{"role": "system", "content": PROMPT}, {"role": "user", "content": pedido}]

    for _ in range(MAX_PASSOS):
        msg = await llm.completar(mensagens, ferramentas)
        mensagens.append(msg)
        if not msg["tool_calls"]:
            # Com tool_choice=required não deveria acontecer; sem proposta, nada é gravado.
            return Resultado([], msg["content"], chamadas_log)

        chamadas = [(c, c["function"]["name"], _argumentos(c)) for c in msg["tool_calls"]]
        # As consultas ao catálogo pedidas de uma vez rodam juntas; a proposta final é conferida depois,
        # já com tudo o que elas devolveram.
        consultas = [(c, nome, args) for c, nome, args in chamadas if nome != "responder" and args is not None]
        retornos = await _consultar_em_paralelo(mcp, consultas)
        conteudos = {c["id"]: _registrar(nome, args, r, vistos, chamadas_log) for (c, nome, args), r in retornos}

        for chamada, nome, args in chamadas:
            if args is None:
                conteudo = "Argumentos inválidos: envie um JSON."
            elif nome == "responder":
                resultado, conteudo = _responder(args, vistos, chamadas_log)
                if resultado:
                    return resultado
            else:
                conteudo = conteudos[chamada["id"]]
            mensagens.append({"role": "tool", "tool_call_id": chamada["id"], "content": conteudo})

    raise AgenteSemResposta(f"o agente não chegou a uma resposta em {MAX_PASSOS} passos")


def _argumentos(chamada: dict) -> dict | None:
    try:
        return json.loads(chamada["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        return None


def _responder(args: dict, vistos: dict[int, dict], chamadas_log: list[dict]) -> tuple[Resultado | None, str]:
    """Confere a proposta final. Devolve (resultado, "") se ela passa, ou (None, motivo) para o modelo corrigir."""
    try:
        proposta = Proposta.model_validate(args)
        itens, problemas = conferir_itens(proposta.itens, vistos)
    except ValidationError as e:
        problemas = [f"argumentos inválidos: {erro['loc']} {erro['msg']}" for erro in e.errors()]

    detalhe = "; ".join(problemas) or None
    chamadas_log.append({"ferramenta": "responder", "argumentos": args, "erro": bool(problemas), "detalhe": detalhe})
    if problemas:
        return None, f"Proposta recusada pela conferência: {detalhe}. Corrija e chame responder de novo."
    faltou = [f.model_dump() for f in proposta.faltou]
    return Resultado(itens, proposta.mensagem, chamadas_log, faltou), ""


async def _consultar_em_paralelo(mcp: Client, consultas: list[tuple]) -> list[tuple]:
    """Chama as ferramentas ao mesmo tempo. Cada tarefa guarda o retorno ou a exceção: nenhuma exceção sai
    do TaskGroup, que a embrulharia num ExceptionGroup. Devolve na ordem pedida."""
    retornos: list = [None] * len(consultas)

    async def consultar(i: int, nome: str, args: dict) -> None:
        try:
            retornos[i] = await mcp.call_tool(nome, args)
        except Exception as e:  # noqa: BLE001 - guardado aqui e tratado em _registrar, fora do TaskGroup
            retornos[i] = e

    async with anyio.create_task_group() as grupo:
        for i, (_, nome, args) in enumerate(consultas):
            grupo.start_soon(consultar, i, nome, args)
    return list(zip(consultas, retornos, strict=True))


def _registrar(nome: str, args: dict, r, vistos: dict[int, dict], chamadas_log: list[dict]) -> str:
    """Registra o retorno de uma ferramenta e devolve o texto que vai para o modelo."""
    if isinstance(r, Exception):
        log.error("falha ao chamar a ferramenta %s", nome, exc_info=r)
        raise CatalogoIndisponivel("o servidor MCP do catálogo parou de responder") from r

    texto = " ".join(getattr(c, "text", "") for c in r.content)
    chamadas_log.append(
        {"ferramenta": nome, "argumentos": args, "erro": r.is_error, "detalhe": texto if r.is_error else None}
    )
    if r.is_error:
        if "CATALOGO_INDISPONIVEL" in texto:
            # Sem catálogo não há o que o modelo possa fazer; para aqui com mensagem clara.
            raise CatalogoIndisponivel(texto.split("CATALOGO_INDISPONIVEL:", 1)[-1].strip())
        return texto

    dados = r.structured_content
    if dados is None:
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError:
            return texto  # sem dado estruturado, nada entra na conferência
    registrar_produtos(vistos, nome, dados)
    return json.dumps(dados, ensure_ascii=False)
