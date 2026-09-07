import os
import sys
import time

from scraper import TELEGRAM_TOKEN, run_pipeline


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not os.getenv("CHAT_ID"):
        sys.exit("Configura TELEGRAM_TOKEN y CHAT_ID.")

    # GitHub Actions y el botón del bot deben acabar al terminar una pasada.
    if os.getenv("RUN_ONCE") == "1":
        run_pipeline()
    else:
        interval = int(os.getenv("INTERVAL_SECONDS", "3600"))
        while True:
            run_pipeline()
            time.sleep(interval)
