# Monitor de Preços

Serviço contínuo que verifica preços de eletrodomésticos e itens de acabamento e avisa no Telegram quando o preço fica menor ou igual ao preço alvo. Tem interface web para cadastrar produtos (por URL ou por busca em várias lojas) e usa IA só no cadastro, nunca nas checagens recorrentes.

## Estrutura

```
main.py              # sobe a interface web (uvicorn) e, junto, o agendador
manage.py            # CLI alternativa para cadastrar/listar/ativar/remover produtos
app/config.py        # variáveis de ambiente
app/db.py            # SQLAlchemy (SQLite local / PostgreSQL via DATABASE_URL)
app/scraper.py       # requests + BeautifulSoup, JSON-LD, User-Agent rotativo, proteção SSRF, gancho p/ Playwright
app/discovery.py     # analisa uma URL (nome/preço) e busca ofertas em várias lojas (Serper)
app/ai.py            # IA (Haiku): sugere seletor CSS e filtra ofertas do mesmo modelo
app/notifier.py      # Telegram (POST direto na Bot API)
app/scheduler.py     # job periódico, gatilho de alerta e cooldown
app/web.py           # FastAPI: rotas, autenticação e proteção CSRF
app/templates/       # páginas HTML
```

## Como funciona o cadastro

1. **Por URL:** você cola o link; o sistema lê nome e preço dos dados estruturados da página (JSON-LD `schema.org/Product`), gratuito e estável. Se a página não tiver, tenta seletores genéricos e, havendo `ANTHROPIC_API_KEY`, a IA escolhe o elemento certo do preço. O seletor fica salvo e as checagens seguintes não usam IA.
2. **Por busca:** você digita o produto, o sistema lista ofertas do Google Shopping (via Serper) e você marca quais monitorar. A opção "Filtrar com IA" mantém só o mesmo modelo.

## Rodar localmente

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # preencha ADMIN_PASSWORD e, se quiser, os demais
python main.py              # http://localhost:8000 (usuário: admin)
```

Sem `ADMIN_PASSWORD` a interface web fica desativada (o monitor continua rodando). A CLI segue disponível:

```bash
python manage.py add --name "Geladeira Exemplo" --url "https://loja.com/produto" --target-price 3499 --selector ".price"
python manage.py list
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição | Padrão |
|---|---|---|---|
| `DATABASE_URL` | produção | URL do banco | `sqlite:///monitor.db` |
| `ADMIN_PASSWORD` | para a web | Senha da interface | — |
| `ADMIN_USER` | não | Usuário da interface | `admin` |
| `TELEGRAM_TOKEN` | para alertas | Token do bot (@BotFather) | — |
| `TELEGRAM_CHAT_ID` | para alertas | Chat que recebe os alertas | — |
| `SERPER_API_KEY` | para a busca | Chave do [serper.dev](https://serper.dev) | — |
| `ANTHROPIC_API_KEY` | para a IA | Chave do [console.anthropic.com](https://console.anthropic.com) | — |
| `AI_MODEL` | não | Modelo usado pela IA | `claude-haiku-5-5` |
| `CHECK_INTERVAL_HOURS` | não | Intervalo entre verificações | `2` |
| `ALERT_COOLDOWN_HOURS` | não | Intervalo mínimo entre alertas do mesmo produto | `24` |

## Deploy no Railway

1. Deploy a partir do GitHub (branch `main`); o `Procfile` define o processo `web`.
2. Adicione um banco: **New → Database → PostgreSQL**.
3. No serviço, em **Variables**, defina `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` e as demais variáveis da tabela acima.
4. Em **Settings → Networking → Generate Domain** para ter a URL pública da interface.

## Segurança

- A interface exige login (HTTP Basic); use uma senha longa, pois a URL é pública.
- POSTs de outros sites são bloqueados (CSRF).
- O servidor só busca URLs `http(s)` que resolvem para IPs públicos, inclusive a cada redirecionamento (evita SSRF).

## Playwright (futuro)

Para páginas com renderização JS: `pip install playwright && playwright install --with-deps chromium`, e cadastre o produto com `--render-js` na CLI. O gancho já existe em `app/scraper.py` (`fetch_html_with_playwright`).
