FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# As dependências de teste vão na imagem para os testes rodarem com
# `docker compose run --rm mcp pytest`, sem instalar Python na máquina.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

EXPOSE 8000
CMD ["python", "-m", "catalogo.servidor"]
