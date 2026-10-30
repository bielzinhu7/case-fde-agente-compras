"""Logs em JSON, uma linha por evento, todas com o id do pedido.

O id nasce no middleware da API e acompanha o pedido por contextvar, inclusive nas tarefas paralelas.
Para achar tudo de um pedido: `docker compose logs backend | grep <id>`.
"""

import contextvars
import json
import logging
import uuid
from datetime import UTC, datetime

pedido_id: contextvars.ContextVar[str] = contextvars.ContextVar("pedido_id", default="-")

# Atributos que todo LogRecord já tem; o resto veio do `extra=` e vira campo da linha.
_PADRAO = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class FormatoJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        linha = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "nivel": record.levelname,
            "origem": record.name,
            "pedido_id": pedido_id.get(),
            "evento": record.getMessage(),
        }
        linha.update({k: v for k, v in vars(record).items() if k not in _PADRAO and not k.startswith("_")})
        if record.exc_info:
            linha["rastro"] = self.formatException(record.exc_info)
        return json.dumps(linha, ensure_ascii=False, default=str)


def configurar() -> None:
    saida = logging.StreamHandler()
    saida.setFormatter(FormatoJson())
    raiz = logging.getLogger()
    raiz.handlers[:] = [saida]
    raiz.setLevel(logging.INFO)
    # O httpx (e o httpx2, que o SDK do MCP usa) registraria cada chamada HTTP; o que interessa já sai nos
    # eventos do agente.
    for ruidoso in ("httpx", "httpx2"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def novo_id() -> str:
    return uuid.uuid4().hex[:8]
