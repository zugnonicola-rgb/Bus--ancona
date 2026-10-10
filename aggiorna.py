#!/usr/bin/env python3
"""Aggiorna orari (dai PDF Conerobus) e avvisi per l'app Bus Ancona."""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------- LE TUE IMPOSTAZIONI (modificabili) ----------
LINEE_NUMERI = ["1/3", "1/4"]
LINEE_LETTERE = ["A", "B", "C", "I"]
PAROLE_CHIAVE = ["torrette", "passetto", "piazza roma", "p.zza roma", "ospedale"]
# --------------------------------------------------------

STATE_FILE = "stato.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SITO = "https://www.conerobus.it"
UA = {"User-Agent": "Mozilla/5.0 (guardiano-bus uso personale)"}


def scarica(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def pulisci(testo_html):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", testo_html or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


# ---------- FONTE 1: sito Conerobus ----------
def avvisi_wp_json():
    url = SITO + "/wp-json/wp/v2/posts?per_page=20&_fields=id,date,link,title,content"
    dati = json.loads(scarica(url))
    return [
        {
            "id": "cb-%s" % p["id"],
            "titolo": pulisci(p["title"]["rendered"]),
            "testo": pulisci(p["content"]["rendered"]),
            "link": p["link"],
            "data": p.get("date", ""),
            "fonte": "Conerobus",
        }
        for p in dati
    ]


def avvisi_feed():
    root = ET.fromstring(scarica(SITO + "/feed/"))
    ns = {"c": "http://purl.org/rss/1.0/modules/content/"}
    out = []
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        corpo = it.findtext("c:encoded", namespaces=ns) or it.findtext("description") or ""
        out.append(
            {
                "id": "cb-" + (it.findtext("guid") or link),
                "titolo": pulisci(it.findtext("title") or ""),
                "testo": pulisci(corpo),
                "link": link,
                "fonte": "Conerobus",
            }
        )
    return out


def avvisi_html():
    pagina = scarica(SITO + "/articoli/")
    trovati = re.findall(r'<h4[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', pagina, re.S)
    out = []
    for link, titolo in trovati[:12]:
        try:
            corpo = pulisci(scarica(link))
        except Exception:
            corpo = ""
        out.append(
            {
                "id": "cb-" + link,
                "titolo": pulisci(titolo),
                "testo": corpo,
                "link": link,
                "fonte": "Conerobus",
            }
        )
    return out


def avvisi_conerobus():
    for nome, f in (("wp-json", avvisi_wp_json), ("feed", avvisi_feed), ("html", avvisi_html)):
        try:
            risultati = f()
            if risultati:
                print("Conerobus OK via", nome, "-", len(risultati), "avvisi")
                return risultati
        except Exception as e:
            print("Conerobus via", nome, "non riuscito:", e)
    raise RuntimeError("impossibile leggere il sito Conerobus")


# ---------- FONTE 2: notizie su scioperi (Google News RSS) ----------
def avvisi_scioperi():
    q = "sciopero trasporto pubblico Ancona OR Marche OR Conerobus OR ATMA when:7d"
    url = "https://news.google.com/rss/search?q=%s&hl=it&gl=IT&ceid=IT:it" % urllib.parse.quote(q)
    root = ET.fromstring(scarica(url))
    limite = datetime.now(timezone.utc) - timedelta(days=7)
    out = []
    for it in root.iter("item"):
        titolo = pulisci(it.findtext("title") or "")
        tl = titolo.lower()
        if "sciopero" not in tl:
            continue
        if not any(k in tl for k in ("ancona", "marche", "conerobus", "atma", "sciopero generale")):
            continue
        try:
            if parsedate_to_datetime(it.findtext("pubDate")) < limite:
                continue
        except Exception:
            pass
        out.append(
            {
                "id": "news-" + (it.findtext("guid") or it.findtext("link") or titolo),
                "titolo": titolo,
                "testo": "",
                "link": it.findtext("link") or "",
                "fonte": "Notizie sciopero",
            }
        )
    return out


# ---------- RILEVANZA ----------
def motivi_rilevanza(avviso):
    testo = avviso["titolo"] + ". " + avviso["testo"]
    bassa = testo.lower()
    motivi = []

    for n in LINEE_NUMERI:
        if re.search(r"(?<![\d/])%s(?![\d])" % re.escape(n), testo):
            motivi.append("linea " + n)

    # lettere solo se compaiono vicino alla parola "linea/linee"
    for seg in re.finditer(r"\bline[ae]\b([^.;:\n]{0,80})", testo, re.I):
        for lettera in re.findall(r"(?<![\w/])([A-Z])(?![\w/])", seg.group(1)):
            if lettera in LINEE_LETTERE and ("linea " + lettera) not in motivi:
                motivi.append("linea " + lettera)

    for k in PAROLE_CHIAVE:
        if k in bassa:
            motivi.append(k.title())

    if "sciopero" in bassa and "sciopero" not in " ".join(motivi).lower():
        motivi.append("sciopero")
    return motivi



# ====================== ORARI DA PDF ======================
import io
from datetime import date

DIAG = [0]
TIME = re.compile(r"^\d{1,2}[.:]\d{2}$")
OGGI = datetime.now(timezone.utc)


def scarica_bytes(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def trova_pdf():
    trovati = {}
    pagine = [SITO + "/servizi-tpl/servizio-urbano-ancona/", SITO + "/servizi-tpl/", SITO + "/orari/"]
    for p in pagine:
        try:
            h = scarica(p)
        except Exception as e:
            print("Pagina non letta:", p, e)
            continue
        for m in re.finditer(r'href="([^"]+\.pdf)"', h, re.I):
            u = urllib.parse.urljoin(p, html.unescape(m.group(1)))
            if "/wp-content/uploads/" in u:
                trovati[u] = 1
    try:
        for r in open("pdf_extra.txt", encoding="utf-8"):
            if r.strip().startswith("http"):
                trovati[r.strip()] = 1
    except FileNotFoundError:
        pass
    return sorted(trovati, key=lambda u: re.findall(r"/(\d{4})/(\d{2})/", u) or [("0", "0")], reverse=True)


def etichetta(testo, url=""):
    t = testo[:1500].upper()
    extra = "EXTRAURBANO" in t or "LIBRETTO" in t
    giorno = "festivo" if re.search(r"\bFESTIVO\b", t) else "feriale"
    stagione = "estivo" if re.search(r"\bESTIVO\b", t) else "invernale"
    scuola = "vacanze" if "VACANZE" in t else ("scuole aperte" if ("SCUOLE APERTE" in t or "SCOLASTIC" in t) else "")
    lab = giorno.capitalize() + " " + stagione + (" (%s)" % scuola if scuola else "")
    if "ferragosto" in url.lower():
        lab = "Ferragosto"
    elif "ridott" in url.lower():
        lab += " ridotto"
    return ("extraurbano" if extra else "urbano"), lab


def norma(t):
    h, m = re.split(r"[.:]", t)
    return "%02d.%02d" % (int(h), int(m))


def righe(parole):
    parole = sorted(parole, key=lambda w: (round(w["top"]), w["x0"]))
    out, cur, top = [], [], None
    for w in parole:
        if top is None or abs(w["top"] - top) > 3:
            if cur:
                out.append(sorted(cur, key=lambda x: x["x0"]))
            cur, top = [], w["top"]
        cur.append(w)
    if cur:
        out.append(sorted(cur, key=lambda x: x["x0"]))
    return out


def nuovo_blocco(testo):
    p = testo.split()
    lid = p[1] if len(p) > 1 else "?"
    if len(p) > 3 and lid.endswith("/") and p[2] == "e":
        lid = " ".join(p[1:4])
    return {"id": lid, "hdr": " ".join(p[2:]), "tabs": []}


def parse_pdf(pdf):
    blocchi, cur, tab, hdr = [], None, None, False
    for page in pdf.pages:
        rr = righe(page.extract_words(x_tolerance=1.5, y_tolerance=2))
        if sum("...." in " ".join(w["text"] for w in r) for r in rr) > 5:
            continue
        for r in rr:
            testo = " ".join(w["text"] for w in r)
            tempi = [w for w in r if TIME.match(w["text"])]
            if "...." in testo:
                continue
            if r[0]["text"] == "Linea" and not tempi:
                cur = nuovo_blocco(testo)
                blocchi.append(cur)
                tab, hdr = None, True
                continue
            if cur is None:
                continue
            if not tempi:
                if hdr and not re.match(r"(?i)^(orario|note|v\d)", testo):
                    cur["hdr"] += " " + testo
                else:
                    hdr = False
                    if re.match(r"(?i)^orario", testo):
                        tab = None
                continue
            hdr = False
            nome = " ".join(w["text"] for w in r if w["x0"] < tempi[0]["x0"] and w["text"] not in ("·", "•")).strip(" ·")
            if not nome or len(nome) > 45 or re.match(r"^\d+\)", nome):
                continue
            if tab is None:
                tab = []
                cur["tabs"].append(tab)
            tab.append((nome, [(w["x0"], w["text"]) for w in tempi]))
    return [b for b in blocchi if b["tabs"]]


def tabella_a_viaggi(tab):
    xs = sorted(x for _, ts in tab for x, _ in ts)
    cols = []
    for x in xs:
        if not cols or x - cols[-1][-1] > 4:
            cols.append([x])
        else:
            cols[-1].append(x)
    centri = [sum(c) / len(c) for c in cols]
    chiavi, cnt, viaggi = [], {}, [{} for _ in centri]
    for nome, ts in tab:
        n = cnt.get(nome, 0)
        cnt[nome] = n + 1
        k = (nome, n)
        chiavi.append(k)
        for x, t in ts:
            i = min(range(len(centri)), key=lambda j: abs(centri[j] - x))
            viaggi[i][k] = norma(t)
    return chiavi, viaggi


def blocco_a_linea(b, lab, report):
    ordine, viaggi, info = [], [], []
    for tab in b["tabs"]:
        chiavi, v = tabella_a_viaggi(tab)
        pos = -1
        for k in chiavi:
            if k in ordine:
                pos = ordine.index(k)
            else:
                pos += 1
                ordine.insert(pos, k)
        info.append("tab %d: colonne=%d, max valori in una riga=%d, righe=%d" % (
            len(info) + 1, len(v), max(len(r) for _, r in tab), len(tab)))
        viaggi += v
    viaggi = [v for v in viaggi if len(v) >= 2]
    if len(ordine) < 2 or not viaggi:
        report.append("  ! linea %s: dati insufficienti" % b["id"])
        return None
    storte, esempio = 0, None
    for v in viaggi:
        seq = [mins_(v[k]) for k in ordine if k in v]
        if any(b2 < a - 5 and a - b2 < 600 for a, b2 in zip(seq, seq[1:])):
            storte += 1
            esempio = esempio or v
    if storte:
        report.append("  ! linea %s (%s): %d/%d corse con orari non crescenti" % (b["id"], b["hdr"][:40], storte, len(viaggi)))
        if DIAG[0] < 6:
            DIAG[0] += 1
            report.append("    " + " | ".join(info))
            report.append("    esempio: " + "; ".join("%s=%s" % (k[0][:18], esempio[k]) for k in ordine if k in esempio)[:420])
    seg = [s.strip() for s in b["hdr"].split(" - ") if s.strip()]
    verso = "%s → %s" % (seg[0], seg[-1]) if len(seg) > 1 else b["hdr"]
    verso = re.sub(r"\s*\(?Orario.*$", "", verso).strip(" -")
    out = {"id": b["id"], "dir": verso, "tipo": lab,
           "f": [[k[0], [v.get(k) for v in viaggi]] for k in ordine]}
    if storte:
        out["amb"] = 1
    return out


def mins_(t):
    a, b = t.split(".")
    return int(a) * 60 + int(b)


def costruisci_orari():
    import pdfplumber
    report, linee, fonti, visti = [], [], [], set()
    for url in trova_pdf():
        try:
            with pdfplumber.open(io.BytesIO(scarica_bytes(url))) as pdf:
                testo = pdf.pages[0].extract_text() or ""
                up = testo.upper()
                if "ANCONA" not in up or not any(x in up for x in ("SERVIZIO URBANO DI ANCONA", "EXTRAURBANO", "LIBRETTO")):
                    continue
                ambito, lab = etichetta(testo, url)
                if (ambito, lab) in visti:
                    continue
                visti.add((ambito, lab))
                blocchi = parse_pdf(pdf)
                report.append("%s | %s | %s" % (ambito, lab, url))
                n0 = len(linee)
                for b in blocchi:
                    l = blocco_a_linea(b, lab, report)
                    if l:
                        l["ambito"] = ambito
                        linee.append(l)
                report.append("  linee/direzioni lette: %d" % (len(linee) - n0))
                fonti.append({"ambito": ambito, "tipo": lab, "url": url})
        except Exception as e:
            report.append("ERRORE su %s: %s" % (url, e))
    return {"generato": OGGI.strftime("%Y-%m-%d %H:%M UTC"), "fonti": fonti, "linee": linee}, report


# ====================== POSIZIONE DELLE FERMATE (approssimata) ======================
import math
import time

MAX_CHIAMATE = 600
CENTRO = (43.6158, 13.5189)
VIEWBOX = "13.0,43.85,13.9,43.35"
SIGLE = {"P.le": "Piazzale", "P.zza": "Piazza", "P.za": "Piazza", "C.so": "Corso", "V.le": "Viale",
         "S.S.": "Strada Statale", "St. Vecchia": "Strada Vecchia", "Str.": "Strada", "Ist.": "Istituto",
         "SP ": "Strada Provinciale ", "Scamb.re": "Scambiatore", "Sc.": "Scuola"}


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def pulisci_nome(n):
    n = re.sub(r"\bCapolinea\b", "", n)
    for a, b in SIGLE.items():
        n = n.replace(a, b)
    return re.sub(r"\s+", " ", n).strip(" -")


def geocodifica(nome, chiamate, esteso=False):
    parti = [pulisci_nome(x) for x in re.split(r"\s+-\s+", nome) if x.strip()]
    cand = []
    for c in [pulisci_nome(nome)] + parti[:1] + parti[-1:]:
        if c and c not in cand:
            cand.append(c)
    if esteso:
        pezzi = [pulisci_nome(x) for x in re.split(r"-", nome) if x.strip()]
        parole = re.sub(r"\b(dif|Sc|Tra|Bivio)\b.*$", "", pulisci_nome(nome)).split()
        for c in pezzi + [" ".join(parole[:k]) for k in range(len(parole) - 1, 1, -1)][:3]:
            if len(c) > 4 and c not in cand:
                cand.append(c)
    for c in cand:
        chiamate[0] += 1
        time.sleep(1.1)
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": c + (", Marche" if esteso else ", Ancona"), "format": "jsonv2", "limit": 5, "viewbox": VIEWBOX,
             "bounded": 0 if esteso else 1, "countrycodes": "it"})
        req = urllib.request.Request(url, headers={"User-Agent": "bus-ancona-personale/1.0 (github.com/zugnonicola-rgb/bus-ancona)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
        best = None
        for x in res:
            lat, lon = float(x["lat"]), float(x["lon"])
            d = km(lat, lon, *CENTRO)
            if d <= (70 if esteso else 45) and (best is None or d < best[0]):
                best = (d, lat, lon)
        if best:
            return [round(best[1], 5), round(best[2], 5)]
    return None


def token(n):
    stop = {"via", "piazza", "piazzale", "viale", "corso", "capolinea", "di", "della", "del", "dei", "le", "la", "il", "p", "zza"}
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", pulisci_nome(n).lower()).split() if t not in stop and len(t) > 1}


_OSM_CACHE = []


def fermate_osm():
    """Tutte le fermate bus di OpenStreetMap nella zona (lat, lon, nome). Scaricate una volta per esecuzione."""
    if _OSM_CACHE:
        return _OSM_CACHE
    box = "(43.40,13.05,43.80,13.85)"
    q = ('[out:json][timeout:90];('
         'node["highway"="bus_stop"]%s;way["highway"="bus_stop"]%s;'
         'node["public_transport"="platform"]%s;way["public_transport"="platform"]%s;'
         'node["public_transport"="stop_position"]["bus"="yes"]%s;);out center tags;') % ((box,) * 5)
    req = urllib.request.Request("https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(q),
                                 headers={"User-Agent": "bus-ancona-personale/1.0 (github.com/zugnonicola-rgb/bus-ancona)"})
    with urllib.request.urlopen(req, timeout=150) as r:
        d = json.loads(r.read().decode("utf-8"))
    out = []
    for e in d.get("elements", []):
        t = e.get("tags", {})
        if any(k in t for k in ("railway", "tram", "subway", "ferry")) and t.get("bus") != "yes" and t.get("highway") != "bus_stop":
            continue                                   # binari, tram, traghetti
        if t.get("public_transport") == "platform" and t.get("highway") != "bus_stop" and t.get("bus") != "yes" and "bus" in t and t["bus"] == "no":
            continue
        lat = e.get("lat") if "lat" in e else (e.get("center") or {}).get("lat")
        lon = e.get("lon") if "lon" in e else (e.get("center") or {}).get("lon")
        if lat is None:
            continue
        out.append((lat, lon, t.get("name", "") or t.get("ref", "")))
    # una fermata e' spesso mappata due volte (marciapiede + posizione): tengo una sola per ~8 metri e nome
    pul, visti = [], {}
    for lat, lon, nm in out:
        k = (round(lat * 12000), round(lon * 12000))
        if k in visti and (not nm or visti[k] == nm):
            continue
        visti[k] = nm
        pul.append((lat, lon, nm))
    _OSM_CACHE.extend(pul)
    return _OSM_CACHE


def risolvi_per_nome(nome, osm):
    """Posizione di una fermata degli orari cercandola per nome tra le fermate OpenStreetMap (piu' vicina al centro)."""
    nt = token(nome)
    if not nt:
        return None
    best = None
    for lat, lon, nm in osm:
        if not nm:
            continue
        tn = token(nm)
        inter = len(nt & tn)
        sim = inter / max(len(nt), len(tn), 1)
        if inter and (sim >= 0.6 or tn <= nt):
            sc = sim * 100 - km(lat, lon, *CENTRO)
            if best is None or sc > best[0]:
                best = (sc, lat, lon)
    return [round(best[1], 5), round(best[2], 5)] if best else None


def aggancia(c, nome, osm):
    nt, best = token(nome), None
    for lat, lon, nm in osm:
        d = km(c[0], c[1], lat, lon) * 1000
        if d > 200:
            continue
        sim = len(nt & token(nm)) / max(1, len(nt)) if nm else 0
        score = sim * 100 - d / 4
        if best is None or score > best[0]:
            best = (score, lat, lon)
    return [round(best[1], 5), round(best[2], 5)] if best else c


def aggiorna_fermate(dati, report):
    prec, manuali, falliti, agg = {}, {}, {}, set()
    for nome_file, dest in (("data.json", "prec"), ("fermate_manuali.json", "man")):
        try:
            d = json.load(open(nome_file, encoding="utf-8"))
            if dest == "prec":
                prec = d.get("fermate", {})
                falliti = d.get("falliti", {})
                agg = set(d.get("agganciate", []))
            else:
                manuali = d
        except Exception:
            pass
    nomi = sorted({st[0] for l in dati["linee"] for st in l["f"]})
    coord = {n: prec[n] for n in nomi if n in prec}
    chiamate, nuove, trovate, errori = [0], 0, 0, 0
    for n in nomi:
        if n in manuali:
            coord[n] = manuali[n]
            continue
        gia = n in coord
        if (gia and (coord[n] or falliti.get(n, 0) >= 2)) or chiamate[0] >= MAX_CHIAMATE or errori >= 5:
            continue
        try:
            coord[n] = geocodifica(n, chiamate, esteso=gia)
            if not coord[n]:
                falliti[n] = 2 if gia else 1
        except Exception as e:
            errori += 1
            report.append("  geocodifica non riuscita per '%s': %s" % (n, e))
            continue
        nuove += 1
        trovate += 1 if coord[n] else 0
    senza = [n for n in nomi if not coord.get(n) and n not in manuali]
    if senza:
        try:
            osm0 = fermate_osm()
            ris = 0
            for n in senza:
                c = risolvi_per_nome(n, osm0)
                if c:
                    coord[n] = c
                    agg.add(n)
                    ris += 1
            report.append("Fermate senza posizione risolte dal nome su OpenStreetMap: %d su %d" % (ris, len(senza)))
        except Exception as e:
            report.append("  ricerca per nome su OpenStreetMap non riuscita: %s" % e)
    da_agg = [n for n in nomi if coord.get(n) and n not in manuali and n not in agg]
    if da_agg:
        try:
            osm = fermate_osm()
            spost = []
            for n in da_agg:
                nuovo = aggancia(coord[n], n, osm)
                spost.append(km(coord[n][0], coord[n][1], nuovo[0], nuovo[1]) * 1000)
                coord[n] = nuovo
                agg.add(n)
            report.append("Agganciate a fermate OpenStreetMap: %d (fermate OSM: %d, spostamento medio %d m)" % (
                len(da_agg), len(osm), sum(spost) / max(1, len(spost))))
        except Exception as e:
            report.append("  aggancio a OpenStreetMap non riuscito (riprovo al prossimo giro): %s" % e)
    dati["agganciate"] = sorted(n for n in agg if n in nomi)
    ok = sum(1 for n in nomi if coord.get(n))
    report.append("Fermate con posizione: %d/%d (cercate ora: %d, trovate: %d)" % (ok, len(nomi), nuove, trovate))
    dati["fermate"] = {n: coord[n] for n in nomi if n in coord}
    dati["falliti"] = {n: v for n, v in falliti.items() if n in nomi and not coord.get(n)}
    senza = [n for n in nomi if not coord.get(n)]
    if senza:
        report.append("  senza posizione (%d): %s" % (len(senza), "; ".join(senza[:90])))


# ====================== PERCORSI SU STRADA (per i bus stimati) ======================
import hashlib

MAX_RICHIESTE_PERC = 260
UA_GEO = "bus-ancona-personale/1.0 (github.com/zugnonicola-rgb/bus-ancona)"


def chiave_perc(nomi):
    return hashlib.md5("\x1f".join(nomi).encode("utf-8")).hexdigest()[:10]


def dist_m(a, b):
    """a e b sono (lon, lat). Distanza approssimata in metri."""
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * 110540)


def osrm_percorso(coords):
    url = "https://router.project-osrm.org/route/v1/driving/" + ";".join("%.6f,%.6f" % c for c in coords) + \
          "?overview=full&geometries=geojson&continue_straight=true"
    req = urllib.request.Request(url, headers={"User-Agent": UA_GEO})
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read().decode("utf-8"))
    if j.get("code") != "Ok":
        raise RuntimeError("OSRM: %s" % j.get("code"))
    return j["routes"][0]["geometry"]["coordinates"]


def percorso_completo(coords, chiamate):
    """Percorso su strada passante per tutte le fermate (a blocchi di 80 punti)."""
    out = []
    for i in range(0, len(coords) - 1, 79):
        chiamate[0] += 1
        time.sleep(1.1)
        g = osrm_percorso(coords[i:i + 80])
        out += g if not out else g[1:]
    return out


def mappa_fermate(P, fermate):
    """Per ogni fermata (lon,lat) o None, indice del vertice di P piu' vicino, in ordine crescente."""
    cum = [0.0]
    for i in range(1, len(P)):
        cum.append(cum[-1] + dist_m(P[i - 1], P[i]))
    out, start, prec = [], 0, None
    for s in fermate:
        if s is None:
            out.append(None)
            continue
        lim = 1500 if prec is None else 3 * dist_m(prec, s) + 800
        best = None
        for i in range(start, len(P)):
            d = dist_m(P[i], s)
            if best is None or d < best[0]:
                best = (d, i)
            if cum[i] - cum[start] > lim:
                break
        out.append(best[1])
        start, prec = best[1], s
    return out


def semplifica(P, tieni, tol=3.0):
    """Douglas-Peucker (metri) mantenendo i vertici in 'tieni'. Ritorna (punti, mappa vecchio->nuovo indice)."""
    n = len(P)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    for i in tieni:
        keep[i] = True
    forti = [i for i in range(n) if keep[i]]
    kx = 111320 * math.cos(math.radians(P[0][1]))
    xy = [((p[0] - P[0][0]) * kx, (p[1] - P[0][1]) * 110540) for p in P]
    for a, b in zip(forti, forti[1:]):
        pila = [(a, b)]
        while pila:
            i, j = pila.pop()
            if j - i < 2:
                continue
            (x1, y1), (x2, y2) = xy[i], xy[j]
            dx, dy = x2 - x1, y2 - y1
            L = math.hypot(dx, dy) or 1e-9
            mx, mi = -1, None
            for k in range(i + 1, j):
                d = abs(dy * xy[k][0] - dx * xy[k][1] + x2 * y1 - y2 * x1) / L
                if d > mx:
                    mx, mi = d, k
            if mx > tol:
                keep[mi] = True
                pila += [(i, mi), (mi, j)]
    nuovi, mappa = [], {}
    for i in range(n):
        if keep[i]:
            mappa[i] = len(nuovi)
            nuovi.append([round(P[i][0], 5), round(P[i][1], 5)])
    return nuovi, mappa


def aggiorna_percorsi(dati, report):
    try:
        prec = json.load(open("percorsi.json", encoding="utf-8")).get("p", {})
    except Exception:
        prec = {}
    coord = dati.get("fermate", {})
    perc, usate, chiamate, errori, nuovi = {}, set(), [0], 0, 0
    for l in dati["linee"]:
        nomi = [st[0] for st in l["f"]]
        k = chiave_perc(nomi)
        l["rk"] = k
        usate.add(k)
        if k in prec:
            perc[k] = prec[k]
            continue
        if k in perc or chiamate[0] >= MAX_RICHIESTE_PERC or errori >= 5 or l.get("amb"):
            continue
        ll = [tuple(coord[n][::-1]) if coord.get(n) else None for n in nomi]
        pts = [p for i, p in enumerate(ll) if p and (i == 0 or p != ll[i - 1])]
        if len(pts) < 2:
            continue
        try:
            P = percorso_completo(pts, chiamate)
            idx = mappa_fermate(P, ll)
            nuove_p, m = semplifica(P, [i for i in idx if i is not None])
            perc[k] = {"g": nuove_p, "s": [m[i] if i is not None else None for i in idx]}
            nuovi += 1
        except Exception as e:
            errori += 1
            report.append("  percorso non riuscito per linea %s: %s" % (l["id"], e))
    json.dump({"v": 1, "p": {k: v for k, v in perc.items() if k in usate}},
              open("percorsi.json", "w", encoding="utf-8"), separators=(",", ":"))
    mancano = len({l["rk"] for l in dati["linee"] if l["rk"] not in perc and not l.get("amb")})
    report.append("Percorsi su strada: %d pronti, %d nuovi ora, %d ancora da calcolare (linee ambigue escluse)" % (len(perc), nuovi, mancano))


def aggiorna_osm_stops(report):
    """Salva tutte le fermate bus di OpenStreetMap (anche quelle assenti dagli orari PDF) per mostrarle sulla mappa."""
    try:
        v = json.load(open("fermate_osm.json", encoding="utf-8"))
        if time.time() - v.get("t", 0) < 20 * 86400 and v.get("s") and v.get("q") == 2:
            return
    except Exception:
        pass
    osm = fermate_osm()
    if not osm:
        report.append("Fermate OpenStreetMap non scaricate (riprovo al prossimo giro)")
        return
    json.dump({"t": int(time.time()), "q": 2, "s": [[round(lo, 5), round(la, 5), nm] for la, lo, nm in osm]},
              open("fermate_osm.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    report.append("Fermate OpenStreetMap salvate per la mappa: %d" % len(osm))

# ====================== AVVISI ======================
def costruisci_avvisi(report):
    av = []
    for nome, f in (("Conerobus", avvisi_conerobus), ("Scioperi", avvisi_scioperi)):
        try:
            av += f()
        except Exception as e:
            report.append("ERRORE avvisi %s: %s" % (nome, e))
    out = []
    for a in av:
        data = a.get("data")
        try:
            if data and datetime.fromisoformat(data).replace(tzinfo=timezone.utc) < OGGI - timedelta(days=45):
                continue
        except Exception:
            pass
        m = motivi_rilevanza(a)
        if not m:
            continue
        linee = [x[6:] for x in m if x.startswith("linea ")]
        if "sciopero" in m:
            linee.append("*")
        out.append({"titolo": a["titolo"], "linee": linee, "link": a["link"], "fonte": a["fonte"]})
    return out[:15]


if __name__ == "__main__":
    dati, report = costruisci_orari()
    avvisi = costruisci_avvisi(report)
    if dati["linee"]:
        aggiorna_fermate(dati, report)
        aggiorna_percorsi(dati, report)
        aggiorna_osm_stops(report)
        json.dump(dati, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    else:
        report.append("ATTENZIONE: nessun orario letto, data.json non aggiornato")
    json.dump({"generato": dati["generato"], "avvisi": avvisi}, open("avvisi.json", "w", encoding="utf-8"), ensure_ascii=False)
    report.insert(0, "Aggiornato: %s | linee: %d | avvisi: %d" % (dati["generato"], len(dati["linee"]), len(avvisi)))
    open("report.txt", "w", encoding="utf-8").write("\n".join(report))
    print("\n".join(report))
