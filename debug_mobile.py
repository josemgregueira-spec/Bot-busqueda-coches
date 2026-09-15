"""
Script de diagnóstico para mobile.de. NO hace scraping real, solo abre la
página de búsqueda con Playwright y lista los botones/campos que encuentra,
para poder identificar los textos/selectores correctos sin adivinar a ciegas.

Uso (en el servidor, dentro de la carpeta del proyecto):
    python3 debug_mobile.py
"""
from playwright.sync_api import sync_playwright

URL = "https://www.mobile.de/fahrzeuge/search.html"


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="de-DE")
        page = context.new_page()

        print(f"Cargando {URL} ...")
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        print("\n=== TÍTULO DE LA PÁGINA ===")
        print(page.title())

        print("\n=== URL FINAL (tras posibles redirecciones) ===")
        print(page.url)

        print("\n=== BOTONES / CAMPOS / ETIQUETAS VISIBLES ===")
        elements = page.eval_on_selector_all(
            "button, input, label, [role=button], [role=combobox]",
            """
            els => els.map(e => ({
                tag: e.tagName,
                text: (e.innerText || '').trim().slice(0, 50),
                placeholder: e.getAttribute('placeholder'),
                name: e.getAttribute('name'),
                id: e.id,
                testid: e.getAttribute('data-testid'),
                visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
            }))
            """,
        )

        for element in elements:
            if not element["visible"]:
                continue
            if not (element["text"] or element["placeholder"] or element["testid"]):
                continue
            print(element)

        print("\n=== LONGITUD TOTAL DEL HTML RENDERIZADO ===")
        content = page.content()
        print(f"{len(content)} caracteres")

        # Guarda el HTML completo en un archivo por si hace falta revisarlo
        # con más detalle (buscar texto concreto, etc.)
        with open("mobile_debug.html", "w", encoding="utf-8") as f:
            f.write(content)
        print("\nHTML completo guardado en mobile_debug.html")

        # Guarda también una captura de pantalla visual
        page.screenshot(path="mobile_debug.png", full_page=True)
        print("Captura de pantalla guardada en mobile_debug.png")

        browser.close()


if __name__ == "__main__":
    main()
