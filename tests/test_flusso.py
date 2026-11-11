"""Prove del servizio contro il SISTER finto (tests/fake_sister.py): tutto il percorso, dal login alle
tabelle, con la sessione che scade, l'utenza occupata, le credenziali sbagliate e l'uscita dopo l'inattività.

    CHROMIUM_PATH=/percorso/chrome python -m pytest tests -q
"""
import os
import socket
import sys
import threading
import time

import pytest
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _porta():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORTA = _porta()
BASE = f"http://127.0.0.1:{PORTA}"
os.environ.update({
    "SISTER_HOME": BASE + "/Servizi/", "SISTER_LOGIN_URL": BASE + "/sam/UI/Login",
    "SISTER_LOGOUT_URL": BASE + "/sam/UI/Logout", "SISTER_HOST_RE": rf"^http://127\.0\.0\.1:{PORTA}/",
    "SISTER_USERNAME": "UTENTE01", "SISTER_PASSWORD": "segreta",
    "IDLE_SECONDI": "1.5", "PAUSA_UTENZA_OCCUPATA": "1", "ATTESA_UTENZA_OCCUPATA": "4", "TEMPO_MAX_RICERCA": "60",
})

import fake_sister  # noqa: E402
from fake_sister import STATO  # noqa: E402

_server = uvicorn.Server(uvicorn.Config(fake_sister.app, host="127.0.0.1", port=PORTA, log_level="warning"))
threading.Thread(target=_server.run, daemon=True).start()
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORTA), timeout=0.2).close()
        break
    except OSError:
        time.sleep(0.05)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


@pytest.fixture()
def client():
    fake_sister.azzera()
    main.STATO.update(blocco=None, sessione=False, ultimo_errore=None, ultima_ricerca=None)
    main.LAVORI.clear()
    main.LAVORATORE = main.Lavoratore()
    with TestClient(main.app) as c:
        yield c


def cerca(c, **r):
    x = c.post("/ricerche", json={"ricerca": r})
    assert x.status_code == 200, x.text
    id = x.json()["id"]
    for _ in range(600):
        j = c.get(f"/ricerche/{id}").json()
        if j["stato"] in ("completata", "errore"):
            return j
        time.sleep(0.1)
    raise AssertionError("ricerca non finita")


PART = dict(tipo="particella", provincia="MI", comune="milano", foglio="520", particella="515")


def test_particella_con_intestati(client):
    j = cerca(client, **PART)
    assert j["stato"] == "completata", j["errore"]
    imm = j["risultato"]["immobili"]
    assert [i["dati"]["Sub"] for i in imm] == ["1", "26", "30"]
    assert [x["Nominativo o denominazione"] for x in imm[1]["intestati"]] == ["MONTE ALBERTO", "BIANCHI ANNA"]
    assert all(i["catasto"] == "F" for i in imm)
    assert STATO["logins"] == 1


def test_subalterno(client):
    j = cerca(client, **PART, subalterno="26")
    assert j["stato"] == "completata", j["errore"]
    imm = j["risultato"]["immobili"]
    assert len(imm) == 1 and len(imm[0]["intestati"]) == 2


def test_fabbricati_e_terreni(client):
    j = cerca(client, tipo="particella", provincia="Milano", comune="MILANO", foglio="12", particella="34", catasto="E")
    assert j["stato"] == "completata", j["errore"]
    imm = j["risultato"]["immobili"]
    assert [i["catasto"] for i in imm] == ["T"]
    assert imm[0]["intestati"][0]["Nominativo o denominazione"] == "VERDI LUCA"
    assert any("NON ha" in a.upper() or "non ha" in a for a in j["risultato"]["avvisi"])


def test_particella_inesistente(client):
    j = cerca(client, tipo="particella", provincia="MI", comune="milano", foglio="1", particella="1")
    assert j["stato"] == "completata"
    assert j["risultato"]["immobili"] == [] and j["risultato"]["avvisi"]


def test_comune_e_provincia_sconosciuti(client):
    j = cerca(client, tipo="particella", provincia="MI", comune="Pippolandia", foglio="1", particella="1")
    assert j["stato"] == "errore" and "Pippolandia" in j["errore"]
    j = cerca(client, tipo="particella", provincia="Atlantide", comune="x", foglio="1", particella="1")
    assert j["stato"] == "errore" and "ufficio" in j["errore"]


def test_monza_dal_comune(client):
    j = cerca(client, tipo="particella", provincia="Milano", comune="Cornate d'Adda", foglio="1", particella="1")
    assert j["stato"] == "completata", j["errore"]


def test_indirizzo(client):
    j = cerca(client, tipo="indirizzo", provincia="MI", comune="Milano", indirizzo="via roma 10")
    assert j["stato"] == "completata", j["errore"]
    assert j["risultato"]["via"] == "VIA ROMA"
    assert len(j["risultato"]["immobili"]) == 3
    assert j["risultato"]["immobili"][0]["intestati"][0]["Nominativo o denominazione"] == "ROSSI MARIO"


def test_indirizzo_sconosciuto(client):
    j = cerca(client, tipo="indirizzo", provincia="MI", comune="Milano", indirizzo="via inesistente")
    assert j["stato"] == "errore" and "non conosce" in j["errore"]


def test_soggetto(client):
    j = cerca(client, tipo="soggetto", provincia="MI", cognome="Monte", nome="Alberto")
    assert j["stato"] == "completata", j["errore"]
    assert len(j["risultato"]["soggetti"]) == 2
    imm = j["risultato"]["immobili"]
    assert len(imm) == 1 and imm[0]["dati"]["Sub"] == "26" and imm[0]["soggetto"]["Codice fiscale"] == "MNTLRT60B02F205Y"


def test_soggetto_per_codice_fiscale(client):
    j = cerca(client, tipo="soggetto", provincia="MI", codice_fiscale="mntlrt60b02f205y")
    assert j["stato"] == "completata", j["errore"]
    assert len(j["risultato"]["soggetti"]) == 1 and len(j["risultato"]["immobili"]) == 1


def test_soggetto_senza_nome(client):
    x = client.post("/ricerche", json={"ricerca": {"tipo": "soggetto", "provincia": "MI"}})
    assert x.status_code == 422


def test_sessione_che_scade_a_meta(client):
    STATO["scadi_dopo"] = 12   # SISTER chiude la sessione durante la lettura degli intestati
    j = cerca(client, **PART)
    assert j["stato"] == "completata", j["errore"]
    assert len(j["risultato"]["immobili"]) == 3
    assert STATO["logins"] == 2


def test_utenza_occupata_poi_libera(client):
    STATO["occupata"] = True
    threading.Timer(1.5, lambda: STATO.update(occupata=False)).start()
    j = cerca(client, **PART)
    assert j["stato"] == "completata", j["errore"]
    assert STATO["logins"] >= 2


def test_utenza_sempre_occupata(client):
    STATO["occupata"] = True
    j = cerca(client, **PART)
    assert j["stato"] == "errore" and "in uso" in j["errore"]


def test_credenziali_errate_non_riprova(client):
    STATO["password"] = "cambiata"
    j = cerca(client, **PART)
    assert j["stato"] == "errore" and "Credenziali errate" in j["errore"]
    x = client.post("/ricerche", json={"ricerca": PART})
    assert x.status_code == 503
    assert client.get("/stato").json()["ok"] is False


def test_esce_dopo_inattivita_e_tra_ricerche_resta_dentro(client):
    cerca(client, **PART)
    cerca(client, tipo="indirizzo", provincia="MI", comune="Milano", indirizzo="via roma", civico="10")
    assert STATO["logins"] == 1           # due ricerche vicine, un login solo
    assert client.get("/stato").json()["sessione_aperta"] is True
    time.sleep(2.5)
    assert STATO["logouts"] == 1 and STATO["attiva"] is None
    assert client.get("/stato").json()["sessione_aperta"] is False


def test_informativa_solo_la_prima_volta(client):
    STATO["lettura_ogni_volta"] = False
    a = cerca(client, **PART)
    b = cerca(client, **PART, subalterno="1")
    assert a["stato"] == b["stato"] == "completata"


def test_coda(client):
    ids = [client.post("/ricerche", json={"ricerca": PART}).json() for _ in range(3)]
    assert ids[2]["posizione"] in (2, 3)
    for x in ids:
        for _ in range(600):
            if client.get(f"/ricerche/{x['id']}").json()["stato"] == "completata":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("coda bloccata")


def test_sister_lento(client):
    STATO["lento"] = True
    a = cerca(client, **PART, subalterno="26")
    b = cerca(client, tipo="soggetto", provincia="MI", cognome="Monte", nome="Alberto")
    c = cerca(client, tipo="indirizzo", provincia="MI", comune="Milano", indirizzo="via roma 10")
    assert [a["stato"], b["stato"], c["stato"]] == ["completata"] * 3, (a["errore"], b["errore"], c["errore"])
    assert len(a["risultato"]["immobili"][0]["intestati"]) == 2
    assert STATO["logins"] == 1


def test_ricerca_sconosciuta(client):
    assert client.get("/ricerche/abc").status_code == 404


def test_esce_alla_chiusura():
    fake_sister.azzera()
    main.LAVORI.clear()
    main.STATO.update(blocco=None)
    main.LAVORATORE = main.Lavoratore()
    with TestClient(main.app) as c:
        cerca(c, **PART)
        assert STATO["attiva"] is not None
    assert STATO["attiva"] is None and STATO["logouts"] == 1
