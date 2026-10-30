"""Manda 5 pedidos de teste ao agente e mostra quantos ele acertou.

Uso, com tudo no ar e a chave no .env:

    docker compose exec backend python avaliacao.py

Chama o modelo de verdade (centavos por rodada) e grava no banco as requisições que forem criadas.
A categoria de cada item é conferida direto na DummyJSON, não no que o agente respondeu.
"""

import sys
from decimal import Decimal

import httpx

API = "http://localhost:8000/requisicoes"
DUMMYJSON = "https://dummyjson.com"


def ids_da_categoria(categoria: str) -> set[int]:
    r = httpx.get(f"{DUMMYJSON}/products/category/{categoria}", params={"limit": 0, "select": "id"}, timeout=15)
    r.raise_for_status()
    return {p["id"] for p in r.json()["products"]}


def criada(categoria: str, quantidade: int, preco_max: int, menciona: str | None = None):
    ids = ids_da_categoria(categoria)

    def checar(resp: dict) -> str | None:
        if resp["status"] != "criada":
            return f"esperava requisição criada, veio {resp['status']}"
        fora = [i["titulo"] for i in resp["itens"] if i["produto_id"] not in ids]
        if fora:
            return f"itens fora da categoria {categoria}: {fora}"
        total_un = sum(i["quantidade"] for i in resp["itens"])
        if total_un != quantidade:
            return f"esperava {quantidade} unidades, veio {total_un}"
        caros = [i["titulo"] for i in resp["itens"] if Decimal(i["preco_unitario"]) > preco_max]
        if caros:
            return f"acima de US$ {preco_max}: {caros}"
        if menciona and menciona not in resp["mensagem"].lower():
            return f"a mensagem não diz o que faltou ({menciona!r})"
        return None

    return checar


def nao_atendida():
    def checar(resp: dict) -> str | None:
        if resp["status"] != "nao_atendida":
            return f"esperava não atendida, veio requisição nº {resp['numero']}"
        return None

    return checar


CASOS = [
    ("Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.", criada("laptops", 3, 2000)),
    ("Preciso de 4 smartphones até US$ 300 cada.", criada("smartphones", 4, 300)),
    ("Preciso de 2 tratores para a fazenda.", nao_atendida()),
    ("Preciso de 3 laptops até US$ 2.000 cada e 2 tratores.", criada("laptops", 3, 2000, menciona="trator")),
    ("Preciso de 30 MacBook Pro para a equipe.", nao_atendida()),  # estoque: 24
]


def main() -> int:
    acertos = 0
    for n, (pedido, checar) in enumerate(CASOS, 1):
        try:
            r = httpx.post(API, json={"pedido": pedido}, timeout=180)
            resp = r.json()
            problema = resp.get("detail") or checar(resp)
        except httpx.HTTPError as e:
            problema = f"falha ao chamar a API: {e}"
        acertos += problema is None
        print(f"{'✓' if problema is None else '✗'} {n}. {pedido}")
        if problema:
            print(f"     {problema}")
        else:
            itens = ", ".join(f"{i['quantidade']}× {i['titulo']} (US$ {i['preco_unitario']})" for i in resp["itens"])
            resumo = f"nº {resp['numero']}: {itens}" if itens else "não atendida"
            print(f"     {resumo} — {resp['mensagem']}")
    print(f"\nAcertos: {acertos}/{len(CASOS)}")
    return 0 if acertos == len(CASOS) else 1


if __name__ == "__main__":
    sys.exit(main())
