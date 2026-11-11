"""visura-api: ricerche catastali su SISTER per la dashboard di Gromia.

POST /ricerche mette in coda una ricerca (particella, indirizzo o soggetto) e risponde subito con il suo id;
GET /ricerche/{id} ne dà lo stato ("in coda", "in corso" con la fase, "completata" con il risultato, "errore"
con il motivo); GET /stato dice se il servizio è pronto. Il servizio si raggiunge solo dalla rete privata di
Railway (dal sito, VISURA_URL), senza dominio pubblico.

SISTER consente una sola sessione per utenza, quindi le ricerche passano da un solo lavoratore, una alla
volta. La sessione si apre alla prima ricerca e si chiude IDLE_SECONDI dopo l'ultima: tra una ricerca e
l'altra l'utenza resta libera anche per chi entra a mano. Se SISTER chiude la sessione a metà, il lavoratore
rientra e ripete la ricerca una volta; se l'utenza è aperta altrove aspetta e riprova; se SISTER rifiuta le
credenziali smette di provarci (altri tentativi bloccherebbero l'utenza) finché non si riavvia il servizio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional, Union

from fastapi import FastAPI, HTTPException
from playwright.async_api import Error as PWError, TimeoutError as PWTimeout, async_playwright
from pydantic import BaseModel, ConfigDict, Field

import sister

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("visura")

USERNAME = os.environ.get("SISTER_USERNAME", "")
PASSWORD = os.environ.get("SISTER_PASSWORD", "")
IDLE_SECONDI = float(os.environ.get("IDLE_SECONDI", "60"))               # sessione aperta dopo l'ultima ricerca
TEMPO_MAX = float(os.environ.get("TEMPO_MAX_RICERCA", "420"))            # una ricerca, intestati compresi
ATTESA_OCCUPATA = float(os.environ.get("ATTESA_UTENZA_OCCUPATA", "480"))  # quanto aspettare l'utenza libera
PAUSA_OCCUPATA = float(os.environ.get("PAUSA_UTENZA_OCCUPATA", "30"))
MAX_INTESTATI = int(os.environ.get("MAX_INTESTATI", "40"))
CONSERVA_ORE = float(os.environ.get("CONSERVA_ORE", "24"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH") or None
MOTIVO = "Verifica della titolarità per un incarico di intermediazione immobiliare"


# ---- richieste ------------------------------------------------------------------------------------

class Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    provincia: str = Field(..., min_length=2, max_length=60)
    comune: str = Field("", max_length=80)


class Particella(Base):
    tipo: Literal["particella"]
    comune: str = Field(..., min_length=1, max_length=80)
    foglio: str = Field(..., pattern=r"^[0-9A-Za-z]{1,10}$")
    particella: str = Field(..., pattern=r"^[0-9A-Za-z]{1,10}$")
    sezione: str = Field("", pattern=r"^[0-9A-Za-z]{0,6}$")
    subalterno: str = Field("", pattern=r"^[0-9A-Za-z]{0,10}$")
    catasto: Literal["F", "T", "E"] = "F"


class Indirizzo(Base):
    tipo: Literal["indirizzo"]
    comune: str = Field(..., min_length=1, max_length=80)
    indirizzo: str = Field(..., min_length=2, max_length=120)
    toponimo: str = Field("", max_length=30)
    civico: str = Field("", pattern=r"^[0-9A-Za-z/]{0,10}$")
    catasto: Literal["F", "T"] = "F"


class Soggetto(Base):
    tipo: Literal["soggetto"]
    tipo_soggetto: Literal["PF", "PNF"] = "PF"
    cognome: str = Field("", max_length=60)
    nome: str = Field("", max_length=60)
    codice_fiscale: str = Field("", pattern=r"^[0-9A-Za-z]{0,16}$")
    denominazione: str = Field("", max_length=120)
    catasto: Literal["F", "T", "E"] = "E"
    richiedente: str = Field("", max_length=60)
    motivo: str = Field("", max_length=100)


Richiesta = Union[Particella, Indirizzo, Soggetto]


class Corpo(BaseModel):
    ricerca: Richiesta = Field(..., discriminator="tipo")


# ---- lavori ---------------------------------------------------------------------------------------

class Lavoro:
    def __init__(self, r: Richiesta):
        self.id = uuid.uuid4().hex
        self.richiesta = r
        self.stato = "in coda"
        self.fase = ""
        self.risultato: Optional[dict] = None
        self.errore: Optional[str] = None
        self.creato = time.time()
        self.finito: Optional[float] = None

    def json(self) -> dict:
        return {"id": self.id, "tipo": self.richiesta.tipo, "stato": self.stato, "fase": self.fase,
                "posizione": _posizione(self), "risultato": self.risultato, "errore": self.errore,
                "creato": self.creato, "finito": self.finito}


LAVORI: dict[str, Lavoro] = {}
STATO = {"sessione": False, "blocco": None, "ultimo_errore": None, "ultima_ricerca": None}


def _pulisci_vecchi():
    limite = time.time() - CONSERVA_ORE * 3600
    for k in [k for k, v in LAVORI.items() if v.finito and v.finito < limite]:
        del LAVORI[k]


def _posizione(l: Lavoro) -> Optional[int]:
    if l.stato != "in coda":
        return None
    in_coda = sorted((x for x in LAVORI.values() if x.stato == "in coda"), key=lambda x: x.creato)
    return next((i + 1 for i, x in enumerate(in_coda) if x.id == l.id), None)


# ---- lavoratore -----------------------------------------------------------------------------------

class Lavoratore:
    def __init__(self):
        self.coda: "asyncio.Queue[Lavoro]" = asyncio.Queue()
        self.pw = None
        self.browser = None
        self.sessione: Optional[sister.Sessione] = None

    async def avvia(self):
        self.pw = await async_playwright().start()

    async def _browser(self):
        if self.browser is None or not self.browser.is_connected():
            self.browser = await self.pw.chromium.launch(
                headless=True, executable_path=CHROMIUM_PATH, args=["--no-sandbox", "--disable-dev-shm-usage"])
        return self.browser

    async def chiudi_sessione(self):
        if self.sessione:
            try:
                await self.sessione.chiudi()
            except Exception as e:
                log.warning("chiusura della sessione: %s", type(e).__name__)
        self.sessione = None
        STATO["sessione"] = False

    async def ferma(self):
        await self.chiudi_sessione()
        try:
            if self.browser:
                await self.browser.close()
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass

    async def _entra(self, l: Lavoro):
        """Apre la sessione se non è aperta. Con l'utenza occupata aspetta e riprova fino ad ATTESA_OCCUPATA."""
        if self.sessione and self.sessione.aperta:
            return
        if STATO["blocco"]:
            raise sister.LoginRifiutato(STATO["blocco"])
        inizio = time.monotonic()
        while True:
            self.sessione = sister.Sessione(await self._browser(), USERNAME, PASSWORD)
            self.sessione.fase = lambda f: setattr(l, "fase", f)
            try:
                await self.sessione.apri()
                STATO["sessione"] = True
                return
            except sister.UtenzaOccupata:
                await self.chiudi_sessione()
                if time.monotonic() - inizio + PAUSA_OCCUPATA > ATTESA_OCCUPATA:
                    raise sister.UtenzaOccupata(
                        "L'utenza SISTER è in uso da un'altra parte (qualcuno collegato a mano o un altro programma) "
                        f"da più di {max(1, int(ATTESA_OCCUPATA // 60))} minuti: chi la usa deve uscire con \"Esci\", "
                        "poi ripeti la ricerca.")
                l.fase = f"Utenza SISTER occupata altrove: riprovo tra {int(PAUSA_OCCUPATA)} secondi"
                log.info("utenza occupata, nuovo tentativo tra %ss", PAUSA_OCCUPATA)
                await asyncio.sleep(PAUSA_OCCUPATA)
            except sister.LoginRifiutato as e:
                STATO["blocco"] = str(e)
                await self.chiudi_sessione()
                raise
            except BaseException:
                await self.chiudi_sessione()
                raise

    async def esegui(self, l: Lavoro):
        l.stato = "in corso"
        r = l.richiesta
        log.info("ricerca %s %s", l.id[:8], r.tipo)
        for tentativo in (1, 2):
            try:
                await self._entra(l)
                self.sessione.fase = lambda f: setattr(l, "fase", f)
                l.risultato = await asyncio.wait_for(_ricerca(self.sessione, r), timeout=TEMPO_MAX)
                l.stato, l.fase = "completata", ""
                log.info("ricerca %s completata: %d immobili", l.id[:8], len(l.risultato.get("immobili", [])))
                return
            except sister.NonTrovato as e:
                l.stato, l.errore = "errore", str(e)
                return
            except (sister.LoginRifiutato, sister.UtenzaOccupata) as e:
                l.stato, l.errore = "errore", str(e)
                await self.chiudi_sessione()
                return
            except asyncio.TimeoutError:
                await self.chiudi_sessione()
                l.stato = "errore"
                l.errore = "SISTER è troppo lento in questo momento: la ricerca ha superato il tempo massimo. Riprova tra qualche minuto."
                return
            except (sister.SessionePersa, PWTimeout, PWError) as e:
                motivo = str(e) if isinstance(e, sister.SisterError) else type(e).__name__
                log.warning("ricerca %s, tentativo %d: %s", l.id[:8], tentativo, motivo[:300])
                await self.chiudi_sessione()
                if tentativo == 2:
                    l.stato = "errore"
                    l.errore = f"SISTER non ha risposto come previsto anche al secondo tentativo ({motivo[:160]}). Riprova tra qualche minuto."
                    return
                l.fase = "SISTER ha chiuso la sessione: rientro e ripeto la ricerca"
            except Exception as e:
                log.error("ricerca %s: errore imprevisto %s\n%s", l.id[:8], type(e).__name__, traceback.format_exc()[-1500:])
                await self.chiudi_sessione()
                l.stato, l.errore = "errore", f"Errore imprevisto del servizio visure ({type(e).__name__}): riprova."
                return

    async def ciclo(self):
        while True:
            try:
                l = await asyncio.wait_for(self.coda.get(), timeout=IDLE_SECONDI if self.sessione else None)
            except asyncio.TimeoutError:
                log.info("nessuna ricerca da %ss: esco da SISTER", int(IDLE_SECONDI))
                await self.chiudi_sessione()
                continue
            try:
                await self.esegui(l)
            finally:
                l.finito = time.time()
                if l.stato == "errore":
                    STATO["ultimo_errore"] = {"quando": l.finito, "errore": l.errore}
                STATO["ultima_ricerca"] = {"quando": l.finito, "stato": l.stato}
                _pulisci_vecchi()


async def _ricerca(s: sister.Sessione, r: Richiesta) -> dict:
    if isinstance(r, Particella):
        out = {"immobili": [], "avvisi": []}
        for c in (["F", "T"] if r.catasto == "E" else [r.catasto]):
            if r.subalterno and c == "T":
                continue
            x = await sister.cerca_particella(s, r.provincia, r.comune, r.foglio, r.particella, r.sezione,
                                              r.subalterno, c, MAX_INTESTATI)
            out["immobili"] += x["immobili"]
            out["avvisi"] += x["avvisi"]
        return out
    if isinstance(r, Indirizzo):
        return await sister.cerca_indirizzo(s, r.provincia, r.comune, r.indirizzo, r.toponimo, r.civico,
                                            r.catasto, MAX_INTESTATI)
    return await sister.cerca_soggetto(s, r.provincia, r.comune, r.tipo_soggetto, r.cognome, r.nome,
                                       r.codice_fiscale.upper(), r.denominazione, r.catasto, r.richiedente,
                                       r.motivo or MOTIVO)


# ---- app ------------------------------------------------------------------------------------------

LAVORATORE = Lavoratore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await LAVORATORE.avvia()
    compito = asyncio.create_task(LAVORATORE.ciclo())
    prova = os.environ.get("VISURA_SELFTEST", "").strip()
    if prova:
        try:
            dati = json.loads(prova)
            for d in dati if isinstance(dati, list) else [dati]:
                l = Lavoro(Corpo(ricerca=d).ricerca)
                LAVORI[l.id] = l
                await LAVORATORE.coda.put(l)
                asyncio.create_task(_riassumi(l))
        except Exception as e:
            log.error("VISURA_SELFTEST non valido: %s", e)
    log.info("servizio visure avviato (credenziali %s)", "presenti" if USERNAME and PASSWORD else "MANCANTI")
    try:
        yield
    finally:
        compito.cancel()
        await LAVORATORE.ferma()
        log.info("servizio visure fermato, sessione SISTER chiusa")


async def _riassumi(l: Lavoro):
    """Ricerca di prova all'avvio: nei log solo la struttura del risultato, mai i dati delle persone."""
    while l.stato in ("in coda", "in corso"):
        await asyncio.sleep(2)
    x = l.risultato or {}
    imm = x.get("immobili", [])
    con = next((i["intestati"] for i in imm if i.get("intestati")), None) or [{}]
    log.info("SELFTEST %s %s: errore=%s immobili=%d soggetti=%d colonne=%s intestati=%s colonne_intestati=%s avvisi=%s",
             l.richiesta.tipo, l.stato, l.errore, len(imm), len(x.get("soggetti", [])), list(imm[0]["dati"]) if imm else [],
             [len(i["intestati"] or []) for i in imm][:10], list(con[0]), x.get("avvisi"))


app = FastAPI(title="visura-api", lifespan=lifespan)


@app.post("/ricerche")
async def nuova(corpo: Corpo):
    r = corpo.ricerca
    if isinstance(r, Soggetto):
        if r.tipo_soggetto == "PF" and not (r.codice_fiscale or r.cognome):
            raise HTTPException(422, "Scrivi cognome e nome, oppure il codice fiscale.")
        if r.tipo_soggetto == "PNF" and not (r.codice_fiscale or r.denominazione):
            raise HTTPException(422, "Scrivi la denominazione, oppure codice fiscale o partita IVA.")
    if STATO["blocco"]:
        raise HTTPException(503, STATO["blocco"])
    if not (USERNAME and PASSWORD):
        raise HTTPException(503, "Credenziali SISTER mancanti su visura-api (SISTER_USERNAME e SISTER_PASSWORD).")
    l = Lavoro(r)
    LAVORI[l.id] = l
    await LAVORATORE.coda.put(l)
    return l.json()


@app.get("/ricerche/{id}")
async def leggi(id: str):
    l = LAVORI.get(id)
    if not l:
        raise HTTPException(404, "Ricerca sconosciuta: il servizio visure è stato riavviato, ripeti la ricerca.")
    return l.json()


@app.get("/stato")
async def stato():
    return {"ok": not STATO["blocco"] and bool(USERNAME and PASSWORD), "credenziali": bool(USERNAME and PASSWORD),
            "sessione_aperta": STATO["sessione"], "in_coda": LAVORATORE.coda.qsize(), "blocco": STATO["blocco"],
            "ultimo_errore": STATO["ultimo_errore"], "ultima_ricerca": STATO["ultima_ricerca"]}


@app.get("/")
async def radice():
    return {"servizio": "visura-api", "stato": "/stato"}
