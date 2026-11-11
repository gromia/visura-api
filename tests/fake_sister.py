"""Un SISTER finto per le prove: le stesse pagine, gli stessi moduli e gli stessi selettori di quello vero
(quelli che usa sister.py), con una sessione sola per utenza, la sessione che scade, l'informativa che
compare a volte, la convenzione da scegliere e gli elenchi table.listaIsp4 con i radio.

STATO permette ai test di cambiare il comportamento: scadenza della sessione, utenza occupata altrove,
password sbagliata, conteggio di login e logout.
"""
from __future__ import annotations

import asyncio
import html
import random
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()


@app.middleware("http")
async def lento(request, call_next):
    """Con STATO["lento"] ogni pagina arriva dopo 0,2-0,9 secondi, come SISTER nelle ore di punta."""
    if STATO.get("lento"):
        await asyncio.sleep(random.uniform(0.2, 0.9))
    return await call_next(request)

UTENTE, PASSWORD = "UTENTE01", "segreta"

STATO: dict = {}


def azzera():
    STATO.clear()
    STATO.update(sessioni={}, attiva=None, occupata=False, logins=0, logouts=0, scadi_dopo=None, pagine=0,
                 lettura_ogni_volta=True, convenzioni=True, password=PASSWORD, lento=False)


azzera()

IMMOBILI = {
    ("MILANO", "520", "515"): [
        {"Foglio": "520", "Particella": "515", "Sub": "1", "Categoria": "A/2", "Classe": "3", "Consistenza": "5 vani",
         "Rendita": "Euro:800,00", "Indirizzo": "VIA ROMA n. 10 piano: 1"},
        {"Foglio": "520", "Particella": "515", "Sub": "26", "Categoria": "A/3", "Classe": "2", "Consistenza": "4 vani",
         "Rendita": "Euro:600,00", "Indirizzo": "VIA ROMA n. 10 piano: 3"},
        {"Foglio": "520", "Particella": "515", "Sub": "30", "Categoria": "C/6", "Classe": "5", "Consistenza": "12 m2",
         "Rendita": "Euro:90,00", "Indirizzo": "VIA ROMA n. 10 piano: S1"},
        {"Foglio": "520", "Particella": "515", "Sub": "41", "Categoria": "F/1", "Classe": "", "Consistenza": "",
         "Rendita": "", "Indirizzo": "VIA ROMA n. 10 piano: T"},
    ],
}
TERRENI = {("MILANO", "12", "34"): [{"Foglio": "12", "Particella": "34", "Qualita": "SEMINATIVO", "Superficie": "1200"}]}
INTESTATI = {
    "1": [{"Nominativo o denominazione": "ROSSI MARIO", "Codice fiscale": "RSSMRA70A01F205X", "Titolarità": "Proprieta' 1/1"}],
    "26": [{"Nominativo o denominazione": "MONTE ALBERTO", "Codice fiscale": "MNTLRT60B02F205Y", "Titolarità": "Proprieta' 1/2"},
           {"Nominativo o denominazione": "BIANCHI ANNA", "Codice fiscale": "BNCNNA62C43F205Z", "Titolarità": "Proprieta' 1/2"}],
    "30": [{"Nominativo o denominazione": "ROSSI MARIO", "Codice fiscale": "RSSMRA70A01F205X", "Titolarità": "Proprieta' 1/1"}],
    "T34": [{"Nominativo o denominazione": "VERDI LUCA", "Codice fiscale": "VRDLCU80D04F205W", "Titolarità": "Proprieta' 1/1"}],
}
VIE = {("MILANO", "VIA", "ROMA"): ["VIA ROMA", "VIA ROMAGNA"]}
SOGGETTI = {
    "MONTE": [{"Cognome": "MONTE", "Nome": "ALBERTO", "Data di nascita": "02/02/1960", "Luogo di nascita": "MILANO",
               "Codice fiscale": "MNTLRT60B02F205Y"},
              {"Cognome": "MONTE", "Nome": "ALBERTO", "Data di nascita": "10/10/1975", "Luogo di nascita": "ROMA",
               "Codice fiscale": "MNTLRT75R10H501K"}],
}
UFFICI = ["AGRIGENTO Territorio", "MILANO Territorio", "MONZA E DELLA BRIANZA Territorio", "ROMA Territorio"]
COMUNI = {"MILANO Territorio": ["MILANO", "ABBIATEGRASSO", "SESTO SAN GIOVANNI"], "ROMA Territorio": ["ROMA", "FIUMICINO"],
          "MONZA E DELLA BRIANZA Territorio": ["MONZA", "CORNATE D'ADDA"], "AGRIGENTO Territorio": ["AGRIGENTO"]}


def pagina(corpo: str, menu: list[tuple[str, str]] | None = None, messaggio: str = "") -> HTMLResponse:
    STATO["pagine"] += 1
    voci = "".join(f'<li><a href="{h}">{html.escape(t)}</a></li>' for t, h in (menu or []))
    msg = f'<div class="errore_txt">{html.escape(messaggio)}</div>' if messaggio else ""
    return HTMLResponse(f"<html><body><div id='menu-left'><ul>{voci}</ul></div>{msg}<div id='contenuto'>{corpo}</div></body></html>")


def tabella(righe: list[dict], radio: str, valori: list[str]) -> str:
    if not righe:
        return ""
    cols = list(righe[0])
    th = "".join(f"<th>{html.escape(c)}</th>" for c in [""] + cols)
    tr = "".join("<tr><td><input type='radio' name='%s' value='%s'></td>%s</tr>" % (
        radio, v, "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols)) for r, v in zip(righe, valori))
    return f"<table class='listaIsp4'><tr>{th}</tr>{tr}</table>"


def sessione(req: Request):
    sid = req.cookies.get("sid")
    s = STATO["sessioni"].get(sid)
    if not s or STATO["attiva"] != sid:
        return None
    if STATO["scadi_dopo"] is not None:
        STATO["scadi_dopo"] -= 1
        if STATO["scadi_dopo"] < 0:
            STATO["scadi_dopo"] = None
            STATO["attiva"] = None
            return None
    return s


MENU_BASE = [("Consultazioni e Certificazioni", "/Servizi/consultazioni"), ("Esci", "/Servizi/CloseSessionsSis")]


def menu_visure(s):
    """Come su SISTER: scelto l'ufficio, il menu ha solo le ricerche (non più Consultazioni e Certificazioni)."""
    if s.get("ufficio"):
        return [("Cambia Ufficio", "/Visure/visure"), ("Persona fisica", "/Visure/pf"), ("Persona giuridica", "/Visure/pnf"), ("Immobile", "/Visure/immobile"),
                ("Indirizzo", "/Visure/indirizzo"), ("Elenco immobili", "/Visure/elenco_imm")]
    return MENU_BASE + [("Visure catastali", "/Visure/visure")]


def scaduta():
    return RedirectResponse("/Servizi/sessione_scaduta.jsp", status_code=302)


# ---- pagina unica dell'Agenzia ----

@app.get("/sam/UI/Login")
async def login_page():
    return HTMLResponse("""<html><body><main>
      <a href="#tab-1">SPID</a> <a href="#tab-5">Sister</a>
      <div id="tab-5"><form method="post" action="/api/login/sister">
        <input id="username-sister" name="u"><input id="password-fo-sist" name="p" type="password">
        <button class="btn-primary" type="submit">Accedi</button></form></div></main></body></html>""")


@app.post("/api/login/sister")
async def login(req: Request):
    f = await req.form()
    if f.get("u") != UTENTE or f.get("p") != STATO["password"]:
        return HTMLResponse("<html><body><main><h2>Credenziali errate</h2><p>Riprova.</p></main></body></html>")
    STATO["logins"] += 1
    sid = uuid.uuid4().hex
    r = RedirectResponse("/Servizi/", status_code=302)
    r.set_cookie("sid", sid)
    if STATO["occupata"] or (STATO["attiva"] and STATO["attiva"] in STATO["sessioni"]):
        r.headers["location"] = "/Servizi/error_locked.jsp"
        return r
    STATO["sessioni"][sid] = {"privacy": False}
    STATO["attiva"] = sid
    return r


@app.get("/sam/UI/Logout")
async def sso_logout():
    return HTMLResponse("<html><body>Disconnesso</body></html>")


# ---- SISTER ----

@app.get("/Servizi/error_locked.jsp")
async def locked():
    return pagina("", [], "Utente gia' in sessione sulla stessa o altra postazione.")


@app.get("/Servizi/sessione_scaduta.jsp")
async def scaduta_page():
    return pagina("<p>La sessione è scaduta</p>")


@app.get("/Servizi/CloseSessionsSis")
async def close(req: Request):
    sid = req.cookies.get("sid")
    if sid and STATO["attiva"] == sid:
        STATO["attiva"] = None
        STATO["logouts"] += 1
    STATO["sessioni"].pop(sid, None)
    return HTMLResponse("<html><body>Sessione chiusa <a href='/sam/UI/Login'>Torna al portale</a></body></html>")


@app.get("/Servizi/")
async def home(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    s["ufficio"] = None
    if not s["privacy"]:
        return pagina("<form name='PrivacyFB' method='post' action='/Servizi/privacy'><input type='submit' value='Conferma'></form>",
                      [("Consultazioni e Certificazioni", "/Servizi/consultazioni")], "Benvenuto, il tuo ultimo accesso...")
    return pagina("<p>Home</p>", MENU_BASE, "Benvenuto")


@app.post("/Servizi/privacy")
async def privacy(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    s["privacy"] = True
    return RedirectResponse("/Servizi/", status_code=302)


@app.get("/Servizi/consultazioni")
async def consultazioni(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    s["ufficio"] = None
    return pagina("<p>Consultazioni</p>", MENU_BASE + [("Visure catastali", "/Visure/visure")])


@app.get("/Visure/visure")
async def visure(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    s["ufficio"] = None
    if STATO["lettura_ogni_volta"] or not s.get("letta"):
        return pagina("<a href='/Visure/lettura'>Conferma Lettura</a>", menu_visure(s))
    return await convenzione_o_uffici(s)


@app.get("/Visure/lettura")
async def lettura(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    s["letta"] = True
    return await convenzione_o_uffici(s)


async def convenzione_o_uffici(s):
    if STATO["convenzioni"] and not s.get("conv"):
        return pagina("""<form name='SelConv' method='post' action='/Visure/conv'>
          <div><input type='radio' name='conv' value='P'> PERSONALE</div>
          <div><input type='radio' name='conv' value='G'> GROMIA SRL (CONSULTAZIONI - PROFILO B)</div>
          <input type='submit' value='Conferma'></form>""", menu_visure(s))
    return uffici_page(s)


def uffici_page(s):
    opts = "".join(f"<option value='{i}'>{u}</option>" for i, u in enumerate(UFFICI))
    return pagina(f"<form method='post' action='/Visure/ufficio'><select name='listacom'>{opts}</select>"
                  "<input type='submit' value='Applica'></form>", menu_visure(s))


@app.post("/Visure/conv")
async def conv(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    if f.get("conv") != "G":
        return pagina("<p>Convenzione sbagliata</p>", menu_visure(s))
    s["conv"] = "G"
    return uffici_page(s)


@app.post("/Visure/ufficio")
async def ufficio(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    s["ufficio"] = UFFICI[int(f["listacom"])]
    return pagina(f"<p>Ufficio {s['ufficio']}</p>", menu_visure(s))


def comuni_opts(s, name):
    return f"<select name='{name}'><option value=''>scegli</option>" + "".join(
        f"<option value='{c}#'>{c}</option>" for c in COMUNI[s["ufficio"]]) + "</select>"


@app.get("/Visure/immobile")
async def immobile(req: Request):
    s = sessione(req)
    if not s or not s.get("ufficio"):
        return scaduta()
    return pagina(f"""<form method='post' action='/Visure/immobile'>
      <select name='tipoCatasto'><option value='F'>Fabbricati</option><option value='T'>Terreni</option></select>
      {comuni_opts(s, 'denomComune')} <input name='sezione' type='text'>
      <input name='foglio'><input name='particella1'><input name='subalterno1'>
      <input type='submit' name='scelta' value='Ricerca'></form>""", menu_visure(s))


@app.post("/Visure/immobile")
async def immobile_cerca(req: Request):
    s = sessione(req)
    if not s or not s.get("ufficio"):
        return scaduta()
    f = await req.form()
    comune = str(f.get("denomComune", "")).rstrip("#")
    k = (comune, f.get("foglio"), f.get("particella1"))
    s["elenco"] = ("T" if f.get("tipoCatasto") == "T" else "F", k, f.get("subalterno1") or "")
    return elenco_immobili(s)


def elenco_immobili(s, messaggio=""):
    tipo, k, sub = s["elenco"]
    righe = (TERRENI if tipo == "T" else IMMOBILI).get(k, [])
    if sub:
        righe = [r for r in righe if r.get("Sub") == sub]
    if not righe:
        return pagina("<p>NESSUNA CORRISPONDENZA TROVATA</p>", menu_visure(s))
    valori = [("T" + r["Particella"]) if tipo == "T" else r["Sub"] for r in righe]
    return pagina(f"<form method='post' action='/Visure/intestati'>{tabella(righe, 'visImm', valori)}"
                  "<input type='submit' name='intestati' value='Intestati'><input type='submit' name='indietro' value='Indietro'></form>",
                  menu_visure(s), messaggio)


@app.post("/Visure/intestati")
async def intestati(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    v = f.get("visImm")
    if v == "41":   # bene comune non censibile: SISTER resta sull'elenco con un messaggio
        return elenco_immobili(s, "Per l'immobile selezionato non sono presenti intestati")
    righe = INTESTATI.get(v, [])
    return pagina(f"<form method='post' action='/Visure/elenco'>{tabella(righe, 'x', [str(i) for i in range(len(righe))])}"
                  "<input type='submit' name='indietro' value='Indietro'></form>", menu_visure(s))


@app.post("/Visure/elenco")
async def torna_elenco(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    if s.get("elenco"):
        return elenco_immobili(s)
    return pagina("<p>?</p>", menu_visure(s))


@app.get("/Visure/indirizzo")
async def indirizzo(req: Request):
    s = sessione(req)
    if not s or not s.get("ufficio"):
        return scaduta()
    top = "".join(f"<option value='{t}'>{t}</option>" for t in ("CORSO", "PIAZZA", "VIA", "VIALE"))
    return pagina(f"""<form name='ricercaIndForm' method='post' action='/Visure/indirizzo'>
      {comuni_opts(s, 'comuneCat')}<select name='toponimo'><option value=''></option>{top}</select>
      <input name='indirizzo'><input type='radio' name='parIntera' value='1'><input type='radio' name='parIntera' value='0'>
      <input type='submit' name='ricerca' value='Ricerca'></form>""", menu_visure(s))


@app.post("/Visure/indirizzo")
async def indirizzo_cerca(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    k = (str(f.get("comuneCat", "")).rstrip("#"), f.get("toponimo"), str(f.get("indirizzo", "")).upper())
    vie = VIE.get(k)
    if not vie:
        return pagina("<p>Nessuna corrispondenza trovata</p>", menu_visure(s))
    opts = "".join(f"<option value='{i}'>{v}</option>" for i, v in enumerate(vie))
    return pagina(f"""<form name='SceltaIndirizzoForm' method='post' action='/Visure/civici'>
      <select name='indirizzoSel'>{opts}</select><input name='numCivicoDal'><input name='numCivicoAl'>
      <input type='submit' name='ricerca' value='Ricerca'></form>""", menu_visure(s))


@app.post("/Visure/civici")
async def civici(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    via = VIE[("MILANO", "VIA", "ROMA")][int(f.get("indirizzoSel"))]
    if via != "VIA ROMA" or (f.get("numCivicoDal") and f.get("numCivicoDal") != "10"):
        return pagina("<p>NESSUNA CORRISPONDENZA TROVATA</p>", menu_visure(s))
    s["elenco"] = ("F", ("MILANO", "520", "515"), "")
    return elenco_immobili(s)


@app.get("/Visure/pf")
async def pf(req: Request):
    s = sessione(req)
    if not s or not s.get("ufficio"):
        return scaduta()
    return pagina(f"""<form method='post' action='/Visure/pf'>
      <select name='tipoCatasto'><option value='E'>Entrambi</option><option value='F'>F</option><option value='T'>T</option></select>
      <select name='comuneCat'><option value='$'>TUTTA LA PROVINCIA</option>{''.join(f"<option value='{c}#'>{c}</option>" for c in COMUNI[s['ufficio']])}</select>
      <input type='radio' name='selDatiAna' value='cognome'><input type='radio' name='selDatiAna' value='CF_PF'>
      <input name='cognome'><input name='nome'><input name='cod_fisc_pf'>
      <input name='richiedente'><input name='motivoText'>
      <input type='submit' name='ricerca' value='Ricerca'></form>""", menu_visure(s))


@app.post("/Visure/pf")
async def pf_cerca(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    if not f.get("motivoText"):
        return pagina("<p>Indicare il motivo della richiesta</p>", menu_visure(s))
    righe = SOGGETTI.get(str(f.get("cognome", "")).upper(), [])
    if f.get("selDatiAna") == "CF_PF":
        righe = [r for sog in SOGGETTI.values() for r in sog if r["Codice fiscale"] == f.get("cod_fisc_pf")]
    if not righe:
        return pagina("<p>NESSUNA CORRISPONDENZA TROVATA</p>", menu_visure(s))
    s["soggetti"] = righe
    return pagina(f"<form method='post' action='/Visure/sog_imm'>{tabella(righe, 'sog', [str(i) for i in range(len(righe))])}"
                  "<input type='submit' name='immobili' value='Immobili'><input type='submit' name='nuova' value='Nuova ricerca'></form>",
                  menu_visure(s))


@app.post("/Visure/sog_imm")
async def sog_imm(req: Request):
    s = sessione(req)
    if not s:
        return scaduta()
    f = await req.form()
    if f.get("indietro"):
        righe = s["soggetti"]
        return pagina(f"<form method='post' action='/Visure/sog_imm'>{tabella(righe, 'sog', [str(i) for i in range(len(righe))])}"
                      "<input type='submit' name='immobili' value='Immobili'></form>", menu_visure(s))
    i = int(f.get("sog"))
    imm = [{"Catasto": "F", "Comune": "MILANO", "Foglio": "520", "Particella": "515", "Sub": "26", "Titolarità": "Proprieta' 1/2"}] if i == 0 else []
    return pagina(f"<form method='post' action='/Visure/sog_imm'>{tabella(imm, 'imm', ['0'] * len(imm))}"
                  "<input type='submit' name='indietro' value='Indietro'></form>", menu_visure(s))
