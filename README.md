# Monitor de Preços

Serviço contínuo que verifica preços de eletrodomésticos e itens de acabamento e avisa no Telegram quando o preço fica menor ou igual ao preço alvo. Tem interface web para cadastrar produtos (por URL ou por busca em várias lojas) e usa IA só no cadastro, nunca nas checagens recorrentes.

## Estrutura

```
main.py              # sobe a interface web (uvicorn) e, junto, o agendador
manage.py            # CLI alternativa para cadastrar/listar/ativar/remover produtos
app/config.py        # variáveis de ambiente
app/db.py            # SQLAlchemy (SQLite local / PostgreSQL via DATABASE_URL)
app/scraper.py       # requests + BeautifulSoup, JSON-LD, User-Agent rotativo, proteção SSRF, leitura com navegador (Playwright)
app/discovery.py     # analisa uma URL (nome/preço) e busca ofertas em várias lojas (Serper), com diagnóstico dos descartes
app/ai.py            # IA (Haiku): sugere seletor CSS e filtra ofertas do mesmo modelo
app/notifier.py      # Telegram (POST direto na Bot API)
app/scheduler.py     # job periódico, gatilho de alerta e cooldown
app/web.py           # FastAPI: rotas, autenticação e proteção CSRF
app/templates/       # páginas HTML
```

## Grupos

Cada **grupo** é um produto e reúne os links das lojas que o vendem. O preço alvo é do grupo e vale para todos os links; o painel mostra o melhor preço entre as lojas e o motivo de cada leitura que falhou (bloqueio da loja, captcha etc.). O alerta no Telegram traz o nome do grupo e a loja. Pausar ou remover um grupo age em todos os links; produtos antigos aparecem em "Sem grupo" e podem ser movidos para um grupo.

Ao subir a versão com grupos, o app adiciona sozinho as colunas novas (`group_id`, `last_error`) à tabela `products` já existente, sem perder dados.

## Como funciona o cadastro

1. **Por URL:** você cola o link; o sistema lê nome e preço dos dados estruturados da página (JSON-LD `schema.org/Product`), gratuito e estável. Se a página não tiver, tenta seletores genéricos e, havendo `ANTHROPIC_API_KEY`, a IA escolhe o elemento certo do preço. O seletor fica salvo e as checagens seguintes não usam IA.
2. **Por busca:** você digita o produto; o sistema busca páginas de lojas (via Serper), lê o preço de cada uma e lista só as que têm preço legível (JSON-LD), ou seja, as que dá para monitorar. Você marca quais acompanhar. A opção "Filtrar com IA" mantém só o mesmo modelo. Lojas com proteção anti-bot podem não aparecer; nesse caso cadastre pelo link.

## Limitações conhecidas

Algumas lojas (por exemplo Mercado Livre, Amazon, Casas Bahia) recusam ou desafiam com captcha as requisições vindas de servidores em nuvem. Nesses casos o link fica "sem leitura" e o motivo aparece no grupo. A solução geral exige um serviço de proxy/scraping pago.

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

## Páginas que carregam o preço por JavaScript

Algumas lojas entregam a página "vazia" e montam o preço depois, com JavaScript. Quando o HTML simples não traz o preço, o cadastro por URL tenta de novo abrindo a página num Chromium (Playwright); se achar, o produto é marcado com o selo **navegador** e passa a ser lido assim em cada verificação (mais lento e com mais memória que a leitura simples). Toda requisição do navegador passa pela mesma proteção contra SSRF e imagens/fontes são bloqueadas.

O Chromium vem do `Dockerfile` (imagem oficial do Playwright), que o Railway usa automaticamente. A versão da imagem e a de `playwright==` em `requirements.txt` precisam ser a mesma. Localmente, sem Chromium, a leitura com navegador é pulada e o motivo aparece na tela; para ativar: `playwright install --with-deps chromium`.
