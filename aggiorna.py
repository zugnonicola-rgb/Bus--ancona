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
    return {"id": b["id"], "dir": verso, "tipo": lab,
            "f": [[k[0], [v.get(k) for v in viaggi]] for k in ordine]}


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
        json.dump(dati, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    else:
        report.append("ATTENZIONE: nessun orario letto, data.json non aggiornato")
    json.dump({"generato": dati["generato"], "avvisi": avvisi}, open("avvisi.json", "w", encoding="utf-8"), ensure_ascii=False)
    report.insert(0, "Aggiornato: %s | linee: %d | avvisi: %d" % (dati["generato"], len(dati["linee"]), len(avvisi)))
    open("report.txt", "w", encoding="utf-8").write("\n".join(report))
    print("\n".join(report))
