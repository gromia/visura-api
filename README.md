# visura-api

Servizio interno di Gromia che fa le ricerche catastali su SISTER (Agenzia delle Entrate) per la sezione
**Visure** della dashboard (gromia/gromia-app). Gira su Railway, progetto `gromia-prototipo`, servizio
`visura-api`, senza dominio pubblico: il sito lo chiama sulla rete privata (`VISURA_URL`).

## Come funziona

- **Una sessione SISTER per volta.** SISTER permette una sola sessione per utenza. Il servizio entra alla
  prima ricerca, fa le ricerche in coda una dopo l'altra ed esce `IDLE_SECONDI` dopo l'ultima (60 secondi).
  Tra una ricerca e l'altra l'utenza è libera: chi entra a mano su SISTER non resta chiuso fuori.
- **Stesso percorso di gromia/sister-parser**, il crawler in produzione: pagina unica dell'Agenzia (scheda
  Sister), informativa privacy, Consultazioni e Certificazioni > Visure catastali > Conferma Lettura >
  convenzione (`SISTER_CONVENZIONE`, GROMIA) > ufficio provinciale, poi la ricerca dal menu.
- **Errori gestiti:** sessione chiusa da SISTER a metà ricerca → nuovo accesso e la ricerca si ripete una
  volta; utenza in uso altrove ("Utente gia' in sessione") → attesa e nuovo tentativo ogni 30 secondi per
  8 minuti; credenziali rifiutate → nessun altro tentativo fino al riavvio (altri tentativi bloccherebbero
  l'utenza) e il motivo compare in `/stato` e nelle ricerche.

## API

`POST /ricerche` con `{"ricerca": {...}}`, una di:

| tipo | campi |
|---|---|
| `particella` | `provincia`, `comune`, `foglio`, `particella`, `sezione`?, `subalterno`?, `catasto` (`F` fabbricati, `T` terreni, `E` entrambi) |
| `indirizzo` | `provincia`, `comune`, `indirizzo` ("via Roma 10"), `toponimo`?, `civico`?, `catasto` |
| `soggetto` | `provincia`, `comune`? (vuoto = tutta la provincia), `tipo_soggetto` (`PF`/`PNF`), `cognome`+`nome` o `codice_fiscale` o `denominazione`, `catasto`, `richiedente`?, `motivo`? |

La provincia si scrive per esteso o con la sigla (MI); i comuni di Monza e Brianza e di Fermo vanno da soli
all'ufficio giusto. Risposta: `{id, stato: "in coda", posizione}`.

`GET /ricerche/{id}`: `stato` (`in coda`, `in corso` con `fase`, `completata` con `risultato`, `errore` con
`errore`). Il risultato è `{immobili: [{catasto, dati: {colonne di SISTER}, intestati: [...]}], avvisi: [...]}`;
per il soggetto anche `soggetti` e, in ogni immobile, `soggetto`. Gli intestati si leggono per le prime
`MAX_INTESTATI` unità (40). Le ricerche restano in memoria `CONSERVA_ORE` (24): lo storico è nel sito.
Una ricerca sconosciuta (servizio riavviato) risponde 404.

`GET /stato`: credenziali presenti, sessione aperta, ricerche in coda, eventuale blocco e ultimo errore.

## Variabili

`SISTER_USERNAME`, `SISTER_PASSWORD` (le inserisce l'utente su Railway). Facoltative: `SISTER_CONVENZIONE`,
`IDLE_SECONDI`, `TEMPO_MAX_RICERCA`, `ATTESA_UTENZA_OCCUPATA`, `PAUSA_UTENZA_OCCUPATA`, `MAX_INTESTATI`,
`CONSERVA_ORE`. `VISURA_SELFTEST` (JSON di una ricerca) la esegue all'avvio e scrive nei log solo la forma del
risultato (colonne e numeri, mai nomi), per controllare il servizio dopo un rilascio.

## Prove

`tests/fake_sister.py` è un SISTER finto con le stesse pagine e gli stessi selettori; i test fanno tutto il
percorso con Chromium, compresi sessione scaduta, utenza occupata, credenziali sbagliate e uscita per
inattività:

```
pip install -r requirements-dev.txt && playwright install chromium
python -m pytest tests -q
```
