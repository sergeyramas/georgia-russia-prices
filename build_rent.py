#!/usr/bin/env python3
# Собирает полный пул объявлений с myhome.ge (tnet API) по городам,
# для аренды и продажи, квартир и домов. Чистит спам, считает диапазоны
# и счётчики по ВСЕМУ пулу, пишет rent-data.js (window.RENT_DATA).
# Обновить сайт: python3 build_rent.py && vercel deploy --prod
import json, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

GEN_DATE = "12.07.2026"
RATE = {"gelRub": 29.07, "usdRub": 76.66, "gelUsd": 0.3801, "usdGel": 2.631}
HDR = {"User-Agent": "Mozilla/5.0", "X-Website-Key": "myhome",
       "Accept": "application/json", "Referer": "https://www.myhome.ge/", "locale": "ru"}
MAX_PAGES = 20            # potolok stranic na segment (20*20=400 lotov)
SAMPLE = 15              # skolko lotov pokazyvat v tablice na segment

CITIES = [("tbilisi", 1, "Тбилиси"), ("batumi", 15, "Батуми"),
          ("kobuleti", 94, "Кобулети"), ("poti", 91, "Поти"), ("zugdidi", 39, "Зугдиди")]
DEALS = [("rent", 2), ("sale", 1)]
TYPES = [("apt", 1), ("house", 2)]

def api(city, deal, ret, page):
    url = (f"https://api-statements.tnet.ge/v1/statements?page={page}"
           f"&deal_types={deal}&real_estate_types={ret}&cities={city}&limit=100")
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["data"]["data"]

def pull(city, deal, ret):
    seen, rows = set(), []
    for page in range(1, MAX_PAGES + 1):
        try:
            batch = api(city, deal, ret, page)
        except Exception:
            break
        if not batch:
            break
        for r in batch:
            if r["id"] not in seen:
                seen.add(r["id"]); rows.append(r)
        if len(batch) < 20:      # poslednyaya nepolnaya stranica
            break
    return rows

def keep(r, deal_slug):
    try:
        gel = r["price"]["1"]["price_total"]; usd = r["price"]["2"]["price_total"]
        area = r.get("area") or 0
        if not gel or not usd or area < 15:
            return None
        if deal_slug == "rent":
            ppm = gel / area
            if gel < 300 or ppm < 4 or ppm > 150:   # spam / posutochka-kak-mesyac
                return None
        else:  # sale
            ppm = usd / area
            if usd < 8000 or ppm < 150 or ppm > 9000:
                return None
        return {"kind": None, "title": (r.get("dynamic_title") or "").replace("Сдается ", "").replace("Продается ", "").strip(),
                "area": area, "gel": round(gel), "usd": round(usd), "nat": r.get("currency_id"),
                "url": f"https://www.myhome.ge/ru/pr/{r['id']}",
                "date": (r.get("last_updated") or "")[:10]}
    except Exception:
        return None

def pct(vals, p):
    vals = sorted(vals);
    return vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))] if vals else 0

def rng(items):
    us = [x["usd"] for x in items]
    lo, hi = pct(us, 10), pct(us, 90)
    step = 10 if hi < 3000 else 500
    return {"low": int(round(lo / step) * step), "high": int(round(hi / step) * step), "cur": "USD"}

NOTE = {
 ("tbilisi", "rent"): "Ваке/Вера дороже, спальные районы (Дигоми, Глдани) дешевле.",
 ("tbilisi", "sale"): "Вторичка и новостройки. Центр и Ваке — верх диапазона.",
 ("batumi", "rent"): "Ближе к морю и New Boulevard — дороже; горгород и Хелвачаури дешевле.",
 ("batumi", "sale"): "Много новостроек у моря; апартаменты с видом — верх рынка.",
 ("kobuleti", "rent"): "Муниципалитет Кобулети (Чакви, Цихисдзири, Очхамури). Июльский пик: долгосрочных квартир мало — почти всё в посуточной.",
 ("kobuleti", "sale"): "Курортные новостройки у моря; частный сектор дешевле.",
 ("poti", "rent"): "Портовый город, не курорт — рынок скромный, цены ниже Батуми.",
 ("poti", "sale"): "Небольшой рынок, преимущественно вторичка.",
 ("zugdidi", "rent"): "Не курорт, рынок узкий, почти всё в лари. Домов в аренду мало.",
 ("zugdidi", "sale"): "Узкий рынок Самегрело, в основном частные дома и вторичка.",
}

def segment(args):
    slug, cid, deal_slug, deal_id, kind, ret = args
    raw = pull(cid, deal_id, ret)
    items = [x for x in (keep(r, deal_slug) for r in raw) if x]
    for x in items:
        x["kind"] = kind
    items.sort(key=lambda x: x["usd"])
    sample = items[:SAMPLE]
    for x in sample:
        d = x["date"]; x["date"] = (d[8:10] + "." + d[5:7]) if d else ""
    return (slug, deal_slug, kind, {"range": rng(items), "count": len(items),
            "capped": len(raw) >= MAX_PAGES * 20, "sample": [{k: v for k, v in x.items() if k != "nat" or True} for x in sample]})

jobs = [(slug, cid, ds, di, kind, ret) for slug, cid, name in CITIES
        for ds, di in DEALS for kind, ret in TYPES]
with ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(segment, jobs))

DATA = {}
name_of = {slug: name for slug, cid, name in CITIES}
for slug, cid, name in CITIES:
    DATA[slug] = {"name": name}
    for ds, _ in DEALS:
        DATA[slug][ds] = {"apt": {}, "house": {}, "listings": [], "note": NOTE.get((slug, ds), "")}
for slug, ds, kind, seg in results:
    node = DATA[slug][ds]
    node[kind] = {**seg["range"], "count": seg["count"], "capped": seg["capped"],
                  "size": "1–3 комнаты" if kind == "apt" else "3+ комнаты / коттедж"}
    node["listings"] += seg["sample"]

total = sum(DATA[s][d][k]["count"] for s in DATA for d, _ in DEALS for k in ("apt", "house"))
out = {"meta": {"date": GEN_DATE, "rate": RATE, "total": total, "order": [s for s, _, _ in CITIES]},
       "cities": DATA}
with open("rent-data.js", "w", encoding="utf-8") as f:
    f.write("window.RENT_DATA = " + json.dumps(out, ensure_ascii=False) + ";")

print(f"generated rent-data.js — total cleaned lots: {total}")
for slug, cid, name in CITIES:
    for ds, _ in DEALS:
        a, h = DATA[slug][ds]["apt"], DATA[slug][ds]["house"]
        print(f"  {name:9} {ds:4} apt ${a['low']}-{a['high']} (n={a['count']}{'+' if a['capped'] else ''})  "
              f"house ${h['low']}-{h['high']} (n={h['count']}{'+' if h['capped'] else ''})")
