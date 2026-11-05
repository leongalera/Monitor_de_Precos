# Monitor de Preços

Worker contínuo que verifica preços de eletrodomésticos e itens de acabamento e avisa no Telegram quando o preço fica menor ou igual ao preço alvo.

## Estrutura

```
main.py            # entrypoint do worker (APScheduler bloqueante)
manage.py          # CLI para cadastrar/listar/ativar/remover produtos
app/config.py      # variáveis de ambiente
app/db.py          # SQLAlchemy (SQLite local / PostgreSQL via DATABASE_URL)
app/scraper.py     # requests + BeautifulSoup, User-Agent rotativo, limpeza de preço, gancho p/ Playwright
app/notifier.py    # Telegram (POST direto na Bot API)
app/scheduler.py   # job periódico, gatilho de alerta e cooldown
```

## Rodar localmente

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # preencha TELEGRAM_TOKEN e TELEGRAM_CHAT_ID

python manage.py add --name "Geladeira Exemplo" --url "https://loja.com/produto" \
  --target-price 3499 --selector ".price"
python manage.py list

python main.py              # roda uma verificação imediata e depois a cada 2h
```

O `--selector` é opcional: sem ele, o scraper tenta seletores genéricos (`itemprop=price`, `.price`, etc.).

## Variáveis de ambiente

| Variável | Descrição | Padrão |
|---|---|---|
| `TELEGRAM_TOKEN` | Token do bot (@BotFather) | — |
| `TELEGRAM_CHAT_ID` | Chat que recebe os alertas | — |
| `DATABASE_URL` | URL do banco | `sqlite:///monitor.db` |
| `CHECK_INTERVAL_HOURS` | Intervalo entre verificações | `2` |
| `ALERT_COOLDOWN_HOURS` | Intervalo mínimo entre alertas do mesmo produto | `24` |

## Deploy no Railway

1. Suba o código para o GitHub.
2. No Railway: **New Project → Deploy from GitHub repo** (o `Procfile` define o serviço `worker`).
3. Adicione um banco: **New → Database → PostgreSQL**.
4. No serviço do worker, em **Variables**, defina:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`
5. Cadastre produtos apontando a CLI para o banco do Railway (ou com `railway run`):
   ```bash
   railway run python manage.py add --name "..." --url "..." --target-price 1000
   ```

## Playwright (futuro)

Para páginas com renderização JS: `pip install playwright && playwright install --with-deps chromium`, e cadastre o produto com `--render-js`. O gancho já existe em `app/scraper.py` (`fetch_html_with_playwright`).
