import logging
import sys

import uvicorn

from app.config import LOG_LEVEL, PORT
from app.web import app

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    # log_config=None: o uvicorn usa o logging acima (stdout) em vez de stderr.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    main()
