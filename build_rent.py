#!/usr/bin/env python3
# Собирает полный пул объявлений с ДВУХ источников — myhome.ge (tnet API) и
# ss.ge (LegendSearch API, анонимный OAuth) — по городам, для аренды и продажи,
# квартир и домов. Чистит спам, считает диапазоны/счётчики по объединённому пулу,
# пишет rent-data.js (window.RENT_DATA).
# Обновить сайт: python3 build_rent.py && vercel deploy --prod
import json, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

GEN_DATE = "18.08.2026"
RATE = {"gelRub": 32.50, "usdRub": 85.01, "gelUsd": 0.3822, "usdGel": 2.6162}
MAX_PAGES = 20
SAMPLE = 16

def fetch(req):
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        except Exception:
            if attempt == 2:
                raise
    return None

CITIES = [("tbilisi", "Тбилиси"), ("batumi", "Батуми"),
          ("kobuleti", "Кобулети"), ("poti", "Поти"), ("zugdidi", "Зугдиди")]
DEALS = ["rent", "sale"]
TYPES = ["apt", "house"]

# --- source config: (city ids), (deal codes), (type codes) ---
MY_CITY = {"tbilisi": 1, "batumi": 15, "kobuleti": 94, "poti": 91, "zugdidi": 39}
MY_DEAL = {"rent": 2, "sale": 1}
MY_TYPE = {"apt": 1, "house": 2}
SS_CITY = {"tbilisi": 95, "batumi": 96, "kobuleti": 14, "poti": 101, "zugdidi": 100}
SS_DEAL = {"rent": 1, "sale": 4}
SS_TYPE = {"apt": 5, "house": 4}

# ---------- myhome.ge (tnet) ----------
MY_HDR = {"User-Agent": "Mozilla/5.0", "X-Website-Key": "myhome",
          "Accept": "application/json", "Referer": "https://www.myhome.ge/", "locale": "ru"}

def my_pull(city, deal, kind):
    seen, out = set(), []
    for page in range(1, MAX_PAGES + 1):
        url = (f"https://api-statements.tnet.ge/v1/statements?page={page}"
               f"&deal_types={MY_DEAL[deal]}&real_estate_types={MY_TYPE[kind]}&cities={MY_CITY[city]}&limit=100")
        try:
            batch = fetch(urllib.request.Request(url, headers=MY_HDR))["data"]["data"]
        except Exception:
            break
        if not batch:
            break
        for r in batch:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            try:
                gel = r["price"]["1"]["price_total"]; usd = r["price"]["2"]["price_total"]
            except Exception:
                continue
            out.append({"kind": kind, "src": "myhome.ge",
                "title": (r.get("dynamic_title") or "").replace("Сдается ", "").replace("Продается ", "").strip(),
                "area": r.get("area") or 0, "gel": gel, "usd": usd, "nat": r.get("currency_id"),
                "url": f"https://www.myhome.ge/ru/pr/{r['id']}", "date": (r.get("last_updated") or "")[:10]})
        if len(batch) < 20:
            break
    return out

# ---------- ss.ge (LegendSearch, anon OAuth) ----------
def ss_token():
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": "ssweb",
                                   "client_secret": "t5w42KQQjowNRYkycrrX"}).encode()
    req = urllib.request.Request("https://account.ss.ge/connect/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())["access_token"]

def ss_pull(city, deal, kind, token):
    H = {"Authorization": "Bearer " + token, "User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    seen, out = set(), []
    for page in range(1, MAX_PAGES + 1):
        payload = {"realEstateDealType": SS_DEAL[deal], "realEstateType": SS_TYPE[kind],
                   "cityIdList": [SS_CITY[city]], "page": page, "pageSize": 30}
        try:
            d = fetch(urllib.request.Request(
                "https://api-gateway.ss.ge/v1/RealEstate/LegendSearch", data=json.dumps(payload).encode(), headers=H))
            batch = d.get("realStateItemModel") or []
        except Exception:
            break
        if not batch:
            break
        for r in batch:
            aid = r.get("applicationId")
            if aid in seen:
                continue
            seen.add(aid)
            pr = r.get("price") or {}
            out.append({"kind": kind, "src": "ss.ge",
                "title": ("Квартира" if kind == "apt" else "Частный дом") + (f", {r.get('numberOfBedrooms')} сп." if r.get("numberOfBedrooms") else ""),
                "area": r.get("totalArea") or 0, "gel": pr.get("priceGeo") or 0, "usd": pr.get("priceUsd") or 0,
                "nat": 1 if pr.get("currencyType") == 1 else 2,
                "url": f"https://home.ss.ge/ru/real-estate/{aid}", "date": (r.get("orderDate") or "")[:10]})
        if len(batch) < 30:
            break
    return out

# ---------- shared spam filter ----------
def clean(items, deal):
    out = []
    for x in items:
        gel, usd, area = x["gel"], x["usd"], x["area"]
        if not gel or not usd or not area or area < 15:
            continue
        if deal == "rent":
            ppm = gel / area
            if gel < 300 or ppm < 4 or ppm > 150:
                continue
        else:
            ppm = usd / area
            if usd < 8000 or ppm < 150 or ppm > 9000:
                continue
        out.append({**x, "gel": round(gel), "usd": round(usd)})
    return out

def pct(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))] if vals else 0

def rng(items):
    us = [x["usd"] for x in items]
    lo, hi = pct(us, 10), pct(us, 90)
    step = 10 if hi < 3000 else 500
    return {"low": int(round(lo / step) * step), "high": int(round(hi / step) * step), "cur": "USD"}

NOTE = {
 ("kobuleti", "rent"): "Муниципалитет Кобулети (Чакви, Цихисдзири, Очхамури). Разгар сезона: долгосрочных квартир мало — почти всё в посуточной.",
 ("zugdidi", "rent"): "Не курорт, рынок узкий, почти всё в лари. Домов в аренду мало.",
 ("poti", "rent"): "Портовый город, не курорт — рынок скромный, цены ниже Батуми.",
}

TOKEN = ss_token()

def segment(job):
    city, deal, kind = job
    my = my_pull(city, deal, kind)
    ss = ss_pull(city, deal, kind, TOKEN)
    capped = len(my) >= MAX_PAGES * 20 or len(ss) >= MAX_PAGES * 30
    items = clean(my + ss, deal)
    items.sort(key=lambda x: x["usd"])
    sample = items[:SAMPLE]
    for x in sample:
        d = x["date"]; x["date"] = (d[8:10] + "." + d[5:7]) if d and len(d) >= 10 else ""
    by_src = {}
    for x in items:
        by_src[x["src"]] = by_src.get(x["src"], 0) + 1
    return (city, deal, kind, {**rng(items), "count": len(items), "capped": capped,
            "by_src": by_src, "size": "1–3 комнаты" if kind == "apt" else "3+ комнаты / коттедж",
            "sample": sample})

jobs = [(c, d, k) for c, _ in CITIES for d in DEALS for k in TYPES]
with ThreadPoolExecutor(max_workers=5) as ex:
    results = list(ex.map(segment, jobs))

DATA = {c: {"name": n} for c, n in CITIES}
for c, n in CITIES:
    for d in DEALS:
        DATA[c][d] = {"apt": {}, "house": {}, "listings": [], "note": NOTE.get((c, d), "")}
for city, deal, kind, seg in results:
    node = DATA[city][deal]
    node[kind] = {k: v for k, v in seg.items() if k != "sample"}
    node["listings"] += seg["sample"]

# API myhome/ss.ge иногда частично отдаёт сегмент (флейк под нагрузкой).
# Берём из прошлого прогона тот сегмент, где лотов было больше — данные того же дня, просто полнее.
try:
    prev_raw = open("rent-data.js", encoding="utf-8").read()
    prev = json.loads(prev_raw[prev_raw.index("{"):prev_raw.rindex("}") + 1])
    if prev.get("meta", {}).get("date") == GEN_DATE:
        for c in DATA:
            for d in DEALS:
                keep = []
                for k in TYPES:
                    old_seg = prev["cities"][c][d][k]
                    if old_seg.get("count", 0) > DATA[c][d][k]["count"]:
                        DATA[c][d][k] = old_seg
                        keep += [x for x in prev["cities"][c][d]["listings"] if x["kind"] == k]
                    else:
                        keep += [x for x in DATA[c][d]["listings"] if x["kind"] == k]
                DATA[c][d]["listings"] = keep
        print("(слит с прошлым прогоном той же даты — взяты более полные сегменты)")
except Exception:
    pass

total = sum(DATA[c][d][k]["count"] for c in DATA for d in DEALS for k in TYPES)
out = {"meta": {"date": GEN_DATE, "rate": RATE, "total": total,
                "sources": ["myhome.ge", "ss.ge"], "order": [c for c, _ in CITIES]},
       "cities": DATA}
with open("rent-data.js", "w", encoding="utf-8") as f:
    f.write("window.RENT_DATA = " + json.dumps(out, ensure_ascii=False) + ";")

print(f"generated rent-data.js — total cleaned lots (myhome+ss.ge): {total}")
for c, n in CITIES:
    for d in DEALS:
        a, h = DATA[c][d]["apt"], DATA[c][d]["house"]
        print(f"  {n:9} {d:4} apt ${a['low']}-{a['high']} (n={a['count']}{'+' if a['capped'] else ''} {a['by_src']})  "
              f"house ${h['low']}-{h['high']} (n={h['count']}{'+' if h['capped'] else ''})")
