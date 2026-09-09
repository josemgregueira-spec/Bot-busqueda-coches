import html
import json
import logging
import os
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
SEEN_RETENTION_DAYS = int(os.getenv("SEEN_RETENTION_DAYS", "90"))

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


def blocked(content):
    content = content.lower()
    markers = (
        "captcha",
        "access denied",
        "verify you are human",
        "unusual traffic",
        "bestätigen sie, dass sie ein mensch sind",
        "zugriff verweigert",
        "ungewöhnlicher datenverkehr",
    )
    return any(marker in content for marker in markers)


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
        match = re.search(r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)\s*km", car["_listing_text"], re.I)
        km = int(re.sub(r"\D", "", match.group(0))) if match else None
        if km is None or km > max_km:
            return False

    return True


def qualifies(car, config):
    return bool(car) and matches_config(car, config) and within_limits(car, config)


# ---------------------------------------------------------------------------
# AutoScout24
# ---------------------------------------------------------------------------

def fetch_autoscout(config, session, max_pages=MAX_PAGES):
    make = config.get("make", "").strip().lower()
    model = config.get("model", "").strip().lower()
    if not make:
        log.error("AutoScout24 requiere make.")
        return []

    url_make_only = f"https://www.autoscout24.de/lst/{quote(make, safe='-')}"
    url_with_model = f"{url_make_only}/{quote(model, safe='-')}" if model else url_make_only
    fallback_used = False

    cars = []

    for page in range(1, max_pages + 1):
        print(f"[AutoScout24] Página {page}/{max_pages}...", flush=True)

        params = {
            "sort": "age",
            "desc": "1",
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

        if blocked(response.text):
            log.error("AutoScout24 parece haber bloqueado la petición.")
            break

        listings = BeautifulSoup(response.text, "html.parser").select("article")
        if not listings:
            print(f"[AutoScout24] Sin anuncios en página {page}, fin.", flush=True)
            break

        for item in listings:
            car = make_car(
                "AutoScout24",
                "https://www.autoscout24.de",
                item,
                "h2",
                '[data-testid="regular-price"], [class*="Price"]',
            )
            if qualifies(car, config):
                cars.append(car)

        time.sleep(1.5)

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
    if not make or not model:
        log.error("mobile.de requiere make y model.")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no está instalado (pip install playwright + playwright install chromium).")
        return []

    cars = []

    with sync_playwright() as playwright:
        browser = None
        context = None

        try:
            print("[mobile.de] Arrancando Chromium...", flush=True)
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="de-DE", user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            def block_heavy(route):
                if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy)

            print("[mobile.de] Cargando página de búsqueda...", flush=True)
            page.goto("https://www.mobile.de/fahrzeuge/search.html", wait_until="domcontentloaded", timeout=30000)

            accept_mobile_cookies(page)
            mobile_select_value(page, "Marke", "marca", make)
            if model:
                mobile_select_value(page, "Modell", "modelo", model)

            page.wait_for_timeout(1500)

            for page_number in range(1, max_pages + 1):
                print(f"[mobile.de] Leyendo página {page_number}/{max_pages}...", flush=True)
                content = page.content()

                if blocked(content):
                    log.error("mobile.de parece haber bloqueado el navegador.")
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
                page.wait_for_timeout(1200)

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

def fetch_kleinanzeigen(config, session, max_pages=MAX_PAGES):
    match_term = (config.get("version") or config.get("model", "")).strip()
    query = " ".join(filter(None, (config.get("make", "").strip(), match_term)))
    if not query:
        return []

    slug = quote(re.sub(r"\s+", "-", query.strip().lower()), safe="-")
    cars = []

    for page in range(1, max_pages + 1):
        print(f"[Kleinanzeigen] Página {page}/{max_pages}...", flush=True)
        prefix = f"seite:{page}/" if page > 1 else ""
        url = f"https://www.kleinanzeigen.de/s-autos/{prefix}{slug}/k0c216"

        params = {
            "maxPrice": config.get("max_price") or None,
            "locationStr": config.get("zip_code") or None,
            "radius": config.get("radius") or None,
            "sortingField": "SORTING_DATE",
        }
        params = {k: v for k, v in params.items() if v is not None}

        response = get(session, url, params, "Kleinanzeigen")
        if not response:
            break

        if blocked(response.text):
            log.error("Kleinanzeigen parece haber bloqueado la petición.")
            break

        listings = BeautifulSoup(response.text, "html.parser").select("article.aditem, li.ad-listitem article")
        if not listings:
            print(f"[Kleinanzeigen] Sin anuncios en página {page}, fin.", flush=True)
            break

        for item in listings:
            car = make_car(
                "Kleinanzeigen",
                "https://www.kleinanzeigen.de",
                item,
                ".text-module-begin, h2",
                ".aditem-main--middle--price-shipping--price, [class*='price']",
            )
            if qualifies(car, config):
                cars.append(car)

        time.sleep(1.5)

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

def run_pipeline():
    config = load_config()
    seen = load_seen()

    session = requests.Session()
    session.headers.update(HEADERS)

    all_cars = []
    for name, fetcher, needs_session in (
        ("AutoScout24", fetch_autoscout, True),
        ("mobile.de", fetch_mobile_de, False),
        ("Kleinanzeigen", fetch_kleinanzeigen, True),
    ):
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
