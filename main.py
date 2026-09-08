import os
import sys
import time

from scraper import TELEGRAM_TOKEN, run_pipeline, log


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not os.getenv("CHAT_ID"):
        sys.exit("Configura TELEGRAM_TOKEN y CHAT_ID.")

    # GitHub Actions y el botón "Buscar ahora" del bot lanzan este script con
    # RUN_ONCE=1 y esperan que termine tras una sola pasada. Sin esta
    # variable (ejecución local/servidor propio), se repite en bucle.
    if os.getenv("RUN_ONCE") == "1":
        run_pipeline()
    else:
        interval = int(os.getenv("INTERVAL_SECONDS", "3600"))
        while True:
            try:
                run_pipeline()
            except Exception:
                log.exception("Error no controlado en el ciclo")
            time.sleep(interval)
