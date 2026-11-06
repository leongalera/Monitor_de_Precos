import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///monitor.db"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "2"))
ALERT_COOLDOWN_HOURS = float(os.getenv("ALERT_COOLDOWN_HOURS", "24"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Interface web (a página só funciona se ADMIN_PASSWORD estiver definido)
PORT = int(os.getenv("PORT", "8000"))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Busca multi-loja (Serper.dev) e IA (Anthropic): ambos opcionais
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "claude-haiku-5-5")
