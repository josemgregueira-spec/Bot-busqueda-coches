import os
import sys
import time

from scraper import TELEGRAM_TOKEN, run_pipeline, log


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not os.getenv("CHAT_ID"):
        sys.exit("Configura TELEGRAM_TOKEN y CHAT_ID.")

    # PLATFORMS permite pedir solo una o varias plataformas concretas, en
    # vez de las 3 a la vez (usado por el botón del bot para elegir dónde
    # buscar). Ejemplo: PLATFORMS="autoscout,kleinanzeigen"
    platforms_env = os.getenv("PLATFORMS", "").strip()
    platforms = [p.strip() for p in platforms_env.split(",") if p.strip()] or None

    # GitHub Actions y el botón "Buscar ahora" del bot lanzan este script con
    # RUN_ONCE=1 y esperan que termine tras una sola pasada. Sin esta
    # variable (ejecución local/servidor propio), se repite en bucle.
    if os.getenv("RUN_ONCE") == "1":
        run_pipeline(platforms)
    else:
        interval = int(os.getenv("INTERVAL_SECONDS", "3600"))
        while True:
            try:
                run_pipeline(platforms)
            except Exception:
                log.exception("Error no controlado en el ciclo")
            time.sleep(interval)
