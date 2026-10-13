#!/usr/bin/env python3
"""Diagnosi dei dati: i tracciati dei bus sono affidabili? OpenStreetMap ha le linee di Ancona?
Scrive diagnosi.txt (da incollare in chat)."""
import collections
import json
import math
import statistics
import urllib.parse
import urllib.request

R = []


def dist_m(a, b):  # a, b = (lon, lat)
    kx = 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * 110540)


def angolo(a, b, c):
    """Cambio di direzione in gradi nel vertice b (180 = inversione a U)."""
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n == 0:
        return 0
    cs = max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cs))


def qualita_tracciati():
    try:
        data = json.load(open("data.json", encoding="utf-8"))
        perc = json.load(open("percorsi.json", encoding="utf-8"))["p"]
    except Exception as e:
        R.append("TRACCIATI: file non leggibili (%s)" % e)
        return
    coord = data.get("fermate", {})
    visti, righe = set(), []
    for l in data["linee"]:
        k = l.get("rk")
        if not k or k in visti or k not in perc:
            continue
        visti.add(k)
        g, s = perc[k]["g"], perc[k]["s"]
        cum = [0.0]
        for i in range(1, len(g)):
            cum.append(cum[-1] + dist_m(g[i - 1], g[i]))
        # 1) tratti tra fermate molto piu' lunghi della distanza in linea d'aria
        dev, ratios = 0, []
        for q in range(1, len(s)):
            a, b = s[q - 1], s[q]
            if a is None or b is None or b <= a or b >= len(g):
                continue
            retto = dist_m(g[a], g[b])
            lungo = cum[b] - cum[a]
            if retto > 80:
                ratios.append(lungo / retto)
                if lungo / retto > 2.2 and lungo - retto > 400:
                    dev += 1
        # 2) inversioni a U nel tracciato (code/spuntoni)
        spunti = sum(1 for i in range(1, len(g) - 1) if angolo(g[i - 1], g[i], g[i + 1]) > 150)
        # 3) distanza delle fermate dal tracciato (punti del tracciato piu' vicini)
        off = []
        for q, nm in enumerate(st[0] for st in l["f"]):
            c = coord.get(nm)
            if c and q < len(s) and s[q] is not None and s[q] < len(g):
                off.append(dist_m((c[1], c[0]), g[s[q]]))
        righe.append((l["id"], l["dir"], len(s), cum[-1] / 1000, statistics.mean(ratios) if ratios else 0, dev, spunti,
                      max(off) if off else 0))
    if not righe:
        R.append("TRACCIATI: nessun tracciato calcolato")
        return
    ok = [r for r in righe if r[5] == 0 and r[6] == 0 and r[7] < 120]
    R.append("TRACCIATI: %d tracciati unici; affidabili (nessuna deviazione, nessuna inversione, fermate <120 m dal tracciato): %d (%d%%)"
             % (len(righe), len(ok), 100 * len(ok) // len(righe)))
    R.append("  rapporto medio lunghezza/linea d'aria tra fermate: %.2f (1.0-1.4 e' normale)" % statistics.mean(r[4] for r in righe if r[4]))
    R.append("  con tratti deviati: %d | con inversioni a U: %d | fermata >120 m dal tracciato: %d"
             % (sum(1 for r in righe if r[5]), sum(1 for r in righe if r[6]), sum(1 for r in righe if r[7] >= 120)))
    peggiori = sorted(righe, key=lambda r: (r[5] * 3 + r[6] + r[7] / 100), reverse=True)[:8]
    R.append("  I peggiori (linea | direzione | fermate | km | rapporto | tratti deviati | inversioni | scarto max m):")
    for r in peggiori:
        R.append("   %s | %s | %d | %.1f | %.2f | %d | %d | %.0f" % (r[0], r[1][:34], r[2], r[3], r[4], r[5], r[6], r[7]))


def overpass(q, tentativi=3):
    import time
    ultimo = None
    for t in range(tentativi):
        for base in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                     "https://overpass.private.coffee/api/interpreter"):
            try:
                req = urllib.request.Request(base + "?data=" + urllib.parse.quote(q),
                                             headers={"User-Agent": "bus-ancona-personale/1.0 (github.com/zugnonicola-rgb/bus-ancona)"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                ultimo = e
        time.sleep(6 * (t + 1))
    raise RuntimeError(ultimo)


def lungh(punti):
    return sum(dist_m((punti[i - 1]["lon"], punti[i - 1]["lat"]), (punti[i]["lon"], punti[i]["lat"])) for i in range(1, len(punti)))


def analizza_relazione(r):
    """Mette in fila i tratti di strada della relazione e conta le interruzioni."""
    vie = [m for m in r.get("members", []) if m.get("type") == "way" and m.get("geometry") and m.get("role", "") in ("", "route", "forward", "backward")]
    fermate = [m for m in r.get("members", []) if m.get("role", "").startswith(("platform", "stop"))]
    rotture, km, ult = 0, 0.0, None
    for w in vie:
        g = w["geometry"]
        km += lungh(g) / 1000
        a, b = (g[0]["lon"], g[0]["lat"]), (g[-1]["lon"], g[-1]["lat"])
        if ult is not None and min(dist_m(ult, a), dist_m(ult, b)) > 5:
            rotture += 1
        ult = b if (ult is None or dist_m(ult, a) <= dist_m(ult, b)) else a
    return len(vie), rotture, km, len(fermate)


def copertura_osm():
    try:
        ids = {l["id"] for l in json.load(open("data.json", encoding="utf-8"))["linee"]}
    except Exception:
        ids = set()
    try:
        d = overpass('[out:json][timeout:120];relation["type"="route"]["route"="bus"](43.40,13.05,43.80,13.85);out tags;')
    except Exception as e:
        R.append("OPENSTREETMAP: non raggiungibile (%s)" % e)
        return
    rel = d.get("elements", [])
    cone = [r for r in rel if "conerobus" in (r["tags"].get("operator", "") + r["tags"].get("network", "")).lower()]
    R.append("OPENSTREETMAP: %d relazioni bus nell'area, di cui Conerobus %d" % (len(rel), len(cone)))
    refs = collections.Counter(r["tags"].get("ref", "?") for r in cone)
    R.append("  linee degli orari ritrovate (per numero): %s" % ", ".join(sorted(x for x in ids if x in refs)))
    mancanti = sorted(ids - set(refs))
    R.append("  NON trovate per numero: %s" % ", ".join(mancanti))
    # cerca le mancanti nel NOME delle relazioni (es. numero scritto diversamente)
    for m in mancanti:
        trovate = [r["tags"].get("name", "")[:45] + " [ref=" + r["tags"].get("ref", "-") + "]" for r in cone
                   if m and (" " + m + " ") in (" " + r["tags"].get("name", "").replace(":", " ") + " ")]
        if trovate:
            R.append("    '%s' compare nel nome di: %s" % (m, " | ".join(trovate[:2])))
    # dettaglio: strade collegate, continuita', fermate (a gruppi, con geometria)
    righe = []
    idl = [r["id"] for r in cone]
    for i in range(0, len(idl), 6):
        try:
            dd = overpass("[out:json][timeout:180];rel(id:%s);out geom;" % ",".join(map(str, idl[i:i + 6])))
        except Exception as e:
            R.append("  gruppo %d..%d non letto (%s)" % (i, i + 6, e))
            continue
        for r in dd.get("elements", []):
            nv, rot, km, nf = analizza_relazione(r)
            t = r["tags"]
            righe.append((t.get("ref", "?"), (t.get("from", "") + " > " + t.get("to", ""))[:42] or t.get("name", "")[:42], nv, rot, km, nf,
                          t.get("public_transport:version", "-")))
    if righe:
        buone = [x for x in righe if x[2] > 0 and x[3] == 0]
        R.append("  DETTAGLIO: %d relazioni lette; con strade collegate e SENZA interruzioni: %d; con fermate collegate: %d"
                 % (len(righe), len(buone), sum(1 for x in righe if x[5] > 0)))
        R.append("  ref | da > a | tratti strada | interruzioni | km | fermate | versione PT")
        for x in sorted(righe, key=lambda z: z[0]):
            R.append("   %s | %s | %d | %d | %.1f | %d | %s" % x)


if __name__ == "__main__":
    for fase in (qualita_tracciati, copertura_osm):
        try:
            fase()
        except Exception as e:
            R.append("ERRORE in %s: %s" % (fase.__name__, e))
    testo = "\n".join(R)
    open("diagnosi.txt", "w", encoding="utf-8").write(testo)
    print(testo)
