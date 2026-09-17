import html
import json
import logging
import os
import random
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")
MAX_PAGES = int(os.getenv("MAX_PAGES", "3"))
# Kleinanzeigen bloquea mucho más rápido que las otras dos plataformas
# (a veces tras solo 2-3 anuncios), así que por defecto se le pide bastante
# menos y con pausas más largas. Ajustable con la variable de entorno.
KLEINANZEIGEN_MAX_PAGES = int(os.getenv("KLEINANZEIGEN_MAX_PAGES", "1"))
SEEN_RETENTION_DAYS = int(os.getenv("SEEN_RETENTION_DAYS", "90"))


def human_pause(min_seconds, max_seconds):
    """Pausa aleatoria para que el patrón de peticiones no sea idéntico
    (y por tanto fácilmente detectable) en cada ciclo."""
    time.sleep(random.uniform(min_seconds, max_seconds))

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "searches_config.json"
SEEN_FILE = BASE_DIR / "seen_cars.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilidades de E/S
# ---------------------------------------------------------------------------

def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_config():
    return read_json(
        CONFIG_FILE,
        {
            "make": "bmw",
            "model": "",
            "vigilancia": False,
            "max_price": "20000",
            "max_km": "150000",
            "zip_code": "",
            "radius": "",
        },
    )


def load_seen():
    raw = read_json(SEEN_FILE, {})
    now = time.time()

    if isinstance(raw, list):
        seen = {str(car_id): now for car_id in raw}
    elif isinstance(raw, dict):
        seen = {
            str(car_id): float(saved_at)
            for car_id, saved_at in raw.items()
            if isinstance(saved_at, (int, float))
        }
    else:
        seen = {}

    return prune_seen(seen)


def prune_seen(seen):
    oldest = time.time() - SEEN_RETENTION_DAYS * 86400
    return {car_id: saved_at for car_id, saved_at in seen.items() if saved_at >= oldest}


def save_seen(seen):
    write_json(SEEN_FILE, prune_seen(seen))


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def norm(value):
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def number(value):
    match = re.search(r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)", str(value))
    return int(re.sub(r"\D", "", match.group(0))) if match else None


def blocked_title(title):
    """
    Comprueba solo el <title> de la página (no todo el HTML) contra frases
    típicas de páginas de bloqueo/challenge. Mirar el título es mucho más
    fiable que buscar palabras sueltas en todo el documento: una página de
    resultados normal puede incluir de fondo el script de un captcha "por si
    acaso" (práctica habitual en muchas webs) sin que eso signifique que la
    petición fue bloqueada; en cambio, una página de bloqueo real SÍ pone
    algo como "Access denied" directamente en el título.
    """
    if not title:
        return None

    title_lower = title.lower()
    markers = (
        "access denied",
        "zugriff verweigert",
        "attention required",
        "just a moment",
        "you have been blocked",
        "pardon our interruption",
        "too many requests",
        "verify you are human",
        "unusual traffic",
    )
    for marker in markers:
        if marker in title_lower:
            return marker
    return None


def has_deductible_vat(text):
    text = " ".join(text.lower().split())
    markers = (
        "mwst. ausweisbar",
        "zzgl. mwst",
        "vat reclaimable",
        "mehrwertsteuer ausweisbar",
    )
    return any(marker in text for marker in markers)


def clean_link(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    filtered = {k: v for k, v in query.items() if not k.startswith(("utm_", "ref"))}
    return parsed._replace(query=urlencode(filtered, doseq=True), fragment="").geturl()


def image_url(element):
    if not element:
        return None
    return element.get("src") or element.get("data-src") or element.get("data-lazy-src")


def clean_text(element, default="Consultar"):
    return element.get_text(" ", strip=True) if element else default


# ---------------------------------------------------------------------------
# HTTP con reintentos
# ---------------------------------------------------------------------------

def get(session, url, params, platform):
    delay = 3
    for attempt in range(1, 4):
        try:
            response = session.get(url, params=params, timeout=(10, 25))

            if response.status_code == 200:
                return response

            if response.status_code not in {429, 500, 502, 503, 504}:
                log.warning("%s: HTTP %s (%s)", platform, response.status_code, response.url)
                return None

            delay = int(response.headers.get("Retry-After", attempt * 3))
            log.warning("%s: HTTP %s, reintento en %ss", platform, response.status_code, delay)

        except requests.RequestException as error:
            delay = attempt * 3
            log.warning("%s: error de red (%s/3): %s", platform, attempt, error)

        time.sleep(delay)

    log.error("%s: sin respuesta tras 3 intentos", platform)
    return None


# ---------------------------------------------------------------------------
# Construcción y filtrado de anuncios
# ---------------------------------------------------------------------------

def make_car(platform, base_url, item, title_selector, price_selector):
    listing_text = clean_text(item, "")

    if has_deductible_vat(listing_text):
        return None

    link_element = item.select_one("a[href]")
    if not link_element:
        return None

    link = clean_link(urljoin(base_url, link_element["href"]))
    if not link.startswith(("http://", "https://")):
        return None

    return {
        "id": f"{platform.lower()}:{link}",
        "title": clean_text(item.select_one(title_selector), clean_text(link_element, "Vehículo")),
        "price": clean_text(item.select_one(price_selector)),
        "link": link,
        "image": image_url(item.select_one("img")),
        "platform": platform,
        "_listing_text": listing_text,
        "seller_type": "Sin IVA deducible detectado",
    }


def matches_config(car, config):
    title = norm(car["title"])
    # Si el modelo está vacío, vale cualquier modelo de esa marca.
    return all(
        norm(value) in title
        for value in (config.get("make", ""), config.get("model", ""))
        if norm(value)
    )


def within_limits(car, config):
    max_price = number(config.get("max_price", ""))
    price = number(car["price"])
    if max_price is not None and (price is None or price > max_price):
        return False

    max_km = number(config.get("max_km", ""))
    if max_km is not None:
        # Se coge el número MÁS ALTO de todos los "<número> km" que
        # aparezcan en el texto, no el primero. Algunos anuncios mencionan
        # más de un número seguido de "km" (p. ej. "quedan 5.000 km para
        # la ITV" además del kilometraje real del coche); coger el primero
        # a ciegas puede hacer que se cuele un coche con muchos más
        # kilómetros de los que el filtro dice permitir.
        matches = re.findall(r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)\s*km", car["_listing_text"], re.I)
        kms = [int(re.sub(r"\D", "", m)) for m in matches]
        km = max(kms) if kms else None
        if km is None or km > max_km:
            return False

    return True


def qualifies(car, config):
    return bool(car) and matches_config(car, config) and within_limits(car, config)


# ---------------------------------------------------------------------------
# AutoScout24
# ---------------------------------------------------------------------------

# AutoScout24 usa "slugs" concretos en la URL que no siempre coinciden con el
# nombre corto/habitual de la marca (p. ej. "mercedes" da 404; hace falta
# "mercedes-benz"). Se van añadiendo alias aquí según se detecten casos.
AUTOSCOUT_MAKE_ALIASES = {
    "mercedes": "mercedes-benz",
    "mercedesbenz": "mercedes-benz",
    "vw": "volkswagen",
    "landrover": "land-rover",
    "land rover": "land-rover",
    "alfa": "alfa-romeo",
    "alfaromeo": "alfa-romeo",
    "alfa romeo": "alfa-romeo",
}


def autoscout_slug(value):
    key = value.strip().lower()
    key_no_spaces = key.replace(" ", "")
    alias = AUTOSCOUT_MAKE_ALIASES.get(key) or AUTOSCOUT_MAKE_ALIASES.get(key_no_spaces)
    return alias if alias else quote(key, safe="-")


def fetch_autoscout(config, session, max_pages=MAX_PAGES):
    make = config.get("make", "").strip().lower()
    model = config.get("model", "").strip().lower()
    if not make:
        log.error("AutoScout24 requiere make.")
        return []

    url_make_only = f"https://www.autoscout24.de/lst/{autoscout_slug(make)}"
    url_with_model = f"{url_make_only}/{quote(model, safe='-')}" if model else url_make_only
    fallback_used = False

    cars = []

    for page in range(1, max_pages + 1):
        print(f"[AutoScout24] Página {page}/{max_pages}...", flush=True)

        params = {
            # Ordenar por precio ascendente (el más barato primero) en vez
            # de por fecha: así, con pocas páginas, cubrimos TODO lo que
            # entra en tu presupuesto, sin importar cuándo se publicó.
            # Antes ordenábamos por "age" (más reciente primero), lo que
            # hacía que anuncios más baratos pero más antiguos nunca
            # llegaran a verse si no estaban entre los últimos publicados.
            "sort": "price",
            "desc": "0",
            "atype": "C",
            "page": page,
            "priceto": config.get("max_price") or None,
            "kmto": config.get("max_km") or None,
            "zip": config.get("zip_code") or None,
            "zipr": config.get("radius") or None,
            "ust": "0",
        }
        params = {k: v for k, v in params.items() if v is not None}

        request_url = url_make_only if fallback_used else url_with_model
        response = get(session, request_url, params, "AutoScout24")

        if response is None and request_url != url_make_only:
            print("[AutoScout24] Ruta con modelo no válida; probando solo con la marca.", flush=True)
            fallback_used = True
            response = get(session, url_make_only, params, "AutoScout24")

        if not response:
            break

        soup = BeautifulSoup(response.text, "html.parser")
        page_title = soup.title.string.strip() if soup.title and soup.title.string else ""

        block_reason = blocked_title(page_title)
        if block_reason:
            log.error(
                "AutoScout24 parece haber bloqueado la petición (título: %r, marcador: %r).",
                page_title, block_reason,
            )
            break

        listings = soup.select("article")
        if not listings:
            print(f"[AutoScout24] Sin anuncios en página {page}, fin.", flush=True)
            print(f"[AutoScout24 DEBUG] Longitud HTML: {len(response.text)} caracteres.", flush=True)
            print(f"[AutoScout24 DEBUG] Fragmento: {response.text[:400]!r}", flush=True)
            break

        print(f"[AutoScout24] {len(listings)} tarjetas <article> encontradas en esta página.", flush=True)

        debug_shown = 0
        for item in listings:
            car = make_car(
                "AutoScout24",
                "https://www.autoscout24.de",
                item,
                "h2",
                '[data-testid="regular-price"], [class*="Price"]',
            )

            if debug_shown < 3:
                if car is None:
                    print("[AutoScout24 DEBUG] Item descartado por make_car (sin enlace válido o con IVA deducible).", flush=True)
                else:
                    print(
                        f"[AutoScout24 DEBUG] título={car['title']!r} precio={car['price']!r} "
                        f"num_precio={number(car['price'])!r} "
                        f"coincide_marca={matches_config(car, config)} "
                        f"dentro_de_limites={within_limits(car, config)}",
                        flush=True,
                    )
                debug_shown += 1

            if qualifies(car, config):
                cars.append(car)

        human_pause(2.0, 4.5)

    print(f"[AutoScout24] {len(cars)} anuncios válidos encontrados.", flush=True)
    return cars


# ---------------------------------------------------------------------------
# mobile.de (requiere Playwright: el listado se renderiza con JS)
# ---------------------------------------------------------------------------

def click_first_visible(locator, timeout=6000):
    try:
        count = locator.count()
    except Exception:
        return False

    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                candidate.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def accept_mobile_cookies(page):
    button_pattern = re.compile("Alle akzeptieren|Akzeptieren|Zustimmen|Accept all", re.IGNORECASE)

    for frame in page.frames:
        try:
            locator = frame.get_by_text(button_pattern)
            if locator.count() and click_first_visible(locator, timeout=4000):
                print("[mobile.de] Banner de cookies cerrado (iframe).", flush=True)
                return True
        except Exception:
            continue

    try:
        locator = page.get_by_text(button_pattern)
        if locator.count() and click_first_visible(locator, timeout=4000):
            print("[mobile.de] Banner de cookies cerrado (DOM principal).", flush=True)
            return True
    except Exception:
        pass

    print("[mobile.de] No se encontró banner de cookies (o ya estaba cerrado).", flush=True)
    return False


def mobile_select_value(page, field_pattern, field_label, value):
    print(f"[mobile.de] Seleccionando {field_label} = {value}...", flush=True)

    field_locator = page.get_by_text(re.compile(field_pattern, re.IGNORECASE), exact=False)
    if not click_first_visible(field_locator):
        raise RuntimeError(f"No se encontró el selector de {field_label}.")

    page.wait_for_timeout(400)
    inputs = page.locator("input:visible")
    if not inputs.count():
        raise RuntimeError(f"No se abrió el campo de {field_label}.")

    target_input = inputs.nth(inputs.count() - 1)
    target_input.fill(value)

    try:
        page.wait_for_selector(f"text=/^{re.escape(value)}/i", timeout=4000)
    except Exception:
        pass

    option = page.get_by_text(re.compile(rf"^{re.escape(value)}", re.IGNORECASE))
    if not click_first_visible(option):
        raise RuntimeError(f"mobile.de no ofreció una opción para {field_label}: {value}")

    print(f"[mobile.de] {field_label} seleccionado.", flush=True)


def fetch_mobile_de(config, _session=None, max_pages=MAX_PAGES):
    make = config.get("make", "").strip()
    model = config.get("model", "").strip()

    if not make:
        log.error("mobile.de requiere make.")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no está instalado (pip install playwright + playwright install chromium).")
        return []

    # playwright-stealth es opcional: si no está instalado EN EL SERVIDOR
    # donde corre este script (no basta con instalarlo en tu PC local), se
    # avisa y se continúa sin él en vez de romper todo el pipeline.
    try:
        from playwright_stealth import Stealth
        stealth_available = True
    except ImportError:
        stealth_available = False
        log.warning(
            "playwright-stealth no está instalado en este entorno; "
            "continuando sin evasión anti-bot adicional. "
            "Instálalo con: pip install playwright-stealth"
        )

    cars = []

    with sync_playwright() as playwright:
        browser = None
        context = None

        try:
            print("[mobile.de] Arrancando Chromium...", flush=True)
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="de-DE", user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            if stealth_available:
                Stealth().apply_stealth_sync(page)
                print("[mobile.de] Modo stealth activado.", flush=True)

            def block_heavy(route):
                if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy)

            print("[mobile.de] Cargando página de búsqueda...", flush=True)
            page.goto("https://www.mobile.de/fahrzeuge/search.html", wait_until="domcontentloaded", timeout=30000)

            accept_mobile_cookies(page)
            human_pause(0.8, 1.8)
            mobile_select_value(page, "Marke", "marca", make)
            human_pause(0.6, 1.4)
            if model:
                mobile_select_value(page, "Modell", "modelo", model)

            page.wait_for_timeout(random.randint(1200, 2200))

            for page_number in range(1, max_pages + 1):
                print(f"[mobile.de] Leyendo página {page_number}/{max_pages}...", flush=True)
                content = page.content()

                block_reason = blocked_title(page.title())
                if block_reason:
                    log.error(
                        "mobile.de parece haber bloqueado el navegador (título: %r, marcador: %r).",
                        page.title(), block_reason,
                    )
                    break

                soup = BeautifulSoup(content, "html.parser")
                listings = soup.select('[data-testid="result-listing"], div.cBox-body--resultitem')

                if not listings:
                    print(f"[mobile.de] Sin tarjetas de resultado en página {page_number}.", flush=True)
                    break

                for item in listings:
                    car = make_car(
                        "mobile.de",
                        "https://www.mobile.de",
                        item,
                        '[data-testid="result-title"], h2',
                        '[data-testid="price-label"], [class*="price"]',
                    )
                    if qualifies(car, config):
                        cars.append(car)

                if page_number == max_pages:
                    break

                next_button = page.locator('[data-testid="pagination-next-button"], a[rel="next"]')
                if not click_first_visible(next_button):
                    break

                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(random.randint(1000, 2000))

        except Exception as error:
            log.error("No se pudo automatizar mobile.de: %s", error)

        finally:
            if context:
                context.close()
            if browser:
                browser.close()

    print(f"[mobile.de] {len(cars)} anuncios válidos encontrados.", flush=True)
    return cars


# ---------------------------------------------------------------------------
# Kleinanzeigen
# ---------------------------------------------------------------------------

def pick_title(lines):
    """
    Elige una línea razonable como título entre las primeras del bloque de
    texto de la tarjeta. La primera línea de Kleinanzeigen suele ser solo
    una etiqueta corta (fecha, "TOP", código postal), no el título real del
    anuncio -- así que se descartan esas y se coge la primera línea "larga"
    que no sea un código postal + ciudad.
    """
    for line in lines[:6]:
        if re.match(r"^\d{5}\s", line):  # código postal + ciudad
            continue
        if re.match(r"^(heute|gestern),?\s*\d{1,2}:\d{2}$", line, re.I):  # "Heute, 09:13"
            continue
        if line.strip().upper() in ("TOP", "ANZEIGE", "GESPONSERT"):
            continue
        if len(line) >= 12:
            return line
    return lines[0] if lines else "Vehículo"


def fetch_kleinanzeigen(config, _session=None, max_pages=KLEINANZEIGEN_MAX_PAGES):
    """
    Kleinanzeigen rediseñó su web con Astro (renderizado del lado del
    cliente): los anuncios NO están en el HTML inicial, se cargan con
    JavaScript después. Por eso ya no se puede usar requests/BeautifulSoup
    aquí, igual que con mobile.de: hace falta un navegador real (Playwright).

    En vez de adivinar selectores de clases CSS (que cambian con cada
    rediseño), nos apoyamos en algo mucho más estable: todos los enlaces a
    un anuncio individual de Kleinanzeigen contienen "/s-anzeige/" en su URL,
    y eso lleva años sin cambiar pese a los rediseños del resto de la web.
    """
    query = " ".join(filter(None, (config.get("make", "").strip(), config.get("model", "").strip())))
    if not query:
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no está instalado (pip install playwright + playwright install chromium).")
        return []

    try:
        from playwright_stealth import Stealth
        stealth_available = True
    except ImportError:
        stealth_available = False

    slug = quote(re.sub(r"\s+", "-", query.strip().lower()), safe="-")
    cars = []

    with sync_playwright() as playwright:
        browser = None
        try:
            print("[Kleinanzeigen] Arrancando Chromium...", flush=True)
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="de-DE", user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            if stealth_available:
                Stealth().apply_stealth_sync(page)

            # Kleinanzeigen permite acotar por precio directamente en la
            # URL con el segmento "preis:MIN:MAX" (formato heredado de
            # eBay Kleinanzeigen; no verificado en vivo tras el rediseño
            # con Astro, pero suele mantenerse por compatibilidad SEO).
            # Además se intenta ordenar por precio ascendente. IMPORTANTE:
            # abre tú mismo esta URL exacta en un navegador normal para
            # confirmar visualmente que el precio máximo y el orden se
            # aplican de verdad -- la URL completa se imprime abajo.
            max_price_value = config.get("max_price") or ""
            price_segment = f"preis::{quote(str(max_price_value))}/" if max_price_value else ""
            url = (
                f"https://www.kleinanzeigen.de/s-autos/{price_segment}{slug}/k0c216"
                f"?sortingField=PRICE_AMOUNT"
            )
            print(f"[Kleinanzeigen] Cargando {url}...", flush=True)
            print(
                "[Kleinanzeigen] ⚠️ Comprueba manualmente esta URL en tu navegador: "
                "¿respeta el precio máximo y el orden ascendente por precio?",
                flush=True,
            )
            # "networkidle" falla a menudo en webs modernas (analíticas,
            # anuncios, websockets de fondo que nunca dejan la red "en
            # silencio"). En vez de eso, esperamos solo a que el HTML básico
            # cargue y luego a que aparezcan los enlaces de anuncio reales,
            # que es lo que de verdad necesitamos.
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            human_pause(2.0, 4.0)

            block_reason = blocked_title(page.title())
            if block_reason:
                log.error(
                    "Kleinanzeigen ha bloqueado la petición (título: %r, marcador: %r). "
                    "Se detiene esta plataforma en este ciclo.",
                    page.title(), block_reason,
                )
                return []

            try:
                page.wait_for_selector('a[href*="/s-anzeige/"]', timeout=15000)
            except Exception:
                print(
                    "[Kleinanzeigen] No aparecieron enlaces de anuncio tras 15s "
                    "de espera (puede que no haya resultados, o que la página "
                    "tarde más de lo esperado en cargar).",
                    flush=True,
                )

            # Se extraen los datos directamente en el navegador (más fiable
            # que descargar el HTML y volver a parsearlo aparte), subiendo
            # desde cada enlace "/s-anzeige/" hasta su contenedor (article/
            # li/div) para sacar el texto e imagen de ese anuncio concreto.
            raw_items = page.eval_on_selector_all(
                'a[href*="/s-anzeige/"]',
                """
                els => els.map(a => {
                    // Se prefieren contenedores "de verdad" de la tarjeta del
                    // anuncio. OJO: closest('article, li, div') es un error
                    // habitual, porque 'div' es tan genérico que casi siempre
                    // encuentra un <div> diminuto (el que envuelve solo la
                    // imagen o el enlace) ANTES de llegar al contenedor real
                    // que tiene el precio y el título -> por eso salía
                    // siempre "Consultar" como precio.
                    let container = a.closest('article, li[data-adid], li');

                    if (!container) {
                        // Último recurso: subir manualmente hasta encontrar
                        // un antepasado cuyo texto SÍ contenga un símbolo de
                        // moneda (señal de que ya llegamos a la tarjeta
                        // completa, no solo a un trozo suyo).
                        let node = a;
                        for (let i = 0; i < 6 && node.parentElement; i++) {
                            node = node.parentElement;
                            if (node.innerText && node.innerText.includes('€')) {
                                container = node;
                                break;
                            }
                        }
                        if (!container) container = a.parentElement || a;
                    }

                    const img = container.querySelector('img');
                    return {
                        href: a.href,
                        text: container.innerText || a.innerText || '',
                        img: img ? (img.src || img.getAttribute('data-src')) : null
                    };
                })
                """,
            )
            print(f"[Kleinanzeigen] {len(raw_items)} enlaces de anuncio detectados.", flush=True)

            # DEBUG TEMPORAL: muestra el texto crudo de los primeros 3
            # anuncios para poder ver exactamente qué estamos capturando,
            # sin tener que adivinar más a ciegas.
            for i, raw in enumerate(raw_items[:3]):
                preview = (raw["text"] or "")[:200].replace("\n", " | ")
                print(f"[Kleinanzeigen DEBUG] Item {i}: {preview!r}", flush=True)

            seen_links = set()
            rejected_no_price = 0
            rejected_no_match = 0
            debug_shown = 0

            for raw in raw_items:
                link = clean_link(raw["href"])
                if link in seen_links or not link.startswith(("http://", "https://")):
                    continue
                seen_links.add(link)

                listing_text = raw["text"] or ""
                if has_deductible_vat(listing_text):
                    continue

                lines = [line.strip() for line in listing_text.split("\n") if line.strip()]
                title = pick_title(lines)
                price = next((line for line in lines if "€" in line), "Consultar")

                car = {
                    "id": f"kleinanzeigen:{link}",
                    "title": title,
                    "price": price,
                    "link": link,
                    "image": raw["img"],
                    "platform": "Kleinanzeigen",
                    "_listing_text": listing_text,
                    "seller_type": "Sin IVA deducible detectado",
                }

                match_ok = matches_config(car, config)
                limits_ok = within_limits(car, config)

                if debug_shown < 3:
                    print(
                        f"[Kleinanzeigen DEBUG] título={title!r} precio={price!r} "
                        f"num_precio={number(price)!r} coincide_marca={match_ok} "
                        f"dentro_de_limites={limits_ok}",
                        flush=True,
                    )
                    debug_shown += 1

                if match_ok and limits_ok:
                    cars.append(car)
                elif not limits_ok:
                    rejected_no_price += 1
                elif not match_ok:
                    rejected_no_match += 1

            if rejected_no_price or rejected_no_match:
                print(
                    f"[Kleinanzeigen DEBUG] Descartados por precio/km no válido: "
                    f"{rejected_no_price}. Descartados por no coincidir marca/modelo: "
                    f"{rejected_no_match}.",
                    flush=True,
                )

        except Exception as error:
            log.error("No se pudo automatizar Kleinanzeigen: %s", error)

        finally:
            if browser:
                browser.close()

    print(f"[Kleinanzeigen] {len(cars)} anuncios válidos encontrados.", flush=True)
    return cars


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def telegram_request(method, payload):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload,
            timeout=(10, 25),
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            log.error("Telegram rechazó %s: %s", method, response.text)
            return False
        return True
    except (requests.RequestException, ValueError) as error:
        log.error("Error llamando a Telegram (%s): %s", method, error)
        return False


def send_telegram(car):
    title = html.escape(car["title"][:600])
    price = html.escape(car["price"][:120])
    platform = html.escape(car["platform"])
    regime = html.escape(car["seller_type"])
    link = html.escape(car["link"], quote=True)

    text = (
        "🚨 <b>NUEVO COCHE ENCONTRADO</b> 🚨\n\n"
        f"🌐 <b>Plataforma:</b> {platform}\n"
        f"🚗 <b>{title}</b>\n"
        f"💰 <b>Precio:</b> {price}\n"
        f"👤 <b>Filtro:</b> {regime}\n\n"
        f'<a href="{link}">Ver anuncio directo</a>'
    )

    if car.get("image") and len(text) <= 1024:
        if telegram_request(
            "sendPhoto",
            {"chat_id": CHAT_ID, "photo": car["image"], "caption": text, "parse_mode": "HTML"},
        ):
            return True

    return telegram_request(
        "sendMessage",
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
    )


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def run_pipeline(platforms=None):
    """
    platforms: lista opcional de claves a rastrear ("autoscout", "mobile",
    "kleinanzeigen"). Si es None, se rastrean las 3. Permite lanzar una
    plataforma sola desde el botón del bot, en vez de golpear las 3 a la vez.
    """
    config = load_config()
    seen = load_seen()

    print(
        f"[Config actual] marca={config.get('make')!r} modelo={config.get('model')!r} "
        f"max_price={config.get('max_price')!r} max_km={config.get('max_km')!r}",
        flush=True,
    )

    session = requests.Session()
    session.headers.update(HEADERS)

    fetchers = {
        "autoscout": ("AutoScout24", fetch_autoscout, True),
        "mobile": ("mobile.de", fetch_mobile_de, False),
        "kleinanzeigen": ("Kleinanzeigen", fetch_kleinanzeigen, False),
    }
    selected = platforms if platforms else list(fetchers.keys())

    all_cars = []
    for key in selected:
        if key not in fetchers:
            log.warning("Plataforma desconocida ignorada: %s", key)
            continue

        name, fetcher, needs_session = fetchers[key]
        try:
            cars = fetcher(config, session) if needs_session else fetcher(config)
            log.info("%s: %d anuncios encontrados.", name, len(cars))
            all_cars.extend(cars)
        except Exception:
            log.exception("Error inesperado rastreando %s", name)

    unique_cars = {car["id"]: car for car in all_cars}
    sent = 0

    for car in unique_cars.values():
        if car["id"] in seen:
            continue

        if send_telegram(car):
            seen[car["id"]] = time.time()
            save_seen(seen)
            sent += 1

        time.sleep(1)

    save_seen(seen)
    print(f"Ciclo finalizado. Avisos enviados: {sent}", flush=True)
