# Imagem oficial do Playwright: já traz o Chromium e as bibliotecas do sistema.
# A versão da tag deve ser a mesma de playwright== em requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

CMD ["python", "main.py"]
