"""O agente: o modelo escolhe as ferramentas do MCP; o código confere a proposta e monta a requisição.

Preço, título e estoque de cada item vêm do que as ferramentas devolveram nesta conversa, nunca do
texto do modelo. Uma proposta com produto não consultado, sem estoque, abaixo do mínimo ou acima do
teto do item volta para o modelo corrigir.
"""

import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

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
unidade, e um preço igual ao teto atende. Um orçamento para o pedido inteiro ("US$ 5.000 no total") limita a \
soma dos itens. Se o pedido der o valor em reais, não converta (aqui não há câmbio): não proponha itens, diga \
em faltou que o valor precisa vir em dólares e, se quiser, cite os preços em US$ que encontrou.
- Se mais de um produto atender, escolha o de menor preço e use o mesmo produto para todas as unidades. O \
sistema confere: se houver opção mais barata da mesma categoria que atenda, a proposta volta. Se o pedido \
exige aquele produto (marca, modelo ou característica citada), diga isso em motivo_escolha.
- Nunca invente produto, preço ou estoque: use só o que as ferramentas devolveram.
- Se o pedido citar um produto ou uma marca específica, não troque por outro.
- Nunca mude a quantidade pedida: nem reduza por falta de estoque nem aumente por causa da quantidade \
mínima do produto. Se um produto não serve para aquela quantidade, escolha outro que sirva ou deixe o item \
de fora.
- Se um dos itens pedidos não existir no catálogo ou não couber no orçamento, proponha os outros e diga o \
que ficou de fora. Se nada atender, não proponha itens e diga com clareza o que faltou.

Para terminar, chame responder com os itens escolhidos (lista vazia se nada atende). Em cada item, ponha em \
quantidade_pedida a quantidade que a pessoa pediu, e em teto_unitario o teto por unidade que o pedido deu \
para aquele item (omita se não deu; piso e preço de referência, como "acima de US$ 1.000" ou "o último custou \
US$ 2.500", não são teto). Se o pedido deu um orçamento para o pedido inteiro, ponha em teto_total. \
Em faltou, liste cada coisa pedida que ficou de fora e o motivo (não \
existe no catálogo, passa do teto, falta estoque, quantidade mínima maior que a pedida); deixe vazio se nada \
faltou. A mensagem é em português, de no máximo duas frases: o que foi escolhido e por quê, ou o que faltou. \
Na mensagem, afirme só o que as ferramentas devolveram e não compare com produtos que você não detalhou. Não \
ofereça o que você não pode fazer: cada pedido é atendido de uma vez, sem continuação, e você não fala com \
fornecedores. Evite repetir preços e total, que a tabela já mostra; se citar um valor, ele tem que ser \
exatamente um preço do catálogo ou um valor do pedido: o sistema confere."""

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
                            "quantidade_pedida": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "A quantidade que a pessoa pediu deste item",
                            },
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
                        "required": ["produto_id", "quantidade", "quantidade_pedida"],
                    },
                },
                "teto_total": {
                    "type": "number",
                    "description": "Orçamento para o pedido inteiro, em US$, se o pedido deu um",
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
    quantidade_pedida: int = Field(ge=1)
    teto_unitario: Decimal | None = Field(default=None, gt=0)
    motivo_escolha: str | None = None


class Faltante(BaseModel):
    descricao: str
    motivo: str


class Proposta(BaseModel):
    itens: list[ItemProposto]
    teto_total: Decimal | None = Field(default=None, gt=0)
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
    """Guarda, por id, o que o catálogo devolveu, para conferir a proposta do modelo.

    `_termos` diz de quais buscas o produto veio: alternativas só se comparam dentro da mesma busca, porque a
    categoria da DummyJSON é larga demais (maçã e leite são os dois "groceries").
    """
    if ferramenta == "buscar_produtos":
        for p in conteudo["produtos"]:
            termos = vistos.get(p["id"], {}).get("_termos", set()) | {conteudo.get("termo", "")}
            vistos[p["id"]] = {**p, "_termos": termos}
    elif ferramenta == "detalhar_produto":
        vistos[conteudo["id"]] = {**conteudo, "_termos": vistos.get(conteudo["id"], {}).get("_termos", set())}


def conferir_itens(
    propostos: list[ItemProposto], vistos: dict[int, dict], teto_total: Decimal | None = None
) -> tuple[list[Item], list[str]]:
    """Monta os itens a partir do catálogo. Devolve (itens, problemas); com problema, não devolve item."""
    # Linhas repetidas do mesmo produto somam a quantidade e ficam com o menor teto.
    quantidades: dict[int, int] = {}
    tetos: dict[int, Decimal] = {}
    motivos: dict[int, str] = {}
    problemas = []
    for p in propostos:
        if p.quantidade != p.quantidade_pedida:
            visto = vistos.get(p.produto_id)
            exigido = bool(p.motivo_escolha)
            saida = _sugestao(visto, p.quantidade_pedida, p.teto_unitario, vistos, exigido) if visto else ""
            problemas.append(
                f"produto {p.produto_id}: a pessoa pediu {p.quantidade_pedida} e a proposta tem {p.quantidade}; não "
                f"mude a quantidade pedida (escolha outro produto ou deixe o item em faltou){saida}"
            )
        quantidades[p.produto_id] = quantidades.get(p.produto_id, 0) + p.quantidade
        if p.teto_unitario is not None:
            tetos[p.produto_id] = min(p.teto_unitario, tetos.get(p.produto_id, p.teto_unitario))
        if p.motivo_escolha:
            motivos[p.produto_id] = p.motivo_escolha

    itens = []
    for produto_id, qtd in quantidades.items():
        p = vistos.get(produto_id)
        if p is None:
            problemas.append(f"o produto {produto_id} não foi devolvido por nenhuma ferramenta nesta conversa")
            continue
        preco = Decimal(str(p["preco"]))
        teto = tetos.get(produto_id)
        falhas = []
        if qtd > p["estoque"]:
            falhas.append(f"{qtd} unidades pedidas, {p['estoque']} em estoque")
        if qtd < p["quantidade_minima"]:
            falhas.append(f"o mínimo por pedido é {p['quantidade_minima']} unidades")
        if teto is not None and preco > teto:
            falhas.append(f"US$ {preco} passa do teto de US$ {teto} por unidade")
        if falhas:
            # A recusa já aponta a saída: sem ela, o modelo chegou a desistir dizendo que nada atendia.
            saida = _sugestao(p, qtd, teto, vistos, exigido=produto_id in motivos)
            problemas.append(f"{p['titulo']}: {'; '.join(falhas)}{saida}")
        elif produto_id not in motivos:
            barato = _mais_barato_que_atende(p, qtd, teto, vistos)
            if barato:
                problemas.append(
                    f"{p['titulo']}: {barato['titulo']} (US$ {barato['preco']}), da mesma busca, também atende e "
                    "é mais barato. Se o pedido aceita outro produto, troque; se exige este, diga o porquê em "
                    "motivo_escolha"
                )
        itens.append(Item(produto_id, p["titulo"], qtd, preco))

    total = sum((i.subtotal for i in itens), Decimal(0))
    if not problemas and teto_total is not None and total > teto_total:
        problemas.append(
            f"o total de US$ {total} passa do orçamento de US$ {teto_total} para o pedido inteiro; troque por "
            "itens mais baratos ou deixe algum em faltou"
        )
    return ([] if problemas else itens), problemas


# Valor em dinheiro citado no texto: "US$ 1.099,99", "US$ 2.000", "$1,099.99", "USD 999".
_DINHEIRO = re.compile(r"(?:US\$|R\$|\$|\b(?:USD|usd))\s*(\d[\d.,]*\d|\d)")


def _ler_valor(numero: str) -> Decimal | None:
    """Lê o número nos formatos brasileiro e americano: "1.099,99", "1,099.99", "2.000", "1099.99"."""
    if "," in numero and "." in numero:
        decimal_sep = "," if numero.rfind(",") > numero.rfind(".") else "."
        numero = numero.replace("." if decimal_sep == "," else ",", "").replace(decimal_sep, ".")
    elif "," in numero or "." in numero:
        # Um só separador com 1 ou 2 casas depois é decimal ("1.5", "1,50"); com 3 casas, ou repetido, é milhar.
        sep = "," if "," in numero else "."
        casas = len(numero.rsplit(sep, 1)[1])
        numero = numero.replace(sep, ".") if casas != 3 and numero.count(sep) == 1 else numero.replace(sep, "")
    try:
        return Decimal(numero)
    except InvalidOperation:
        return None


def conferir_valores_citados(textos: list[str], conhecidos: set[Decimal]) -> list[str]:
    """Todo valor em dinheiro do texto tem que ser um preço do catálogo, um valor do pedido ou o total calculado."""
    problemas = []
    for texto in textos:
        # "US$ 999" e "999 dólares" são o mesmo valor; repetido, ele vira uma só recusa.
        for numero in dict.fromkeys(_DINHEIRO.findall(texto) + _DOLARES.findall(texto)):
            valor = _ler_valor(numero)
            if valor is not None and not any(abs(valor - c) <= Decimal("0.01") for c in conhecidos):
                problemas.append(
                    f"o texto cita US$ {numero}, que não é preço do catálogo, valor do pedido nem total calculado; "
                    "tire o valor ou use o exato"
                )
    return problemas


# Valores e quantidades que a pessoa escreveu no pedido: é contra eles que o teto e a quantidade declarados pelo
# modelo são conferidos, para a conferência não confiar no que o próprio modelo extraiu.
_MIL = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:mil|k)\b", re.IGNORECASE)
_DOLARES = re.compile(r"(\d[\d.,]*)\s*(?:d[oó]lares|usd)\b", re.IGNORECASE)
# Valor em reais ("R$ 3.000", "R$ 3 mil", "2 mil reais"): o catálogo está em US$ e aqui não há câmbio.
_REAIS = re.compile(r"R\$\s*\d[\d.,]*(?:\s*(?:mil|k)\b)?|\d[\d.,]*\s*(?:mil\s+|k\s+)?reais\b", re.IGNORECASE)
_EM_DOLAR = re.compile(r"US\$|(?<!R)\$|d[oó]lar|\busd\b", re.IGNORECASE)
PEDIDO_EM_REAIS = (
    "o pedido dá o valor em reais e o catálogo está em US$. Não converta, porque aqui não há câmbio: não proponha "
    "itens nem teto, e diga em faltou que o valor precisa vir em dólares"
)
# Valor que não é limite: piso ("acima de US$ 1.000") ou referência ("o último custou US$ 2.500").
_NAO_E_LIMITE = re.compile(
    r"(?:acima de|a partir de|mais de|mais que|maior que|superior a|m[ií]nimo de|no m[ií]nimo|pelo menos|custou|"
    r"custava|paguei|pagamos)\s+(?:US\$|\$|USD)?\s*\d[\d.,]*(?:\s*(?:mil|k|d[oó]lares|usd)\b)?",
    re.IGNORECASE,
)
# Número sem moeda depois de "até", "máximo", "orçamento", "teto" ou "limite" ("até 2000 cada") vale como teto,
# mas não obriga a declarar um: "entrega em até 10 dias" também casa aqui.
_LIMITE_SEM_MOEDA = re.compile(
    r"\b(?:até|m[áa]ximo|or[çc]amento|teto|limite)(?:\s+(?:total|de|do))*\s+(\d[\d.,]*)", re.IGNORECASE
)
_INTEIRO = re.compile(r"\b\d+\b")
_POR_EXTENSO = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5,
                "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10}


def valores_do_pedido(pedido: str) -> set[Decimal]:
    """Dólares escritos no pedido: "US$ 2.000", "$2000", "2 mil", "2k", "2000 dólares". Valor em reais fica de
    fora: sem câmbio, ele não vale como teto em dólar."""
    pedido = _REAIS.sub(" ", pedido)
    valores = {v for n in _DINHEIRO.findall(pedido) + _DOLARES.findall(pedido) if (v := _ler_valor(n)) is not None}
    valores |= {v * 1000 for n in _MIL.findall(pedido) if (v := _ler_valor(n)) is not None}
    return valores


def quantidades_do_pedido(pedido: str) -> set[int] | None:
    """Quantidades escritas no pedido. None quando nenhuma vem em algarismos: "meia dúzia", "vinte e cinco" ou
    "um laptop" não dão para conferir com segurança, e uma regra errada aqui travaria um pedido legítimo."""
    sem_dinheiro = pedido
    for padrao in (_REAIS, _DINHEIRO, _DOLARES, _MIL):  # "US$ 2.000", "2 mil reais" e "2 mil" não são quantidade
        sem_dinheiro = padrao.sub(" ", sem_dinheiro)
    numeros = {int(n) for n in _INTEIRO.findall(sem_dinheiro)}
    if not numeros:
        return None
    # Pedido misto ("três laptops e 2 monitores"): os números por extenso mais comuns também valem.
    palavras = {_POR_EXTENSO[p] for p in re.findall(r"[a-zà-ú]+", pedido.lower()) if p in _POR_EXTENSO}
    return numeros | palavras


def conferir_contra_o_pedido(proposta: "Proposta", pedido: str, questionadas: set[int] | None = None) -> list[str]:
    """Confere teto e quantidade declarados pelo modelo contra o texto do pedido.

    Quantidade que não aparece escrita é questionada uma vez e vale se o modelo a mantiver: "meia dúzia de
    smartphones e 2 carregadores" ou "2 para cada um dos 3 gerentes" estão certas e não dá para conferir só pelo
    texto. `questionadas` guarda as já questionadas na conversa."""
    questionadas = set() if questionadas is None else questionadas
    limites = valores_do_pedido(_NAO_E_LIMITE.sub(" ", pedido))
    aceitos = limites | {v for n in _LIMITE_SEM_MOEDA.findall(pedido) if (v := _ler_valor(n)) is not None}
    quantidades = quantidades_do_pedido(pedido)
    tetos = [i.teto_unitario for i in proposta.itens if i.teto_unitario is not None]
    if proposta.teto_total is not None:
        tetos.append(proposta.teto_total)

    if _REAIS.search(pedido) and not _EM_DOLAR.search(pedido):
        # Converter exigiria um câmbio que o agente não tem, e o que ele inventasse passaria como teto.
        return [PEDIDO_EM_REAIS] if proposta.itens or tetos else []

    problemas = [
        f"o teto US$ {t} não aparece no pedido como limite; use o valor que a pessoa deu como teto, ou omita se ela "
        "não deu"
        for t in tetos if not any(abs(t - v) <= Decimal("0.01") for v in aceitos)
    ]
    if limites and proposta.itens and not tetos:
        problemas.append("o pedido dá um limite em dinheiro: informe-o em teto_unitario (por unidade) ou em "
                         "teto_total (pedido inteiro)")
    if quantidades is not None:
        fora = [i for i in proposta.itens if i.quantidade_pedida not in quantidades | questionadas]
        problemas += [
            f"produto {i.produto_id}: a quantidade_pedida {i.quantidade_pedida} não aparece escrita no pedido. Se a "
            "pessoa pediu essa quantidade de outro jeito (\"meia dúzia\", \"2 para cada um dos 3\"), mantenha; se ela "
            "escreveu outra, use a dela; se não disse quantos, deixe o item em faltou"
            for i in fora
        ]
        questionadas.update(i.quantidade_pedida for i in fora)
    return problemas


def _valores_conhecidos(pedido: str, proposta: "Proposta", itens: list[Item], vistos: dict[int, dict]) -> set[Decimal]:
    conhecidos = {Decimal(str(v["preco"])) for v in vistos.values()}
    conhecidos |= {i.subtotal for i in itens} | {sum((i.subtotal for i in itens), Decimal(0))}
    conhecidos |= {p.teto_unitario for p in proposta.itens if p.teto_unitario is not None}
    if proposta.teto_total is not None:
        conhecidos.add(proposta.teto_total)
    conhecidos |= {v for n in _DINHEIRO.findall(pedido) if (v := _ler_valor(n)) is not None}
    return conhecidos


def _que_atendem(produto: dict, qtd: int, teto: Decimal | None, vistos: dict[int, dict]) -> list[dict]:
    """Produtos da mesma busca e da mesma categoria que o produto, que atendem à quantidade e ao teto; do mais
    barato ao mais caro."""
    opcoes = [
        v for v in vistos.values()
        if v["categoria"] == produto["categoria"]
        and v.get("_termos", set()) & produto.get("_termos", set())
        and v["estoque"] >= qtd
        and v["quantidade_minima"] <= qtd
        and (teto is None or Decimal(str(v["preco"])) <= teto)
    ]
    return sorted(opcoes, key=lambda v: Decimal(str(v["preco"])))


def _mais_barato_que_atende(escolhido: dict, qtd: int, teto: Decimal | None, vistos: dict[int, dict]) -> dict | None:
    preco = Decimal(str(escolhido["preco"]))
    opcoes = _que_atendem(escolhido, qtd, teto, vistos)
    mais_baratos = [v for v in opcoes if Decimal(str(v["preco"])) < preco]
    return mais_baratos[0] if mais_baratos else None


def _sugestao(produto: dict, qtd: int, teto: Decimal | None, vistos: dict[int, dict], exigido: bool = False) -> str:
    """O que dizer ao modelo depois de uma recusa. Se o pedido exige este produto, não sugere trocar."""
    if exigido:
        return ". O pedido exige este produto: deixe o item em faltou com o motivo"
    opcoes = [v for v in _que_atendem(produto, qtd, teto, vistos) if v["id"] != produto["id"]]
    if not opcoes:
        return ". Nenhum outro produto da mesma busca atende; se não houver outra busca a fazer, deixe o item em faltou"
    v = opcoes[0]
    return (
        f". Se o pedido aceita outro produto, um da mesma busca que atende é {v['titulo']} (id {v['id']}, "
        f"US$ {v['preco']}, mínimo {v['quantidade_minima']}, estoque {v['estoque']}); se exige este, deixe o item "
        "em faltou"
    )


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
            log.exception("mcp_indisponivel")
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
    questionadas: set[int] = set()
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
                resultado, conteudo = _responder(args, pedido, vistos, chamadas_log, questionadas)
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


def _responder(
    args: dict, pedido: str, vistos: dict[int, dict], chamadas_log: list[dict], questionadas: set[int]
) -> tuple[Resultado | None, str]:
    """Confere a proposta final. Devolve (resultado, "") se ela passa, ou (None, motivo) para o modelo corrigir."""
    try:
        proposta = Proposta.model_validate(args)
        itens, problemas = conferir_itens(proposta.itens, vistos, proposta.teto_total)
        problemas = conferir_contra_o_pedido(proposta, pedido, questionadas) + problemas
        if not problemas:
            textos = [proposta.mensagem] + [f.motivo for f in proposta.faltou]
            problemas = conferir_valores_citados(textos, _valores_conhecidos(pedido, proposta, itens, vistos))
    except ValidationError as e:
        problemas = [f"argumentos inválidos: {erro['loc']} {erro['msg']}" for erro in e.errors()]

    detalhe = "; ".join(problemas) or None
    chamadas_log.append({"ferramenta": "responder", "argumentos": args, "erro": bool(problemas), "detalhe": detalhe})
    if problemas:
        log.info("proposta_recusada", extra={"motivo": detalhe})
        return None, f"Proposta recusada pela conferência: {detalhe}. Corrija e chame responder de novo."
    faltou = [f.model_dump() for f in proposta.faltou]
    log.info("proposta_aceita", extra={"itens": len(itens), "faltou": len(faltou)})
    return Resultado(itens, proposta.mensagem, chamadas_log, faltou), ""


async def _consultar_em_paralelo(mcp: Client, consultas: list[tuple]) -> list[tuple]:
    """Chama as ferramentas ao mesmo tempo. Cada tarefa guarda o retorno ou a exceção: nenhuma exceção sai
    do TaskGroup, que a embrulharia num ExceptionGroup. Devolve na ordem pedida."""
    retornos: list = [None] * len(consultas)

    async def consultar(i: int, nome: str, args: dict) -> None:
        inicio = time.monotonic()
        try:
            retornos[i] = await mcp.call_tool(nome, args)
        except Exception as e:  # noqa: BLE001 - guardado aqui e tratado em _registrar, fora do TaskGroup
            retornos[i] = e
        r = retornos[i]
        log.info("ferramenta", extra={"ferramenta": nome, "argumentos": args,
                                      "duracao_ms": round((time.monotonic() - inicio) * 1000),
                                      "erro": isinstance(r, Exception) or r.is_error})

    async with anyio.create_task_group() as grupo:
        for i, (_, nome, args) in enumerate(consultas):
            grupo.start_soon(consultar, i, nome, args)
    return list(zip(consultas, retornos, strict=True))


def _registrar(nome: str, args: dict, r, vistos: dict[int, dict], chamadas_log: list[dict]) -> str:
    """Registra o retorno de uma ferramenta e devolve o texto que vai para o modelo."""
    if isinstance(r, Exception):
        log.error("ferramenta_falhou", extra={"ferramenta": nome}, exc_info=r)
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
