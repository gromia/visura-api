from bs4 import BeautifulSoup
import time

def parse_table(html):
    soup = BeautifulSoup(html, "html.parser")
    headers = [th.get_text(strip=True) for th in soup.find_all("th")]
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells:
            # Se ci sono meno celle che header, aggiungi celle vuote
            while len(cells) < len(headers):
                cells.append("")
            rows.append(dict(zip(headers, cells)))
    return rows

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
import os
import re

async def login(page: Page):
    ade_username = os.getenv("ADE_USERNAME")
    ade_password = os.getenv("ADE_PASSWORD")
    
    if not ade_username or not ade_password:
        raise ValueError("ADE_USERNAME and ADE_PASSWORD environment variables must be set")
    
    print("[LOGIN] Navigo alla pagina di login...")
    await page.goto("https://iampe.agenziaentrate.gov.it/sam/UI/Login?realm=/agenziaentrate")
    print("[LOGIN] Clicco 'Entra con SPID'...")
    await page.get_by_role("button", name="Entra con SPID").click()
    print("[LOGIN] Clicco 'Sielte ID'...")
    await page.get_by_role("link", name="Sielte ID").click()
    print("[LOGIN] Inserisco username...")
    await page.get_by_role("textbox", name="Codice Fiscale / Partita IVA").press("CapsLock")
    await page.get_by_role("textbox", name="Codice Fiscale / Partita IVA").fill(ade_username)
    print("[LOGIN] Inserisco password...")
    await page.get_by_role("textbox", name="Password").click()
    await page.get_by_role("textbox", name="Password").fill(ade_password)
    print("[LOGIN] Clicco 'Prosegui'...")
    await page.get_by_role("button", name="Prosegui").click()
    print("[LOGIN] Cerco link notifica (può non esserci)...")
    try:
        await page.get_by_role("link", name="Utilizza il le notifiche Ricevi una notifica sull'app MySielteID").click(timeout=4000)
        print("[LOGIN] Cliccato link notifica (testo completo).")
    except PlaywrightTimeoutError:
        print("[LOGIN] Link notifica con testo completo non trovato, provo fallback...")
        # Fallback: cerca <a> con alt="Utilizza il le notifiche" e <p> con testo 'Ricevi una notifica sull'app MySielteID'
        try:
            await page.locator('a.link-sso:has(img[alt="Utilizza il le notifiche"]):has(p:text("Ricevi una notifica sull\'app MySielteID"))').click(timeout=4000)
            print("[LOGIN] Cliccato link notifica (fallback DOM selector).")
        except PlaywrightTimeoutError:
            print("[LOGIN] Nessun link notifica trovato, stampo contenuto pagina per debug:")
            content = await page.content()
            print("[LOGIN][DEBUG] HTML pagina:\n" + content)
            print("[LOGIN] Continuo comunque.")
    # Il pulsante "Autorizza" compare solo dopo l'approvazione sull'app MySielteID: 90 secondi per farlo
    print("[LOGIN] Attendo l'approvazione su MySielteID e clicco 'Autorizza'...")
    await page.get_by_role("button", name="Autorizza").click(timeout=90000)
    try:
        await page.wait_for_load_state("networkidle", timeout=60000)
    except PlaywrightTimeoutError:
        pass
    await dump_page(page, "dopo Autorizza")
    try:
        await open_sister(page)
    except Exception:
        await dump_page(page, "apertura SISTER non riuscita")
        raise

    print("[LOGIN] Attendo caricamento pagina...")
    await page.wait_for_load_state("networkidle")
    print("[LOGIN] Controllo blocco sessione...")
    content = await page.content()
    url = page.url
    if (
        "Utente gia' in sessione" in content
        or "error_locked.jsp" in url
    ):
        print("[LOGIN][ERRORE] Utente già in sessione su un'altra postazione!")
        raise Exception("Utente già in sessione su un'altra postazione")

    try:
        print("[LOGIN] Clicco 'Conferma'...")
        await page.get_by_role("button", name="Conferma").click()
        print("[LOGIN] Clicco 'Consultazioni e Certificazioni'...")
        await page.get_by_role("link", name="Consultazioni e Certificazioni").click()
        print("[LOGIN] Clicco 'Visure catastali'...")
        await page.get_by_role("link", name="Visure catastali").click()
        print("[LOGIN] Clicco 'Conferma Lettura'...")
        await page.get_by_role("link", name="Conferma Lettura").click()
    except Exception:
        await dump_page(page, "menu SISTER non riuscito")
        raise
    print("[LOGIN] Login completato, SISTER pronto.")


async def dump_page(page: Page, tag: str):
    """Scrive nei log cosa c'è nella pagina (indirizzo, titolo, link, pulsanti, campi, testo) per capire dove si è fermato il login."""
    try:
        info = await page.evaluate("""() => {
            const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
            const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
            return {
                links: [...document.querySelectorAll('a')].filter(vis).slice(0, 80).map(a => txt(a) + ' -> ' + (a.getAttribute('href') || '')),
                buttons: [...document.querySelectorAll('button, input[type=submit], input[type=button]')].filter(vis).slice(0, 40).map(txt),
                inputs: [...document.querySelectorAll('input:not([type=hidden]), textarea')].filter(vis).slice(0, 30)
                    .map(i => [i.type, i.name, i.id, i.placeholder, i.getAttribute('aria-label')].filter(Boolean).join(' | ')),
                text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 1500),
            };
        }""")
        print(f"[LOGIN][PAGINA] {tag}: url={page.url} titolo={await page.title()!r} schede={len(page.context.pages)}")
        for k in ("links", "buttons", "inputs"):
            for v in info[k]:
                print(f"[LOGIN][PAGINA]   {k}: {v}")
        print(f"[LOGIN][PAGINA]   testo: {info['text']}")
    except Exception as e:
        print(f"[LOGIN][PAGINA] {tag}: impossibile leggere la pagina ({e})")


async def open_sister(page: Page):
    """Dall'area riservata dell'Agenzia delle Entrate apre SISTER: ricerca dei servizi, poi "Vai al servizio".
    La casella di ricerca è doppia (una nascosta per il menu dei telefoni): conta solo quella visibile.
    Se SISTER si apre in una nuova scheda, la si porta nella scheda di lavoro, l'unica che il servizio usa."""
    search = page.locator(
        "input[name='cerca']:visible, input[aria-label*='Cerca il servizio' i]:visible, "
        "input[placeholder*='Cerca il servizio' i]:visible, input[type=search]:visible"
    ).first
    try:
        await search.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeoutError:
        print("[LOGIN] Ricerca dei servizi non trovata, cerco un link a SISTER...")
        await page.locator("a[href*='sister' i]").or_(page.get_by_role("link", name=re.compile("sister", re.I))).first.click(timeout=15000)
        return
    print("[LOGIN] Cerco servizio SISTER...")
    await search.fill("SISTER")
    await search.press("Enter")
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    go = page.get_by_role("link", name=re.compile("vai al servizio", re.I)).or_(page.get_by_role("button", name=re.compile("vai al servizio", re.I))).first
    try:
        await go.wait_for(state="visible", timeout=20000)
    except PlaywrightTimeoutError:
        await dump_page(page, "risultati della ricerca SISTER")
        raise
    pages_before = len(page.context.pages)
    print("[LOGIN] Clicco 'Vai al servizio'...")
    await go.click()
    await page.wait_for_timeout(3000)
    if len(page.context.pages) > pages_before:
        tab = page.context.pages[-1]
        await tab.wait_for_load_state()
        print(f"[LOGIN] SISTER aperto in una nuova scheda ({tab.url}), lo porto nella scheda di lavoro")
        url = tab.url
        await tab.close()
        await page.goto(url, timeout=60000)
