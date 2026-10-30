"""A conferência da chave no /saude: a OpenRouter é simulada, então não precisa de chave real."""

import httpx
import pytest
import respx

from app import saude

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def chave_e_cache_limpo(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "chave-de-teste")
    monkeypatch.setattr(saude, "_cache_chave", None)


@respx.mock
async def test_chave_recusada_aparece_como_falha():
    respx.get(saude.OPENROUTER_CHAVE).mock(return_value=httpx.Response(401))

    r = await saude._medir(saude._modelo)

    assert r["ok"] is False and "recusou a chave" in r["erro"]


@respx.mock
async def test_chave_sem_credito_aparece_como_falha():
    respx.get(saude.OPENROUTER_CHAVE).mock(return_value=httpx.Response(200, json={"data": {"limit_remaining": 0}}))

    r = await saude._medir(saude._modelo)

    assert r["ok"] is False and "sem crédito" in r["erro"]


@respx.mock
async def test_chave_valida_traz_credito_e_validade_e_fica_em_cache():
    dados = {"data": {"limit_remaining": 19.5, "expires_at": "2026-11-05T20:44:00Z"}}
    rota = respx.get(saude.OPENROUTER_CHAVE).mock(return_value=httpx.Response(200, json=dados))

    primeira = await saude._medir(saude._modelo)
    segunda = await saude._medir(saude._modelo)

    assert primeira["ok"] and primeira["credito_restante_usd"] == 19.5
    assert primeira["expira_em"] == "2026-11-05T20:44:00Z"
    assert segunda["ok"] and rota.call_count == 1  # a segunda veio do cache


async def test_sem_chave_nem_consulta_a_openrouter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")

    r = await saude._medir(saude._modelo)

    assert r["ok"] is False and ".env" in r["erro"]
