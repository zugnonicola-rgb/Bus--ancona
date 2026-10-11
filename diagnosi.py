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


def overpass(q):
    for base in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            req = urllib.request.Request(base + "?data=" + urllib.parse.quote(q),
                                         headers={"User-Agent": "bus-ancona-personale/1.0 (github.com/zugnonicola-rgb/bus-ancona)"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            ultimo = e
    raise RuntimeError(ultimo)


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
    R.append("OPENSTREETMAP: %d relazioni di linee bus nell'area" % len(rel))
    ops = collections.Counter((r["tags"].get("operator") or r["tags"].get("network") or "?") for r in rel)
    R.append("  operatori/reti: " + "; ".join("%s=%d" % (k, v) for k, v in ops.most_common(8)))
    refs = collections.Counter(r["tags"].get("ref", "?") for r in rel)
    R.append("  numeri di linea presenti (%d): %s" % (len(refs), ", ".join(sorted(refs)[:60])))
    comuni = sorted(x for x in ids if x in refs)
    R.append("  linee degli orari ritrovate su OSM: %d su %d  -> %s" % (len(comuni), len(ids), ", ".join(comuni)))
    R.append("  linee degli orari NON trovate su OSM: %s" % ", ".join(sorted(ids - set(refs))[:40]))
    try:
        d2 = overpass('[out:json][timeout:120];relation["type"="route"]["route"="bus"](43.40,13.05,43.80,13.85);'
                      'out body;')
        con_fermate = sum(1 for r in d2["elements"] if any(m.get("role", "").startswith(("platform", "stop")) for m in r.get("members", [])))
        con_vie = sum(1 for r in d2["elements"] if any(m.get("type") == "way" and m.get("role", "") in ("", "route") for m in r.get("members", [])))
        R.append("  relazioni con fermate collegate: %d | con tratti di strada: %d (servono per un tracciato esatto)" % (con_fermate, con_vie))
    except Exception as e:
        R.append("  dettaglio relazioni non letto (%s)" % e)


if __name__ == "__main__":
    for fase in (qualita_tracciati, copertura_osm):
        try:
            fase()
        except Exception as e:
            R.append("ERRORE in %s: %s" % (fase.__name__, e))
    testo = "\n".join(R)
    open("diagnosi.txt", "w", encoding="utf-8").write(testo)
    print(testo)
