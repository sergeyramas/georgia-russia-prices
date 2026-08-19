#!/usr/bin/env python3
# Собирает полный пул объявлений с ДВУХ источников — myhome.ge (tnet API) и
# ss.ge (LegendSearch API, анонимный OAuth) — по городам, для аренды и продажи,
# квартир и домов. Чистит спам, помечает НОВЫЕ объявления (которых не было в
# прошлый прогон), считает диапазоны/счётчики, пишет rent-data.js.
# Обновить сайт: python3 build_rent.py && vercel deploy --prod
import json, os, re, urllib.request, urllib.parse, urllib.error
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

TODAY = date.today()
GEN_DATE = TODAY.strftime("%d.%m.%Y")
RATE = {"gelRub": 32.50, "usdRub": 85.01, "gelUsd": 0.3822, "usdGel": 2.6162}
MAX_PAGES = 20
SAMPLE = 18            # сколько лотов показывать на сегмент
NEW_SLOTS = 10         # сколько мест в выборке резервируем под новые
SEEN_FILE = "seen-listings.json"
DESC_FILE = "desc-ru.json"        # кэш переводов грузинских описаний
PENDING_FILE = "desc-pending.json"  # что осталось перевести
DESC_MAX = 700
PRUNE_DAYS = 90

GE_RE = re.compile(r"[\u10A0-\u10FF]")
TAG_RE = re.compile(r"<[^>]+>")

def clean_desc(t):
    """HTML → плоский текст, схлопнутые пробелы, обрезка."""
    if not t:
        return ""
    t = TAG_RE.sub(" ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:DESC_MAX].rstrip() + ("…" if len(t) > DESC_MAX else "")

def is_georgian(t):
    return bool(t) and len(GE_RE.findall(t)) > len(t) * 0.15

DESC_RU = {}
if os.path.exists(DESC_FILE):
    try:
        DESC_RU = json.load(open(DESC_FILE, encoding="utf-8"))
    except Exception:
        DESC_RU = {}

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

MY_CITY = {"tbilisi": 1, "batumi": 15, "kobuleti": 94, "poti": 91, "zugdidi": 39}
MY_DEAL = {"rent": 2, "sale": 1}
MY_TYPE = {"apt": 1, "house": 2}
SS_CITY = {"tbilisi": 95, "batumi": 96, "kobuleti": 14, "poti": 101, "zugdidi": 100}
SS_DEAL = {"rent": 1, "sale": 4}
SS_TYPE = {"apt": 5, "house": 4}

# ---------- память о виденных объявлениях ----------
SEEN = {}
FIRST_RUN = not os.path.exists(SEEN_FILE)
if not FIRST_RUN:
    try:
        SEEN = json.load(open(SEEN_FILE, encoding="utf-8"))
    except Exception:
        SEEN, FIRST_RUN = {}, True

def is_new(item):
    """НОВОЕ = URL не встречался в прошлых прогонах. Осмысленно только когда история есть."""
    return (not FIRST_RUN) and item["url"] not in SEEN

def is_fresh(item):
    """СВЕЖЕЕ = объявление размещено или поднято за последние двое суток."""
    d = item.get("date") or ""
    if len(d) >= 10:
        try:
            return date.fromisoformat(d[:10]) >= TODAY - timedelta(days=2)
        except Exception:
            return False
    return False

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
                "url": f"https://www.myhome.ge/ru/pr/{r['id']}", "date": (r.get("last_updated") or "")[:10],
                "desc": clean_desc(r.get("comment"))})
        if len(batch) < 20:
            break
    return out

# ---------- ss.ge ----------
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
                "url": f"https://home.ss.ge/ru/real-estate/{aid}", "date": (r.get("orderDate") or "")[:10],
                "desc": clean_desc(r.get("description"))})
        if len(batch) < 30:
            break
    return out

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
 ("kobuleti", "rent"): "Муниципалитет Кобулети (Чакви, Цихисдзири, Очхамури). Сезон идёт на спад — долгосрочных предложений становится больше.",
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
    for x in items:
        x["new"] = 1 if is_new(x) else 0
        x["fresh"] = 1 if is_fresh(x) else 0
    items.sort(key=lambda x: x["usd"])
    # в выборку гарантированно попадают новые (до NEW_SLOTS), остальное — самые дешёвые
    prio = [x for x in items if x["new"] or x["fresh"]]
    prio.sort(key=lambda x: (-x["new"], -x["fresh"], x["usd"]))
    prio = prio[:NEW_SLOTS]
    rest = [x for x in items if x not in prio][:max(0, SAMPLE - len(prio))]
    sample = sorted(prio + rest, key=lambda x: (-x["new"], -x["fresh"], x["usd"]))
    for x in sample:
        d = x["date"]; x["date"] = (d[8:10] + "." + d[5:7]) if d and len(d) >= 10 else ""
        desc = x.get("desc") or ""
        if is_georgian(desc):
            ru = DESC_RU.get(x["url"])
            if ru:
                x["desc"], x["tr"] = ru, 1      # переведено
            else:
                x["desc"], x["need_tr"] = desc, 1
    by_src = {}
    for x in items:
        by_src[x["src"]] = by_src.get(x["src"], 0) + 1
    return (city, deal, kind, {**rng(items), "count": len(items), "capped": capped,
            "new_count": sum(x["new"] for x in items), "fresh_count": sum(x["fresh"] for x in items), "by_src": by_src,
            "size": "1–3 комнаты" if kind == "apt" else "3+ комнаты / коттедж",
            "sample": sample, "_urls": [x["url"] for x in items]})

jobs = [(c, d, k) for c, _ in CITIES for d in DEALS for k in TYPES]
with ThreadPoolExecutor(max_workers=5) as ex:
    results = list(ex.map(segment, jobs))

DATA = {c: {"name": n} for c, n in CITIES}
for c, n in CITIES:
    for d in DEALS:
        DATA[c][d] = {"apt": {}, "house": {}, "listings": [], "note": NOTE.get((c, d), "")}
all_urls = []
for city, deal, kind, seg in results:
    all_urls += seg.pop("_urls")
    node = DATA[city][deal]
    node[kind] = {k: v for k, v in seg.items() if k != "sample"}
    node["listings"] += seg["sample"]

# API иногда частично отдаёт сегмент — берём из прошлого прогона той же даты более полный
try:
    prev_raw = open("rent-data.js", encoding="utf-8").read()
    prev = json.loads(prev_raw[prev_raw.index("{"):prev_raw.rindex("}") + 1])
    if prev.get("meta", {}).get("date") == GEN_DATE:
        for c in DATA:
            for d in DEALS:
                keep = []
                for k in TYPES:
                    if prev["cities"][c][d][k].get("count", 0) > DATA[c][d][k]["count"]:
                        DATA[c][d][k] = prev["cities"][c][d][k]
                        keep += [x for x in prev["cities"][c][d]["listings"] if x["kind"] == k]
                    else:
                        keep += [x for x in DATA[c][d]["listings"] if x["kind"] == k]
                DATA[c][d]["listings"] = keep
        print("(слит с прошлым прогоном той же даты — взяты более полные сегменты)")
except Exception:
    pass

pending = {}
for c in DATA:
    for d in DEALS:
        for x in DATA[c][d]["listings"]:
            if x.get("need_tr"):
                pending[x["url"]] = {"city": c, "deal": d, "text": x["desc"]}
json.dump(pending, open(PENDING_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# непереведённый грузинский текст в публичный файл не кладём — только в pending
for c in DATA:
    for d in DEALS:
        for x in DATA[c][d]["listings"]:
            if x.pop("need_tr", None):
                x.pop("desc", None)

total = sum(DATA[c][d][k]["count"] for c in DATA for d in DEALS for k in TYPES)
new_total = sum(DATA[c][d][k].get("new_count", 0) for c in DATA for d in DEALS for k in TYPES)
fresh_total = sum(DATA[c][d][k].get("fresh_count", 0) for c in DATA for d in DEALS for k in TYPES)
out = {"meta": {"date": GEN_DATE, "rate": RATE, "total": total, "new_total": new_total, "fresh_total": fresh_total,
                "first_run": FIRST_RUN, "sources": ["myhome.ge", "ss.ge"],
                "order": [c for c, _ in CITIES]},
       "cities": DATA}
with open("rent-data.js", "w", encoding="utf-8") as f:
    f.write("window.RENT_DATA = " + json.dumps(out, ensure_ascii=False) + ";")

# обновляем память: новые URL с сегодняшней датой, старьё чистим
iso = TODAY.isoformat()
cutoff = (TODAY - timedelta(days=PRUNE_DAYS)).isoformat()
for u in all_urls:
    SEEN.setdefault(u, iso)
SEEN = {u: d for u, d in SEEN.items() if d >= cutoff}
json.dump(SEEN, open(SEEN_FILE, "w", encoding="utf-8"))

print(f"описаний без перевода: {len(pending)} -> {PENDING_FILE}")
print(f"lots: {total} | новых: {new_total}{' (первый прогон — по дате объявления)' if FIRST_RUN else ''} | в памяти URL: {len(SEEN)}")
for c, n in CITIES:
    for d in DEALS:
        a, h = DATA[c][d]["apt"], DATA[c][d]["house"]
        print(f"  {n:9} {d:4} кв {a['count']}{'+' if a['capped'] else ''} (нов {a.get('new_count',0)}/свеж {a.get('fresh_count',0)}) ${a['low']}-{a['high']}"
              f"   дом {h['count']}{'+' if h['capped'] else ''} (нов {h.get('new_count',0)}/свеж {h.get('fresh_count',0)}) ${h['low']}-{h['high']}")
