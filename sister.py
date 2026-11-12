"""Navigazione di SISTER (Agenzia delle Entrate) con Playwright.

Il percorso e i selettori sono quelli di gromia/sister-parser, il crawler che gira in produzione con
decine di utenze: login dalla pagina unica dell'Agenzia (scheda "Sister"), conferma dell'informativa
privacy, Consultazioni e Certificazioni > Visure catastali > Conferma Lettura > convenzione > ufficio
provinciale ("MILANO Territorio"), poi le ricerche del menu a sinistra (Immobile, Indirizzo, Persona
fisica, Persona giuridica). Ogni elenco di SISTER è una table.listaIsp4 con un radio per riga; il pulsante
"intestati" apre i titolari della riga scelta e "indietro" torna all'elenco.

SISTER consente una sola sessione per utenza: chi la usa deve uscire (CloseSessionsSis e logout della
pagina unica), altrimenti chiunque altro, anche a mano dal browser, trova "Utente gia' in sessione" finché
la sessione non scade sul server. Per questo una Sessione si apre per un gruppo di ricerche e si chiude
subito dopo (vedi main.py): non si tiene aperta.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PWTimeout

from province import ufficio_per

log = logging.getLogger("sister")

SISTER_HOME = os.environ.get("SISTER_HOME", "https://sister.agenziaentrate.gov.it/Servizi/")
SSO_LOGIN = os.environ.get(
    "SISTER_LOGIN_URL",
    "https://iampe.agenziaentrate.gov.it/sam/UI/Login?realm=/agenziaentrate&goto=" + quote(SISTER_HOME, safe=""),
)
SSO_LOGOUT = os.environ.get("SISTER_LOGOUT_URL", "https://iampe.agenziaentrate.gov.it/sam/UI/Logout?realm=/agenziaentrate")
# sister.agenziaentrate.gov.it, sister3..., e in prova localhost
SISTER_HOST = re.compile(os.environ.get("SISTER_HOST_RE", r"^https://sister\d*\.agenziaentrate\.gov\.it/"))
CONVENZIONE = os.environ.get("SISTER_CONVENZIONE", "GROMIA").upper()

T_PAGE = 45_000      # una pagina di SISTER, anche quando è lenta
T_LOGIN = 60_000     # la pagina unica dell'Agenzia dopo "Accedi"
T_OPTIONAL = 4_000   # un elemento che a volte c'è e a volte no


# ---- errori ---------------------------------------------------------------------------------------

class SisterError(Exception):
    """Errore da mostrare all'agente così com'è (in italiano)."""


class LoginRifiutato(SisterError):
    """La pagina unica ha rifiutato le credenziali: riprovare blocca l'utenza, quindi non si riprova."""


class UtenzaOccupata(SisterError):
    """'Utente gia' in sessione': l'utenza è aperta altrove (a mano o un altro programma)."""


class SessionePersa(SisterError):
    """SISTER ha chiuso la sessione (scaduta, o aperta da un'altra parte): si rientra e si ripete."""


class SenzaIntestati(SisterError):
    """SISTER non dà gli intestati di un'unità (per esempio un bene comune non censibile): resta sull'elenco."""


class NonTrovato(SisterError):
    """Dati che SISTER non conosce (comune, via, particella): ripetere dà lo stesso risultato."""


# ---- utilità --------------------------------------------------------------------------------------

def chiave(testo: str) -> str:
    """Nome confrontabile: senza accenti, apostrofi e spazi ("CORNATE D'ADDA" = "cornate d adda")."""
    t = unicodedata.normalize("NFKD", str(testo or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", t.upper())


def scegli_opzione(opzioni: list[dict], cercato: str) -> Optional[dict]:
    """L'opzione di una select che corrisponde al nome cercato: uguale, poi che inizia con il nome, poi che
    lo contiene (la più corta, cioè la più vicina). opzioni: [{"value", "text"}]."""
    k = chiave(cercato)
    if not k:
        return None
    validi = [o for o in opzioni if o.get("value") not in (None, "") and chiave(o.get("text"))]
    for o in validi:
        if chiave(o["text"]) == k or chiave(o["value"]) == k:
            return o
    inizio = [o for o in validi if chiave(o["text"]).startswith(k)]
    if inizio:
        return min(inizio, key=lambda o: len(o["text"]))
    dentro = [o for o in validi if k in chiave(o["text"])]
    if dentro:
        return min(dentro, key=lambda o: len(o["text"]))
    return None


JS_TABELLA = """
(t) => {
  const rows = [...t.querySelectorAll('tr')];
  const head = rows.find(r => r.querySelector('th'));
  if (!head) return [];
  const cols = [...head.querySelectorAll('th')].map(th => th.innerText.replace(/\\s+/g, ' ').trim());
  const out = [];
  for (const r of rows) {
    if (r === head) continue;
    const tds = [...r.querySelectorAll('td')];
    if (!tds.length) continue;
    const radio = r.querySelector('input[type=radio], input[type=checkbox]');
    const o = {};
    cols.forEach((c, i) => { if (c && tds[i]) o[c] = tds[i].innerText.replace(/\\s+/g, ' ').trim(); });
    if (!Object.values(o).some(v => v)) continue;
    out.push({ dati: o, radio: radio ? { name: radio.name, value: radio.value } : null });
  }
  return out;
}
"""


@dataclass
class Riga:
    dati: dict
    radio: Optional[dict]


def pulisci(r: Riga) -> dict:
    return dict(r.dati)


# ---- sessione -------------------------------------------------------------------------------------

@dataclass
class Sessione:
    """Una sessione SISTER su un contesto pulito del browser. apri() entra, chiudi() esce sempre."""
    browser: Browser
    username: str
    password: str
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    aperta: bool = False
    accesso_tentato: bool = False   # "Accedi" premuto: una sessione può essere aperta anche se apri() non è finito
    host: str = ""
    ufficio: str = ""        # ufficio scelto nella sessione ("MILANO Territorio")
    fase: Callable[[str], None] = field(default=lambda s: None)

    # -- ingresso e uscita --

    async def apri(self):
        if not self.username or not self.password:
            raise LoginRifiutato("Credenziali SISTER mancanti: impostare SISTER_USERNAME e SISTER_PASSWORD su visura-api.")
        await self._nuovo_contesto()
        p = self.page
        self.fase("Accesso a SISTER")
        log.info("login")
        await p.goto(SSO_LOGIN, timeout=T_LOGIN)
        await p.locator('a[href="#tab-5"]').first.click(timeout=T_PAGE)          # scheda "Sister"
        await p.locator("#username-sister").fill(self.username)
        await p.locator("#password-fo-sist").fill(self.password)
        self.accesso_tentato = True
        await p.locator("#tab-5 button.btn-primary").first.click()               # "Accedi"
        await self._attendi_home()

    async def _nuovo_contesto(self):
        await self._chiudi_contesto()
        self.context = await self.browser.new_context(locale="it-IT", viewport={"width": 1280, "height": 900})
        self.context.set_default_timeout(T_PAGE)
        self.page = await self.context.new_page()
        self.ufficio = ""

    async def _attendi_home(self):
        """Dopo "Accedi": la home di SISTER su uno dei suoi host, oppure la pagina d'errore della pagina unica
        ("Credenziali errate", "Autenticazione fallita" con password scaduta o utenza bloccata)."""
        p = self.page
        scadenza = asyncio.get_running_loop().time() + T_LOGIN / 1000
        while True:
            errore = await self._errore_login()
            if errore:
                log.warning("login rifiutato: %s", errore[:160])
                if re.search(r"scadut", errore, re.I):
                    raise LoginRifiutato("La password SISTER è scaduta: va cambiata sul sito dell'Agenzia e aggiornata su visura-api.")
                if re.search(r"cambio password|bloccat", errore, re.I):
                    raise LoginRifiutato("L'utenza SISTER è bloccata per troppi tentativi: va sbloccata sul sito dell'Agenzia.")
                raise LoginRifiutato("SISTER ha rifiutato le credenziali (Credenziali errate): controllare SISTER_USERNAME e SISTER_PASSWORD.")
            if SISTER_HOST.match(p.url) and "/sam/UI/" not in p.url and "/api/login" not in p.url:
                break
            if asyncio.get_running_loop().time() > scadenza:
                raise SessionePersa("La pagina di accesso dell'Agenzia non risponde.")
            await asyncio.sleep(0.5)
        self.aperta = True
        self.host = "{0.scheme}://{0.netloc}/".format(urlparse(p.url))
        log.info("entrato su %s", urlparse(p.url).netloc)
        # messaggi di SISTER (benvenuto, errori) o il menu
        try:
            await p.locator(".errore_txt, #bloccoContenutiAll, #menu-left li a").first.wait_for(timeout=T_PAGE)
        except PWTimeout:
            raise SessionePersa("La home di SISTER non si è caricata dopo l'accesso.")
        messaggio = await self._messaggio()
        if re.search(r"utente gi.{0,3} in sessione", messaggio, re.I) or "error_locked" in p.url:
            self.aperta = self.accesso_tentato = False   # la sessione aperta è di qualcun altro: non va chiusa da qui
            raise UtenzaOccupata("L'utenza SISTER è già in uso (qualcuno è collegato a mano o c'è un'altra sessione aperta).")
        if re.search(r"account bloccato", messaggio, re.I):
            raise LoginRifiutato("SISTER dice che l'utenza è bloccata.")
        # informativa privacy: senza "Conferma" SISTER non dà i servizi
        conferma = p.locator("form[name=PrivacyFB] input[type=submit][value=Conferma]")
        if await conferma.count():
            async with p.expect_navigation(wait_until="domcontentloaded", timeout=T_PAGE):
                await conferma.first.click()
        await self.controlla()
        try:
            await self._link_menu(r"Consultazioni e Certificazioni").wait_for(timeout=T_PAGE)
        except PWTimeout:
            raise SessionePersa("Il menu di SISTER non è comparso dopo l'accesso.")

    async def _errore_login(self) -> str:
        p = self.page
        if "/sam/UI/Login" not in p.url and "/api/login" not in p.url:
            return ""
        try:
            titoli = await p.locator("main h2").all_inner_texts()
        except Exception:
            return ""
        if any(re.search(r"Credenziali errate|Autenticazione fallita", t, re.I) for t in titoli):
            return " ".join((await p.locator("main").inner_text()).split())
        return ""

    async def _messaggio(self) -> str:
        try:
            testi = await self.page.locator(".errore_txt, #bloccoContenutiAll").all_inner_texts()
        except Exception:
            return ""
        return " ".join(" ".join(testi).split())

    async def chiudi(self):
        """Esce da SISTER e dalla pagina unica, sempre, anche dopo un errore: una sessione lasciata aperta
        blocca ogni altro accesso con la stessa utenza."""
        try:
            if (self.aperta or self.accesso_tentato) and self.page:
                try:
                    await self.page.goto(urljoin(self.host or SISTER_HOME, "/Servizi/CloseSessionsSis"), timeout=20_000)
                except Exception as e:
                    log.warning("CloseSessionsSis non riuscito: %s", type(e).__name__)
                try:
                    await self.page.goto(SSO_LOGOUT, timeout=20_000)
                except Exception as e:
                    log.warning("logout della pagina unica non riuscito: %s", type(e).__name__)
                log.info("uscito da SISTER")
        finally:
            self.aperta = self.accesso_tentato = False
            await self._chiudi_contesto()

    async def _chiudi_contesto(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        self.context = None
        self.page = None

    # -- controlli --

    async def controlla(self):
        """Dopo ogni pagina: SISTER ha chiuso la sessione?"""
        url = self.page.url
        if "sessione_scaduta" in url or "error_locked" in url or "/sam/UI/Login" in url:
            self.aperta = "error_locked" not in url and self.aperta
            raise SessionePersa("SISTER ha chiuso la sessione.")
        if not SISTER_HOST.match(url):
            raise SessionePersa(f"Pagina inattesa fuori da SISTER ({urlparse(url).netloc}).")
        messaggio = await self._messaggio()
        if re.search(r"sessione .{0,20}scadut|utente gi.{0,3} in sessione", messaggio, re.I):
            raise SessionePersa("SISTER ha chiuso la sessione.")

    async def naviga(self, locator):
        """Clic che porta a una nuova pagina: aspetta la pagina nuova, non quella di prima."""
        async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=T_PAGE):
            await locator.click()
        await self.controlla()

    async def assesta(self):
        """Dopo una scelta che può ricaricare la pagina (tipo di catasto): lascia il tempo di farlo."""
        await self.page.wait_for_timeout(400)
        await self.page.wait_for_load_state("domcontentloaded")

    def _link_menu(self, testo: str):
        return self.page.locator("#menu-left li a").filter(has_text=re.compile(testo, re.I)).first

    async def _clicca_menu(self, testo: str, esatto: Optional[str] = None):
        """Voce del menu a sinistra: quella che si chiama esattamente `esatto`, se c'è, se no la prima che
        contiene `testo`."""
        p = self.page
        links = p.locator("#menu-left li a")
        link = self._link_menu(testo)
        try:
            await link.wait_for(timeout=15_000)
        except PWTimeout:
            await self.controlla()
            voci = [" ".join(t.split()) for t in await links.all_inner_texts()]
            raise SessionePersa(f"Voce \"{testo}\" assente dal menu di SISTER ({' | '.join(voci)[:200]}).")
        if esatto:
            for i in range(await links.count()):
                if re.fullmatch(esatto, (await links.nth(i).inner_text()).strip(), re.I):
                    link = links.nth(i)
                    break
        await self.naviga(link)

    async def consultazioni(self):
        """Consultazioni e Certificazioni. Dentro le visure il menu a sinistra non l'ha più (ha le ricerche):
        si cerca il collegamento nel resto della pagina e, se non c'è, si torna alla home dei servizi."""
        p = self.page
        if await self._link_menu(r"Consultazioni e Certificazioni").count():
            return await self._clicca_menu(r"Consultazioni e Certificazioni")
        altrove = p.locator("a").filter(has_text=re.compile(r"Consultazioni e Certificazioni", re.I))
        if await altrove.count():
            return await self.naviga(altrove.first)
        log.info("torno alla home dei servizi")
        await p.goto(urljoin(self.host or SISTER_HOME, "/Servizi/"), wait_until="domcontentloaded", timeout=T_PAGE)
        await self.controlla()
        conferma = p.locator("form[name=PrivacyFB] input[type=submit][value=Conferma]")
        if await conferma.count():
            await self.naviga(conferma.first)
        await self._clicca_menu(r"Consultazioni e Certificazioni")

    async def mantieni(self):
        """Un giro nel menu, perché SISTER non chiuda la sessione per inattività: dentro le visure apre la
        ricerca per immobile (l'ufficio resta quello), altrimenti Consultazioni e Certificazioni."""
        self.fase("Sessione SISTER attiva")
        if self.ufficio and await self._link_menu(r"^\s*Immobile\s*$").count():
            await self._clicca_menu(r"^\s*Immobile\s*$")
            return
        await self.consultazioni()
        self.ufficio = ""

    # -- ufficio provinciale --

    async def scegli_ufficio(self, provincia: str, comune: str = ""):
        """Visure catastali > Conferma Lettura > convenzione > ufficio ("MILANO Territorio"). Si ripassa
        sempre dal menu: aprire gli indirizzi di SISTER direttamente fa rispondere "sessione scaduta"."""
        p = self.page
        uffici = ufficio_per(provincia, comune)
        # stesso ufficio della ricerca di prima: le ricerche sono già nel menu
        if (self.ufficio and any(chiave(self.ufficio).startswith(chiave(u)) for u in uffici)
                and await self._link_menu(r"^\s*Immobile\s*$").count()):
            return
        self.fase(f"Scelta dell'ufficio di {uffici[0].title()}")
        self.ufficio = ""
        # dentro le visure c'è "Cambia Ufficio", che porta alla scelta dell'ufficio; altrimenti dal menu
        cambia = p.locator("a").filter(has_text=re.compile(r"^\s*Cambia Ufficio\s*$", re.I))
        if await cambia.count():
            await self.naviga(cambia.first)
        else:
            await self.consultazioni()
            await self._clicca_menu(r"Visure catastali")
        # l'informativa "Conferma Lettura" compare solo a volte; poi forse la convenzione; poi gli uffici
        pronto = p.locator("select[name=listacom], form[name=SelConv]")
        lettura = p.get_by_text("Conferma Lettura", exact=True)
        try:
            await pronto.or_(lettura).first.wait_for(timeout=T_PAGE)
        except PWTimeout:
            await self.controlla()
            raise SessionePersa("SISTER non ha mostrato la scelta dell'ufficio.")
        if await lettura.count() and not await pronto.count():
            await self.naviga(lettura.first)
        await self._scegli_convenzione()
        select = p.locator("select[name=listacom]")
        try:
            await select.wait_for(timeout=T_PAGE)
        except PWTimeout:
            await self.controlla()
            raise SessionePersa("SISTER non ha mostrato l'elenco degli uffici.")
        opzioni = await self._opzioni(select)
        if not opzioni:
            raise SessionePersa("SISTER ha mostrato un elenco di uffici vuoto.")
        scelta = None
        for nome in uffici:
            scelta = (next((o for o in opzioni if chiave(o["text"]) == chiave(f"{nome} Territorio")), None)
                      or next((o for o in opzioni if chiave(o["text"]).startswith(chiave(nome)) and "TERRITORIO" in chiave(o["text"])), None))
            if scelta:
                break
        if not scelta:
            raise NonTrovato(f"SISTER non ha l'ufficio di \"{provincia}\": scrivi la provincia per esteso (es. Milano) o la sigla (MI).")
        await select.select_option(scelta["value"])
        await self.naviga(p.locator("input[type=submit][value=Applica]").first)
        self.ufficio = scelta["text"].strip()
        log.info("ufficio %s", self.ufficio)

    async def _scegli_convenzione(self):
        """Un'utenza con più convenzioni sceglie quale usare (quella di GROMIA, o la prima)."""
        p = self.page
        form = p.locator("form[name=SelConv]")
        if not await form.count():
            return
        scelte = form.locator("input[type=radio], input[type=checkbox]")
        n = await scelte.count()
        if n:
            etichette = []
            for i in range(n):
                etichette.append(await scelte.nth(i).evaluate(
                    "e => (e.closest('tr, li, label, p, div') || e.parentElement).innerText"))
            i = next((i for i, t in enumerate(etichette) if CONVENZIONE in str(t).upper()), 0)
            await scelte.nth(i).check()
        else:
            sel = form.locator("select").first
            if await sel.count():
                opzioni = await self._opzioni(sel)
                o = next((o for o in opzioni if CONVENZIONE in o["text"].upper()), opzioni[0] if opzioni else None)
                if o:
                    await sel.select_option(o["value"])
        log.info("convenzione scelta")
        await self.naviga(form.locator("input[type=submit]").first)

    async def _opzioni(self, select) -> list[dict]:
        return await select.evaluate("s => [...s.options].map(o => ({value: o.value, text: o.text.replace(/\\s+/g, ' ').trim()}))")

    async def _seleziona(self, selettore: str, cercato: str, cosa: str, scope=None) -> str:
        sel = (scope or self.page).locator(selettore).first
        opzioni = await self._opzioni(sel)
        o = scegli_opzione(opzioni, cercato)
        if not o:
            raise NonTrovato(f"{cosa} \"{cercato}\" non trovato in SISTER ({self.ufficio or 'ufficio scelto'}).")
        await sel.select_option(o["value"])
        return o["text"]

    # -- tabelle --

    async def tabella(self) -> list[Riga]:
        t = self.page.locator("table.listaIsp4").first
        if not await t.count():
            return []
        return [Riga(r["dati"], r["radio"]) for r in await t.evaluate(JS_TABELLA)]

    async def nessun_risultato(self) -> bool:
        testo = (await self.page.content()).lower()
        return "nessuna corrispondenza trovata" in testo or "nessun immobile" in testo or "nessun soggetto" in testo

    async def _invia(self, selettore: str):
        await self.naviga(self.page.locator(selettore).first)

    async def intestati_di(self, riga: Riga, rifai_elenco: Callable[[], Awaitable[None]]) -> list[dict]:
        """Titolari di una riga dell'elenco: radio della riga, "intestati", tabella, "indietro". Se SISTER
        perde l'elenco si rifà la ricerca (rifai_elenco) una volta."""
        p = self.page
        if not riga.radio:
            return []
        radio = p.locator(f"input[name='{riga.radio['name']}'][value='{riga.radio['value']}']")
        if not await radio.count():
            await rifai_elenco()
            if not await radio.count():
                raise SessionePersa("SISTER ha perso l'elenco degli immobili.")
        await radio.first.check()
        await self._invia("input[type=submit][name=intestati]")
        if os.environ.get("VISURA_ESPLORA"):
            st = await p.evaluate(JS_STRUTTURA)
            st.pop("superficie", None) if not st.get("superficie") else None
            log.info("ESPLORA intestati: tabelle=%s pulsanti=%s superficie=%s", st["tabelle"], st["pulsanti"], st.get("superficie"))
        if await p.locator("input[type=submit][name=intestati]").count():
            # ancora l'elenco: SISTER non ha gli intestati di questa unità e lo dice nel suo messaggio
            messaggio = await self._messaggio()
            log.info("unità senza intestati: %s", messaggio[:160])
            raise SenzaIntestati(messaggio or "SISTER non dà gli intestati di questa unità.")
        righe = [pulisci(r) for r in await self.tabella()]
        indietro = p.locator("input[type=submit][name=indietro]")
        if await indietro.count():
            await self._invia("input[type=submit][name=indietro]")
        else:
            await p.go_back(wait_until="domcontentloaded")
            if not await p.locator("input[type=submit][name=intestati]").count():
                await rifai_elenco()
        return righe

    async def pulsanti(self) -> list[str]:
        """Nomi e valori dei pulsanti della pagina: per i log quando una pagina non è quella attesa."""
        return await self.page.evaluate(
            "() => [...document.querySelectorAll('input[type=submit], input[type=button], button')]"
            ".map(b => (b.name || '') + '=' + (b.value || b.innerText || '').trim()).slice(0, 30)")


# ---- ricerche -------------------------------------------------------------------------------------

def avviso_limite(n: int, limite: int) -> list[str]:
    return [f"Intestati letti per le prime {limite} unità su {n}: per le altre cerca il singolo subalterno."] if n > limite else []


async def cerca_particella(s: Sessione, provincia: str, comune: str, foglio: str, particella: str,
                           sezione: str = "", subalterno: str = "", catasto: str = "F",
                           max_intestati: int = 40) -> dict:
    """Consultazioni > Immobile, catasto fabbricati (F) o terreni (T): le unità della particella e, per
    ognuna (al massimo max_intestati), gli intestati. Con il subalterno solo quella unità."""
    p = s.page
    await s.scegli_ufficio(provincia, comune)

    async def ricerca():
        s.fase(f"Ricerca foglio {foglio} particella {particella} ({'terreni' if catasto == 'T' else 'fabbricati'})")
        await s._clicca_menu(r"immobil", esatto=r"Immobile")
        tipo = p.locator("select[name=tipoCatasto]")
        if await tipo.count():
            await tipo.first.select_option(catasto)
            await s.assesta()
        await s._seleziona("select[name=denomComune]", comune, "Comune")
        if sezione:
            await _sezione(s, sezione)
        await p.locator("input[name=foglio]").first.fill(str(foglio))
        await p.locator("input[name=particella1]").first.fill(str(particella))
        if subalterno and catasto == "F":
            sub = p.locator("input[name=subalterno1]")
            if await sub.count():
                await sub.first.fill(str(subalterno))
        await s._invia("input[name=scelta][value=Ricerca]")
        # una particella senza subalterni chiede di confermare la ricerca sulla particella intera
        conferma = p.locator("input[name=confAssSub][value=Conferma]")
        if await conferma.count():
            await s._invia("input[name=confAssSub][value=Conferma]")

    await ricerca()
    righe = await s.tabella()
    if not righe:
        if await s.nessun_risultato():
            return {"immobili": [], "avvisi": [f"SISTER non ha immobili per foglio {foglio} particella {particella} a {comune}."]}
        log.warning("pagina senza elenco dopo la ricerca: %s", await s.pulsanti())
        await s.controlla()
        return {"immobili": [], "avvisi": ["SISTER non ha mostrato l'elenco degli immobili."]}
    if subalterno:
        scelte = [r for r in righe if _sub(r.dati) == str(subalterno).lstrip("0")]
        if not scelte:
            return {"immobili": [], "avvisi": [f"Il subalterno {subalterno} non c'è tra le {len(righe)} unità della particella."]}
        righe = scelte
    if os.environ.get("VISURA_ESPLORA"):
        await _esplora(s, righe[0], ricerca)
    return {"immobili": await _con_intestati(s, righe, catasto, ricerca, max_intestati),
            "avvisi": avviso_limite(len(righe), max_intestati)}


JS_STRUTTURA = """
() => ({
  titolo: document.title,
  tabelle: [...document.querySelectorAll('table')].map(t => [...t.querySelectorAll('th')].map(h => h.innerText.trim()).join('|')).filter(x => x),
  pulsanti: [...document.querySelectorAll('input[type=submit], input[type=button], button')].map(b => (b.name || '') + '=' + (b.value || b.innerText || '').trim()),
  superficie: (document.body.innerText.match(/.{0,60}uperfic.{0,90}/gi) || []).slice(0, 8),
})
"""


JS_MODULO = """
() => [...document.querySelectorAll('input, select')].filter(e => e.type !== 'hidden').map(e => {
  const lab = (e.closest('tr, li, label, p, div') || e.parentElement).innerText.replace(/\\s+/g, ' ').trim().slice(0, 80);
  if (e.tagName === 'SELECT') return e.name + ' select [' + [...e.options].map(o => o.value + ':' + o.text.trim()).join(', ').slice(0, 300) + ']';
  return e.name + ' ' + e.type + '=' + e.value + (e.checked ? ' (scelto)' : '') + ' «' + lab + '»';
}).slice(0, 40)
"""


async def _esplora(s: Sessione, riga: Riga, rifai):
    """Solo con VISURA_ESPLORA: struttura delle pagine dell'unità (tabelle, pulsanti, righe con "superficie")
    per capire dove SISTER mette un dato. Nei log niente nomi: solo intestazioni e dati dell'immobile."""
    p = s.page
    try:
        log.info("ESPLORA elenco: %s consistenza=%r altri=%r", await p.evaluate(JS_STRUTTURA),
                 riga.dati.get("Consistenza"), riga.dati.get("Altri Dati"))
        for nome in ("visura", "dettaglio", "dati", "visuraImm"):
            b = p.locator(f"input[type=submit][name={nome}]")
            if await b.count() and riga.radio:
                await p.locator(f"input[name='{riga.radio['name']}'][value='{riga.radio['value']}']").first.check()
                await s.naviga(b.first)
                log.info("ESPLORA dopo %s: %s", nome, await p.evaluate(JS_STRUTTURA))
                log.info("ESPLORA modulo: %s", await p.evaluate(JS_MODULO))
                inoltra = p.locator("input[type=submit][name=inoltra]")
                if await inoltra.count():
                    try:
                        async with p.expect_download(timeout=20_000) as dl:
                            await inoltra.first.click()
                        d = await dl.value
                        log.info("ESPLORA inoltra: scaricato %s", d.suggested_filename)
                        dati = open(await d.path(), "rb").read()
                        log.info("ESPLORA file: %d byte, inizio %r", len(dati), dati[:8])
                    except PWTimeout:
                        await p.wait_for_load_state("domcontentloaded")
                        log.info("ESPLORA dopo inoltra: %s url=%s", await p.evaluate(JS_STRUTTURA), urlparse(p.url).path)
                        log.info("ESPLORA modulo dopo inoltra: %s", await p.evaluate(JS_MODULO))
                await rifai()
                break
    except Exception as e:
        log.warning("ESPLORA non riuscita: %s %s", type(e).__name__, str(e)[:200])
        await rifai()


def _sub(dati: dict) -> str:
    for k in ("Sub", "Subalterno", "Sub."):
        if k in dati:
            return str(dati[k]).strip().lstrip("0")
    return ""


async def _sezione(s: Sessione, sezione: str):
    p = s.page
    campi = p.locator("input[type=text], select")
    n = await campi.count()
    trovato = None
    for i in range(n):
        nome = (await campi.nth(i).get_attribute("name")) or ""
        if re.search(r"sez.*urb|urb.*sez", nome, re.I):
            trovato = campi.nth(i)
            break
    if trovato is None:
        for i in range(n):
            nome = (await campi.nth(i).get_attribute("name")) or ""
            if re.search(r"sez", nome, re.I) and nome != "denomComune":
                trovato = campi.nth(i)
                break
    if trovato is None:
        raise NonTrovato("Il modulo di SISTER per questo comune non ha la sezione: prova senza.")
    if (await trovato.evaluate("e => e.tagName")).lower() == "select":
        if len(await s._opzioni(trovato)) <= 1:
            pulsante = p.locator("input[name=selSezione]")
            if await pulsante.count():
                nome = await trovato.get_attribute("name")
                await pulsante.first.click()
                # SISTER riempie l'elenco delle sezioni dopo "scegli la sezione": si aspetta che arrivi
                for _ in range(20):
                    await p.wait_for_timeout(500)
                    await p.wait_for_load_state("domcontentloaded")
                    trovato = p.locator(f"select[name='{nome}']").first
                    if len(await s._opzioni(trovato)) > 1:
                        break
        o = scegli_opzione(await s._opzioni(trovato), sezione)
        if not o:
            raise NonTrovato(f"Sezione \"{sezione}\" non trovata per questo comune.")
        await trovato.select_option(o["value"])
    else:
        await trovato.fill(sezione)


async def _con_intestati(s: Sessione, righe: list[Riga], catasto: str, rifai, max_intestati: int) -> list[dict]:
    out = []
    for i, r in enumerate(righe):
        voce = {"catasto": catasto, "dati": pulisci(r), "intestati": None, "errore_intestati": None}
        if i < max_intestati and r.radio:
            s.fase(f"Lettura degli intestati: unità {i + 1} di {min(len(righe), max_intestati)}")
            try:
                voce["intestati"] = await s.intestati_di(r, rifai)
            except SenzaIntestati as e:
                voce["errore_intestati"] = str(e)
            except SessionePersa:
                raise
            except Exception as e:
                log.warning("intestati non letti: %s", type(e).__name__)
                voce["errore_intestati"] = "Intestati non letti per questa unità."
        out.append(voce)
    return out


TOPONIMI = ("VIA", "VIALE", "PIAZZA", "PIAZZALE", "CORSO", "LARGO", "VICOLO", "STRADA", "LUNGOTEVERE", "LUNGOMARE",
            "VIA PRIVATA", "PIAZZETTA", "CONTRADA", "LOCALITA", "FRAZIONE", "BORGO", "SALITA", "PASSAGGIO", "RIPA",
            "GALLERIA", "ALZAIA", "BASTIONI", "CIRCONVALLAZIONE", "TRAVERSA", "RAMPA", "CALATA", "VICO", "SCALINATA")


def separa_indirizzo(toponimo: str, indirizzo: str) -> tuple[str, str, str]:
    """("", "Via Roma 10") -> ("VIA", "ROMA", "10"). Il civico è il numero in fondo, se c'è."""
    testo = " ".join(str(indirizzo or "").replace(",", " ").split()).upper()
    civico = ""
    m = re.search(r"\s(\d+[A-Z]?(?:/\w+)?)$", testo)
    if m:
        civico, testo = m.group(1), testo[: m.start()].strip()
    top = " ".join(str(toponimo or "").split()).upper()
    if not top:
        for t in sorted(TOPONIMI, key=len, reverse=True):
            if testo.startswith(t + " "):
                top, testo = t, testo[len(t) + 1:].strip()
                break
    return top or "VIA", testo, civico


async def cerca_indirizzo(s: Sessione, provincia: str, comune: str, indirizzo: str, toponimo: str = "",
                          civico: str = "", catasto: str = "F", max_intestati: int = 40) -> dict:
    """Consultazioni > Indirizzo: la via (tra quelle che SISTER propone, la più vicina), i civici e le unità,
    con gli intestati di ognuna (al massimo max_intestati)."""
    p = s.page
    top, via, civ = separa_indirizzo(toponimo, indirizzo)
    civico = str(civico or civ or "").strip()
    if not via:
        raise NonTrovato("Scrivi il nome della via.")
    await s.scegli_ufficio(provincia, comune)
    scelta_via = {}

    async def ricerca():
        s.fase(f"Ricerca di {top} {via}{' ' + civico if civico else ''}")
        await s._clicca_menu(r"indirizzo")
        tipo = p.locator("form[name=ricercaIndForm] select[name=tipoCatasto], select[name=tipoCatasto]")
        if await tipo.count():
            await tipo.first.select_option(catasto)
            await s.assesta()
        form = p.locator("form[name=ricercaIndForm]")
        scope = form if await form.count() else p
        await s._seleziona("select[name=comuneCat]", comune, "Comune", scope)
        toponimi = await s._opzioni(scope.locator("select[name=toponimo]").first)
        o = next((o for o in toponimi if o["text"].strip().upper() == top), None) or scegli_opzione(toponimi, top)
        if not o:
            raise NonTrovato(f"SISTER non ha il tipo di strada \"{top}\".")
        await scope.locator("select[name=toponimo]").first.select_option(o["value"])
        await scope.locator("input[name=indirizzo]").first.fill(via)
        intera = scope.locator("input[name=parIntera][value='1']")
        if await intera.count():
            await intera.first.check()
        await s._invia("input[type=submit][name=ricerca]")
        if await s.nessun_risultato():
            raise NonTrovato(f"SISTER non conosce {top} {via} a {comune}.")
        sel = p.locator("form[name=SceltaIndirizzoForm] select[name=indirizzoSel]")
        if not await sel.count():
            log.warning("pagina senza elenco delle vie: %s", await s.pulsanti())
            raise SessionePersa("SISTER non ha mostrato l'elenco delle vie.")
        vie = await s._opzioni(sel)
        if not vie:
            raise NonTrovato(f"SISTER non conosce {top} {via} a {comune}.")
        v = (next((x for x in vie if chiave(x["text"]) == chiave(f"{top} {via}")), None)
             or (vie[0] if len(vie) == 1 else None) or scegli_opzione(vie, f"{top} {via}") or scegli_opzione(vie, via))
        if not v:
            raise NonTrovato(f"SISTER ha più vie simili: {', '.join(x['text'] for x in vie[:8])}. Scrivi il nome esatto.")
        await sel.select_option(v["value"])
        scelta_via["via"] = v["text"]
        if civico:
            dal = p.locator("form[name=SceltaIndirizzoForm] input[name=numCivicoDal]")
            al = p.locator("form[name=SceltaIndirizzoForm] input[name=numCivicoAl]")
            numero = re.match(r"\d+", civico)
            if await dal.count() and numero:
                await dal.first.fill(numero.group(0))
                await al.first.fill(numero.group(0))
        await s._invia("form[name=SceltaIndirizzoForm] input[type=submit][name=ricerca]")

    await ricerca()
    if await s.nessun_risultato():
        return {"immobili": [], "via": scelta_via.get("via"),
                "avvisi": [f"SISTER non ha unità in {scelta_via.get('via', via)}{' ' + civico if civico else ''}."]}
    righe = await s.tabella()
    if not righe:
        log.warning("pagina senza elenco delle unità: %s", await s.pulsanti())
        await s.controlla()
        return {"immobili": [], "via": scelta_via.get("via"), "avvisi": ["SISTER non ha mostrato l'elenco delle unità."]}
    avvisi = avviso_limite(len(righe), max_intestati)
    if not civico and len(righe) > max_intestati:
        avvisi = [f"La via ha {len(righe)} unità: intestati letti per le prime {max_intestati}. Aggiungi il civico per vederli tutti."]
    return {"via": scelta_via.get("via"), "immobili": await _con_intestati(s, righe, catasto, ricerca, max_intestati),
            "avvisi": avvisi}


async def cerca_soggetto(s: Sessione, provincia: str, comune: str = "", tipo_soggetto: str = "PF",
                         cognome: str = "", nome: str = "", codice_fiscale: str = "", denominazione: str = "",
                         catasto: str = "E", richiedente: str = "", motivo: str = "", max_soggetti: int = 5) -> dict:
    """Consultazioni > Persona fisica / Persona giuridica: i soggetti che SISTER trova (omonimi compresi) e,
    per ognuno (al massimo max_soggetti), gli immobili di cui è titolare nella provincia."""
    p = s.page
    await s.scegli_ufficio(provincia, comune)
    voce = "Persona fisica" if tipo_soggetto == "PF" else "Persona giuridica"
    chi = codice_fiscale or denominazione or " ".join(x for x in (cognome, nome) if x)

    async def ricerca():
        s.fase(f"Ricerca di {chi}")
        await s._clicca_menu(voce.split()[-1], esatto=voce)
        tipo = p.locator("select[name=tipoCatasto]")
        if await tipo.count():
            await tipo.first.select_option(catasto)
        if comune:
            await s._seleziona("select[name=comuneCat]", comune, "Comune")
        else:
            com = p.locator("select[name=comuneCat]")
            if await com.count():
                tutta = next((o for o in await s._opzioni(com.first) if o["value"] == "$" or "TUTTA" in o["text"].upper()), None)
                if tutta:
                    await com.first.select_option(tutta["value"])
        if tipo_soggetto == "PF":
            if codice_fiscale:
                await _spunta(p, "input[name=selDatiAna][value=CF_PF]")
                await p.locator("input[name=cod_fisc_pf]").first.fill(codice_fiscale)
            else:
                await _spunta(p, "input[name=selDatiAna][value=cognome]")
                await p.locator("input[name=cognome]").first.fill(cognome)
                if nome:
                    await p.locator("input[name=nome]").first.fill(nome)
        else:
            if codice_fiscale:
                await _spunta(p, "input[name=selCfDn][value=CF_PNF]")
                await p.locator("input[name=cod_fisc]").first.fill(codice_fiscale)
            else:
                await _spunta(p, "input[name=selCfDn][value=denominazione]")
                await p.locator("input[name=denominazione]").first.fill(denominazione)
        for nome_campo, valore in (("richiedente", richiedente), ("motivoText", motivo)):
            campo = p.locator(f"input[name={nome_campo}], textarea[name={nome_campo}]")
            if valore and await campo.count():
                await campo.first.fill(valore[:100])
        await s._invia("input[name=ricerca][value=Ricerca], input[type=submit][name=ricerca]")

    await ricerca()
    if await s.nessun_risultato():
        return {"soggetti": [], "immobili": [], "avvisi": [f"SISTER non ha soggetti per {chi} nella provincia."]}
    soggetti = await s.tabella()
    if not soggetti:
        log.warning("pagina senza elenco dei soggetti: %s", await s.pulsanti())
        await s.controlla()
        return {"soggetti": [], "immobili": [], "avvisi": ["SISTER non ha mostrato l'elenco dei soggetti."]}
    log.info("soggetti trovati: %d, colonne %s, pulsanti %s", len(soggetti), list(soggetti[0].dati), await s.pulsanti())
    immobili, avvisi = [], []
    if len(soggetti) > max_soggetti:
        avvisi.append(f"SISTER ha trovato {len(soggetti)} soggetti: immobili letti per i primi {max_soggetti}. Usa il codice fiscale per una ricerca precisa.")
    for i, sog in enumerate(soggetti[:max_soggetti]):
        if not sog.radio:
            continue
        s.fase(f"Immobili del soggetto {i + 1} di {min(len(soggetti), max_soggetti)}")
        radio = p.locator(f"input[name='{sog.radio['name']}'][value='{sog.radio['value']}']")
        if not await radio.count():
            await ricerca()
        await radio.first.check()
        pulsante = await _pulsante_immobili(p)
        if not pulsante:
            log.warning("nessun pulsante per gli immobili del soggetto: %s", await s.pulsanti())
            avvisi.append("SISTER non ha offerto l'elenco degli immobili del soggetto.")
            break
        await s._invia(pulsante)
        righe = await s.tabella()
        log.info("immobili del soggetto: %d, colonne %s", len(righe), list(righe[0].dati) if righe else [])
        for r in righe:
            immobili.append({"soggetto": pulisci(sog), "catasto": _catasto_riga(r.dati, catasto), "dati": pulisci(r),
                             "intestati": None, "errore_intestati": None})
        indietro = p.locator("input[type=submit][name=indietro]")
        if await indietro.count():
            await s._invia("input[type=submit][name=indietro]")
        else:
            await ricerca()
    return {"soggetti": [pulisci(x) for x in soggetti], "immobili": immobili, "avvisi": avvisi}


async def _spunta(p: Page, selettore: str):
    el = p.locator(selettore)
    if await el.count():
        await el.first.check()


async def _pulsante_immobili(p: Page) -> Optional[str]:
    """Il pulsante che dall'elenco dei soggetti apre i loro immobili (il nome cambia tra le pagine)."""
    for sel in ("input[type=submit][name=immobili]", "input[type=submit][name=visura]",
                "input[type=submit][name=elencoImmobili]", "input[type=submit][name=intestati]"):
        if await p.locator(sel).count():
            return sel
    pulsanti = p.locator("input[type=submit]")
    n = await pulsanti.count()
    for i in range(n):
        b = pulsanti.nth(i)
        testo = " ".join([(await b.get_attribute("name")) or "", (await b.get_attribute("value")) or ""])
        if re.search(r"immobil|elenco|visura", testo, re.I) and not re.search(r"indietro|nuova|annulla|stampa", testo, re.I):
            nome, valore = await b.get_attribute("name"), await b.get_attribute("value")
            return f"input[type=submit][name='{nome}']" if nome else f"input[type=submit][value='{valore}']"
    return None


def _catasto_riga(dati: dict, predefinito: str) -> str:
    for k, v in dati.items():
        if re.fullmatch(r"catasto|c\.?", k.strip(), re.I) and str(v).strip().upper() in ("F", "T"):
            return str(v).strip().upper()
    return predefinito if predefinito in ("F", "T") else ""
