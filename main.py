import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "")
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "3600"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "3"))
SEEN_RETENTION_DAYS = int(os.getenv("SEEN_RETENTION_DAYS", "90"))

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "searches_config.json"
SEEN_FILE = BASE_DIR / "seen_cars.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as error:
        log.error("No se pudo leer %s: %s", path, error)
        return default


def save_json_atomic(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    temporary.replace(path)


def normalise_seen(raw_seen):
    now = time.time()

    if isinstance(raw_seen, list):
        return {str(car_id): now for car_id in raw_seen}

    if isinstance(raw_seen, dict):
        return {
            str(car_id): float(saved_at)
            for car_id, saved_at in raw_seen.items()
            if isinstance(saved_at, (int, float))
        }

    return {}


def prune_seen(seen):
    oldest = time.time() - SEEN_RETENTION_DAYS * 86400

    return {
        car_id: saved_at
        for car_id, saved_at in seen.items()
        if saved_at >= oldest
    }


def get_with_retries(session, url, *, params, platform):
    for attempt in range(1, 4):
        try:
            response = session.get(url, params=params, timeout=(10, 25))

            if response.status_code == 200:
                return response

            if response.status_code not in {429, 500, 502, 503, 504}:
                log.warning(
                    "%s devolvió HTTP %s: %s",
                    platform,
                    response.status_code,
                    response.url,
                )
                return None

            delay = int(response.headers.get("Retry-After", attempt * 3))
            log.warning(
                "%s devolvió HTTP %s; reintento en %ss.",
                platform,
                response.status_code,
                delay,
            )

        except requests.RequestException as error:
            delay = attempt * 3
            log.warning(
                "Error consultando %s (%s/3): %s",
                platform,
                attempt,
                error,
            )

        time.sleep(delay)

    return None


def page_looks_blocked(content):
    content = content.lower()

    return any(
        marker in content
        for marker in (
            "captcha",
            "verify you are human",
            "access denied",
            "unusual traffic",
        )
    )


def clean_text(element, default="Consultar"):
    return element.get_text(" ", strip=True) if element else default


def clean_url(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    filtered_query = {
        k: v for k, v in query.items() if not k.startswith(("utm_", "ref"))
    }

    new_query = urlencode(filtered_query, doseq=True)
    return parsed._replace(query=new_query, fragment="").geturl()


def image_url(element):
    if not element:
        return None

    return (
        element.get("src")
        or element.get("data-src")
        or element.get("data-lazy-src")
    )


def has_deductible_vat(text):
    normalized_text = " ".join(text.lower().split())

    return any(
        term in normalized_text
        for term in (
            "mwst. ausweisbar",
            "zzgl. mwst",
            "vat reclaimable",
            "mehrwertsteuer ausweisbar",
        )
    )


def normalized(value):
    value = unicodedata.normalize(
        "NFKD",
        str(value),
    ).encode("ascii", "ignore").decode()

    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def first_number(value):
    match = re.search(
        r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)",
        str(value),
    )

    return int(re.sub(r"\D", "", match.group(0))) if match else None


def matches_requested_car(car, config):
    title = normalized(car["title"])

    return all(
        normalized(value) in title
        for value in (
            config.get("make", ""),
            config.get("model", ""),
        )
        if normalized(value)
    )


def within_limits(car, config):
    max_price = first_number(config.get("max_price", ""))
    price = first_number(car["price"])

    if max_price is not None and (price is None or price > max_price):
        return False

    max_km = first_number(config.get("max_km", ""))

    if max_km is not None:
        match = re.search(
            r"(?:\d{1,3}(?:[.\s]\d{3})+|\d+)\s*km",
            car["_listing_text"],
            re.I,
        )

        km = int(re.sub(r"\D", "", match.group(0))) if match else None

        if km is None or km > max_km:
            return False

    return True


def make_car(platform, base_url, item, title_selector, price_selector):
    listing_text = clean_text(item, "")

    if has_deductible_vat(listing_text):
        return None

    link_element = item.select_one("a[href]")

    if not link_element:
        return None

    raw_link = urljoin(base_url, link_element["href"])
    link = clean_url(raw_link)

    if not link.startswith(("https://", "http://")):
        return None

    return {
        "id": f"{platform.lower()}:{link}",
        "title": clean_text(
            item.select_one(title_selector),
            clean_text(link_element, "Vehículo"),
        ),
        "price": clean_text(item.select_one(price_selector)),
        "link": link,
        "image": image_url(item.select_one("img")),
        "platform": platform,
        "_listing_text": listing_text,
        "seller_type": "Sin IVA deducible detectado",
    }


def fetch_autoscout(config, session, max_pages=MAX_PAGES):
    make = config.get("make", "").strip().lower()
    model = config.get("model", "").strip().lower()

    if not make or not model:
        log.error("AutoScout24 requiere make y model.")
        return []

    cars = []

    url = (
        "https://www.autoscout24.de/lst/"
        f"{quote(make, safe='-')}/{quote(model, safe='-')}"
    )

    for page_number in range(1, max_pages + 1):
        params = {
            "sort": "age",
            "desc": "1",
            "atype": "C",
            "page": page_number,
            "priceto": config.get("max_price") or None,
            "kmto": config.get("max_km") or None,
            "zip": config.get("zip_code") or None,
            "zipr": config.get("radius") or None,
            "ust": "0",
        }

        params = {
            key: value
            for key, value in params.items()
            if value is not None
        }

        response = get_with_retries(
            session,
            url,
            params=params,
            platform="AutoScout24",
        )

        if not response:
            break

        if page_looks_blocked(response.text):
            log.error("AutoScout24 parece haber bloqueado la petición.")
            break

        listings = BeautifulSoup(
            response.text,
            "html.parser",
        ).select("article")

        if not listings:
            break

        for item in listings:
            car = make_car(
                "AutoScout24",
                "https://www.autoscout24.de",
                item,
                "h2",
                '[data-testid="regular-price"], [class*="Price"]',
            )

            if car and matches_requested_car(car, config) and within_limits(car, config):
                cars.append(car)

        time.sleep(1.5)

    return cars


def click_visible(locator, timeout=8000):
    for index in range(locator.count()):
        candidate = locator.nth(index)

        if candidate.is_visible():
            candidate.click(timeout=timeout)
            return True

    return False


def mobile_select_value(page, field, value):
    field_locator = page.get_by_text(
        re.compile(field, re.IGNORECASE),
        exact=False,
    )

    if not click_visible(field_locator):
        raise RuntimeError(f"No se encontró el selector de {field}.")

    inputs = page.locator("input:visible")

    if not inputs.count():
        raise RuntimeError(f"No se abrió el campo de {field}.")

    inputs.nth(inputs.count() - 1).fill(value)
    page.wait_for_timeout(600)

    option = page.get_by_text(
        re.compile(rf"^{re.escape(value)}$", re.IGNORECASE)
    )

    if not click_visible(option):
        raise RuntimeError(
            f"mobile.de no ofreció una opción exacta para {field}: {value}"
        )


def fetch_mobile_de(config, _session, max_pages=MAX_PAGES):
    make = config.get("make", "").strip()
    model = config.get("model", "").strip()

    if not make or not model:
        log.error("mobile.de requiere make y model.")
        return []

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "Instala Playwright: pip install playwright ; "
            "playwright install chromium"
        )
        return []

    cars = []

    with sync_playwright() as playwright:
        browser = None
        context = None

        try:
            browser = playwright.chromium.launch(headless=True)

            context = browser.new_context(
                locale="de-DE",
                user_agent=HEADERS["User-Agent"],
            )

            page = context.new_page()

            def block_heavy_resources(route):
                if route.request.resource_type in ["image", "stylesheet", "font", "media"]:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", block_heavy_resources)

            page.goto(
                "https://www.mobile.de/fahrzeuge/search.html",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            try:
                click_visible(
                    page.get_by_text(
                        re.compile("Alle akzeptieren|Akzeptieren", re.IGNORECASE)
                    ),
                    timeout=3000,
                )
            except PlaywrightTimeoutError:
                pass

            mobile_select_value(page, "Marke", make)
            mobile_select_value(page, "Modell", model)

            page.wait_for_timeout(1500)

            for page_number in range(1, max_pages + 1):
                content = page.content()

                if page_looks_blocked(content):
                    log.error("mobile.de parece haber bloqueado el navegador.")
                    break

                soup = BeautifulSoup(content, "html.parser")

                listings = soup.select(
                    '[data-testid="result-listing"], div.cBox-body--resultitem'
                )

                if not listings:
                    log.warning("mobile.de no devolvió tarjetas de resultado.")
                    break

                for item in listings:
                    car = make_car(
                        "mobile.de",
                        "https://www.mobile.de",
                        item,
                        '[data-testid="result-title"], h2',
                        '[data-testid="price-label"], [class*="price"]',
                    )

                    if (
                        car
                        and matches_requested_car(car, config)
                        and within_limits(car, config)
                    ):
                        cars.append(car)

                if page_number == max_pages:
                    break

                next_button = page.locator(
                    '[data-testid="pagination-next-button"], a[rel="next"]'
                )

                if not click_visible(next_button):
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

    return cars


def kleinanzeigen_slug(query):
    return quote(
        re.sub(r"\s+", "-", query.strip().lower()),
        safe="-",
    )


def fetch_kleinanzeigen(config, session, max_pages=MAX_PAGES):
    query = " ".join(
        filter(
            None,
            (
                config.get("make", "").strip(),
                config.get("model", "").strip(),
            ),
        )
    )

    if not query:
        return []

    cars = []
    slug = kleinanzeigen_slug(query)

    for page_number in range(1, max_pages + 1):
        prefix = f"seite:{page_number}/" if page_number > 1 else ""

        url = (
            "https://www.kleinanzeigen.de/s-autos/"
            f"{prefix}{slug}/k0c216"
        )

        params = {
            "maxPrice": config.get("max_price") or None,
            "locationStr": config.get("zip_code") or None,
            "radius": config.get("radius") or None,
            "sortingField": "SORTING_DATE",
        }

        params = {
            key: value
            for key, value in params.items()
            if value is not None
        }

        response = get_with_retries(
            session,
            url,
            params=params,
            platform="Kleinanzeigen",
        )

        if not response:
            break

        if page_looks_blocked(response.text):
            log.error("Kleinanzeigen parece haber bloqueado la petición.")
            break

        listings = BeautifulSoup(
            response.text,
            "html.parser",
        ).select("article.aditem, li.ad-listitem article")

        if not listings:
            break

        for item in listings:
            car = make_car(
                "Kleinanzeigen",
                "https://www.kleinanzeigen.de",
                item,
                ".text-module-begin, h2",
                ".aditem-main--middle--price-shipping--price, [class*='price']",
            )

            if car and matches_requested_car(car, config) and within_limits(car, config):
                cars.append(car)

        time.sleep(1.5)

    return cars


def telegram_request(method, payload):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload,
            timeout=(10, 25),
        )

        response.raise_for_status()

        if not response.json().get("ok"):
            log.error("Telegram rechazó el mensaje: %s", response.text)
            return False

        return True

    except (requests.RequestException, ValueError) as error:
        log.error("Error enviando a Telegram: %s", error)
        return False


def send_telegram_message(car):
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
            {
                "chat_id": CHAT_ID,
                "photo": car["image"],
                "caption": text,
                "parse_mode": "HTML",
            },
        ):
            return True

    return telegram_request(
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


def run_pipeline():
    config = load_json(
        DATA_FILE,
        {
            "make": "Volkswagen",
            "model": "Golf",
            "max_price": "20000",
            "max_km": "120000",
            "zip_code": "",
            "radius": "",
        },
    )

    seen = prune_seen(normalise_seen(load_json(SEEN_FILE, {})))

    session = requests.Session()
    session.headers.update(HEADERS)

    all_cars = []

    for name, fetcher in (
        ("AutoScout24", fetch_autoscout),
        ("mobile.de", fetch_mobile_de),
        ("Kleinanzeigen", fetch_kleinanzeigen),
    ):
        try:
            cars = fetcher(config, session)
            log.info("%s: %d anuncios encontrados.", name, len(cars))
            all_cars.extend(cars)
        except Exception:
            log.exception("Error inesperado rastreando %s", name)

    unique_cars = {car["id"]: car for car in all_cars}

    for car in unique_cars.values():
        if car["id"] in seen:
            continue

        if send_telegram_message(car):
            seen[car["id"]] = time.time()
            save_json_atomic(SEEN_FILE, prune_seen(seen))

        time.sleep(1)

    save_json_atomic(SEEN_FILE, prune_seen(seen))


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not CHAT_ID:
        sys.exit("Configura TELEGRAM_TOKEN y CHAT_ID antes de iniciar el bot.")

    if INTERVAL_SECONDS == 0:
        log.info("Ejecutando iteración única para GitHub Actions...")
        run_pipeline()
    else:
        while True:
            try:
                run_pipeline()
            except Exception:
                log.exception("Error no controlado en el ciclo")

            log.info(
                "Esperando %.0f minutos para la siguiente búsqueda.",
                INTERVAL_SECONDS / 60,
            )

            time.sleep(INTERVAL_SECONDS)
