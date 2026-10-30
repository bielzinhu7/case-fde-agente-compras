"""Erros da OpenRouter viram mensagens que dizem o que conferir. A OpenRouter é simulada: sem chave real."""

import httpx
import pytest
import respx

from app import llm

pytestmark = pytest.mark.anyio

RESPOSTA_OK = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": []}}]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def chave_e_sem_espera(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "chave-de-teste")

    async def sem_espera(_):
        pass

    monkeypatch.setattr(llm.anyio, "sleep", sem_espera)


@respx.mock
async def test_modelo_recusado_traz_o_motivo_da_openrouter_e_o_que_conferir():
    respx.post(llm.URL).mock(
        return_value=httpx.Response(404, json={"error": {"message": "No endpoints found that support tool use"}})
    )

    with pytest.raises(llm.ModeloIndisponivel) as erro:
        await llm.completar([], [])

    assert "No endpoints found that support tool use" in str(erro.value)
    assert "OPENROUTER_MODEL" in str(erro.value)


@respx.mock
async def test_limite_de_taxa_tenta_de_novo_antes_de_desistir():
    rota = respx.post(llm.URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, json=RESPOSTA_OK)])

    msg = await llm.completar([], [])

    assert rota.call_count == 2
    assert msg["role"] == "assistant"


@respx.mock
async def test_erro_em_texto_no_corpo_nao_quebra_o_tratamento():
    respx.post(llm.URL).mock(return_value=httpx.Response(200, json={"error": "provider overloaded"}))

    with pytest.raises(llm.ModeloIndisponivel, match="provider overloaded"):
        await llm.completar([], [])


async def test_sem_chave_diz_onde_configurar(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")

    with pytest.raises(llm.ModeloIndisponivel, match=r"\.env"):
        await llm.completar([], [])


@respx.mock
async def test_soma_tokens_e_custo_das_chamadas_do_pedido():
    resposta = {**RESPOSTA_OK, "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0003}}
    respx.post(llm.URL).mock(return_value=httpx.Response(200, json=resposta))

    acumulado = llm.medir_uso()
    await llm.completar([], [])
    await llm.completar([], [])

    assert acumulado["tokens_entrada"] == 200 and acumulado["tokens_saida"] == 40
    assert round(acumulado["custo_usd"], 6) == 0.0006
