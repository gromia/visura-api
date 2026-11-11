"""Da provincia (nome o sigla) e comune all'ufficio provinciale di SISTER ("MILANO Territorio").

Come in gromia/sister-parser: i comuni della provincia di Monza e della Brianza e di quella di Fermo
stanno negli uffici di MONZA e FERMO, che SISTER a volte non elenca: allora si prova l'ufficio della
provincia da cui sono nate (MILANO, ASCOLI PICENO).
"""
import re
import unicodedata

SIGLE = {
    "AG": "AGRIGENTO", "AL": "ALESSANDRIA", "AN": "ANCONA", "AO": "AOSTA", "AP": "ASCOLI PICENO", "AQ": "L'AQUILA",
    "AR": "AREZZO", "AT": "ASTI", "AV": "AVELLINO", "BA": "BARI", "BG": "BERGAMO", "BI": "BIELLA", "BL": "BELLUNO",
    "BN": "BENEVENTO", "BO": "BOLOGNA", "BR": "BRINDISI", "BS": "BRESCIA", "BT": "BARLETTA", "BZ": "BOLZANO",
    "CA": "CAGLIARI", "CB": "CAMPOBASSO", "CE": "CASERTA", "CH": "CHIETI", "CL": "CALTANISSETTA", "CN": "CUNEO",
    "CO": "COMO", "CR": "CREMONA", "CS": "COSENZA", "CT": "CATANIA", "CZ": "CATANZARO", "EN": "ENNA", "FC": "FORLI",
    "FE": "FERRARA", "FG": "FOGGIA", "FI": "FIRENZE", "FM": "FERMO", "FR": "FROSINONE", "GE": "GENOVA",
    "GO": "GORIZIA", "GR": "GROSSETO", "IM": "IMPERIA", "IS": "ISERNIA", "KR": "CROTONE", "LC": "LECCO",
    "LE": "LECCE", "LI": "LIVORNO", "LO": "LODI", "LT": "LATINA", "LU": "LUCCA", "MB": "MONZA", "MC": "MACERATA",
    "ME": "MESSINA", "MI": "MILANO", "MN": "MANTOVA", "MO": "MODENA", "MS": "MASSA", "MT": "MATERA", "NA": "NAPOLI",
    "NO": "NOVARA", "NU": "NUORO", "OR": "ORISTANO", "PA": "PALERMO", "PC": "PIACENZA", "PD": "PADOVA",
    "PE": "PESCARA", "PG": "PERUGIA", "PI": "PISA", "PN": "PORDENONE", "PO": "PRATO", "PR": "PARMA",
    "PT": "PISTOIA", "PU": "PESARO", "PV": "PAVIA", "PZ": "POTENZA", "RA": "RAVENNA", "RC": "REGGIO CALABRIA",
    "RE": "REGGIO EMILIA", "RG": "RAGUSA", "RI": "RIETI", "RM": "ROMA", "RN": "RIMINI", "RO": "ROVIGO",
    "SA": "SALERNO", "SI": "SIENA", "SO": "SONDRIO", "SP": "LA SPEZIA", "SR": "SIRACUSA", "SS": "SASSARI",
    "SU": "CAGLIARI", "SV": "SAVONA", "TA": "TARANTO", "TE": "TERAMO", "TN": "TRENTO", "TO": "TORINO",
    "TP": "TRAPANI", "TR": "TERNI", "TS": "TRIESTE", "TV": "TREVISO", "UD": "UDINE", "VA": "VARESE",
    "VB": "VERBANIA", "VC": "VERCELLI", "VE": "VENEZIA", "VI": "VICENZA", "VR": "VERONA", "VT": "VITERBO",
    "VV": "VIBO VALENTIA",
}

# I comuni delle province di Monza e della Brianza e di Fermo (nomi ISTAT senza accenti e apostrofi)
MONZA_BRIANZA = [
    "monza", "agrate brianza", "aicurzio", "albiate", "arcore", "barlassina", "bellusco", "bernareggio",
    "besana in brianza", "biassono", "bovisio masciago", "briosco", "brugherio", "burago di molgora", "busnago",
    "camparada", "caponago", "carate brianza", "carnate", "cavenago di brianza", "ceriano laghetto",
    "cesano maderno", "cogliate", "concorezzo", "cornate d adda", "correzzana", "desio", "giussano", "lazzate",
    "lentate sul seveso", "lesmo", "limbiate", "lissone", "macherio", "meda", "mezzago", "misinto", "muggio",
    "nova milanese", "ornago", "renate", "roncello", "ronco briantino", "seregno", "seveso", "sovico", "sulbiate",
    "triuggio", "usmate velate", "varedo", "vedano al lambro", "veduggio con colzano", "verano brianza",
    "villasanta", "vimercate",
]
FERMO = [
    "fermo", "altidona", "amandola", "belmonte piceno", "campofilone", "falerone", "francavilla d ete",
    "grottazzolina", "lapedona", "magliano di tenna", "massa fermana", "monsampietro morico", "montappone",
    "monte giberto", "monte rinaldo", "monte san pietrangeli", "monte urano", "monte vidon combatte",
    "monte vidon corrado", "montefalcone appennino", "montefortino", "montegiorgio", "montegranaro",
    "monteleone di fermo", "montelparo", "monterubbiano", "montottone", "moresco", "ortezzano", "pedaso",
    "petritoli", "ponzano di fermo", "porto san giorgio", "porto sant elpidio", "rapagnano",
    "sant elpidio a mare", "santa vittoria in matenano", "servigliano", "smerillo", "torre san patrizio",
]


def _k(s: str) -> str:
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", t.upper())


_COMUNI_MB = {_k(c) for c in MONZA_BRIANZA}
_COMUNI_FM = {_k(c) for c in FERMO}

OFFICES = {"SORA": "FROSINONE", "MONOPOLI": "BARI", "TORTORETO": "TERAMO", "REGGIO DI CALABRIA": "REGGIO CALABRIA"}
FALLBACK = {"MONZA": "MILANO", "FERMO": "ASCOLI PICENO", "REGGIO CALABRIA": "REGGIO DI CALABRIA",
            "BARLETTA": "TRANI", "FORLI": "FORLI'", "PESARO": "PESARO E URBINO", "MASSA": "MASSA CARRARA",
            "BOLZANO": "BOLZANO-BOZEN", "VERBANIA": "VERBANO CUSIO OSSOLA"}


def ufficio_per(provincia: str, comune: str = "") -> list[str]:
    """Nomi dell'ufficio da cercare nell'elenco di SISTER, in ordine: [ufficio, alternativa]."""
    p = " ".join(str(provincia or "").split()).upper()
    p = SIGLE.get(p, p)
    c = _k(comune)
    if c and c in _COMUNI_MB:
        p = "MONZA"
    elif c and c in _COMUNI_FM:
        p = "FERMO"
    elif c and c in {_k(k) for k in OFFICES}:
        p = next(v for k, v in OFFICES.items() if _k(k) == c)
    if _k(p) in ("MONZAEDELLABRIANZA", "MONZABRIANZA"):
        p = "MONZA"
    out = [p]
    if p in FALLBACK:
        out.append(FALLBACK[p])
    return out
