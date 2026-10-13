#!/usr/bin/env python3
"""
Генератор фіду для каталогу реклами Meta (тип "vehicle_offer") з каталогів automoto.ua.

Що робить:
  1. Читає посилання на каталоги з catalogs.txt (по одному в рядку).
  2. Збирає з кожного каталогу посилання на оголошення (з пагінацією).
  3. Відкриває кожне оголошення і витягує дані (марка, модель, рік, ціна, фото ...).
  4. Записує docs/catalog_vehicle_offer.csv та docs/catalog_vehicle_offer.xml
     у форматі твого шаблону Meta.

Запуск:   python build_feed.py
Діагностика (збереже HTML перших сторінок каталогів): python build_feed.py --debug
"""
import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# ───────────────────────── НАЛАШТУВАННЯ ─────────────────────────
FEED_TITLE = "AutoMoto.ua vehicle offers"
PRICE_CURRENCY = "USD"          # "USD" або "UAH" - у якій валюті йде ціна у фіді
MAX_LISTINGS_PER_CATALOG = 300  # ліміт оголошень з одного каталогу
MAX_PAGES_PER_CATALOG = 30      # ліміт сторінок пагінації з одного каталогу
PAGE_PARAM = "page"             # як сайт позначає номер сторінки: ?page=2
REQUEST_DELAY = 0.7             # пауза між запитами до сайту, сек
DETAIL_REFRESH_DAYS = 1         # як часто перезавантажувати сторінку оголошення (ціна, наявність)
STALE_CACHE_MAX_DAYS = 7        # якщо сайт не відповів - скільки днів можна брати старі дані з кешу
IMAGE_TAG = "Exterior"          # обов'язкове поле image[0].tag[0]
OFFER_TYPE = "cash"             # як у твоєму шаблоні
UTM = {                         # додається до посилань на оголошення; порожній {} = без UTM
    "utm_source": "facebook",
    "utm_medium": "paid_social",
    "utm_campaign": "meta_catalog",
}
USER_AGENT = "Mozilla/5.0 (compatible; AutoMotoFeedBot/1.0; +https://automoto.ua)"
# Захист: якщо оголошень стало менше ніж X від минулого запуску - фід НЕ перезаписується
MIN_RATIO_VS_PREVIOUS = 0.5
# ────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
TEMPLATE_CSV = ROOT / "templates" / "catalog_vehicle_offer.csv"
OUT_DIR = ROOT / "docs"
CACHE_FILE = ROOT / "data" / "cache.json"
STATE_FILE = ROOT / "data" / "state.json"
DEBUG_DIR = ROOT / "debug"

LISTING_PATH_RE = re.compile(r"^/(?:uk/)?[^/]+-(\d{6,})\.html$")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"})


def log(*a):
    print(*a, flush=True)


def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def to_int(s):
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


# ───────────────────────── мережа ─────────────────────────
def fetch(url):
    """Повертає (status, text). status=None - мережева помилка."""
    status = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30)
            status = r.status_code
            if status == 200:
                return status, r.text
            if status in (404, 410):
                return status, None
        except requests.RequestException as e:
            log(f"  ! {url}: {e}")
        time.sleep(2 * (attempt + 1))
    return status, None


# ───────────────────────── каталог → список оголошень ─────────────────────────
def with_page(url, n):
    if n == 1:
        return url
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != PAGE_PARAM] + [(PAGE_PARAM, str(n))]
    return urlunparse(p._replace(query=urlencode(q)))


def extract_listing_links(html, base_url):
    """Усі посилання виду https://automoto.ua/uk/<slug>-<ID>.html (без новин/оглядів)."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"].strip())
        p = urlparse(full)
        if not p.netloc.endswith("automoto.ua"):
            continue
        m = LISTING_PATH_RE.match(p.path)
        if m:
            found.append((m.group(1), f"{p.scheme}://{p.netloc}{p.path}"))
    return found


def collect_catalog(url, debug=False):
    """Повертає впорядкований список (id, url) оголошень з каталогу."""
    ordered, seen = [], set()
    for page in range(1, MAX_PAGES_PER_CATALOG + 1):
        page_url = with_page(url, page)
        status, html = fetch(page_url)
        time.sleep(REQUEST_DELAY)
        if not html:
            log(f"  сторінка {page}: немає відповіді (status={status}), зупиняюсь")
            break
        if debug and page <= 2:
            DEBUG_DIR.mkdir(exist_ok=True)
            name = re.sub(r"[^\w]+", "_", urlparse(url).path).strip("_") or "root"
            (DEBUG_DIR / f"{name}_page{page}.html").write_text(html, encoding="utf-8")
        new = 0
        for lid, lurl in extract_listing_links(html, page_url):
            if lid not in seen:
                seen.add(lid)
                ordered.append((lid, lurl))
                new += 1
        log(f"  сторінка {page}: знайдено нових оголошень - {new}")
        if new == 0 or len(ordered) >= MAX_LISTINGS_PER_CATALOG:
            break
    return ordered[:MAX_LISTINGS_PER_CATALOG]


# ───────────────────────── сторінка оголошення → дані ─────────────────────────
FUEL_RULES = [  # (ключове слово у нижньому регістрі, значення Meta)
    ("плагін", "PLUGIN_HYBRID"), ("plug", "PLUGIN_HYBRID"), ("phev", "PLUGIN_HYBRID"),
    ("гібрид", "HYBRID"), ("hybrid", "HYBRID"),
    ("електро", "ELECTRIC"), ("electric", "ELECTRIC"),
    ("дизел", "DIESEL"), ("diesel", "DIESEL"),
    ("бензин", "GASOLINE"), ("gasoline", "GASOLINE"), ("petrol", "GASOLINE"),
    ("газ", "OTHER"),
]
BODY_RULES = [
    ("кабріолет", "CONVERTIBLE"), ("купе", "COUPE"), ("ліфтбек", "HATCHBACK"),
    ("хетчбек", "HATCHBACK"), ("хэтчбек", "HATCHBACK"), ("універсал", "WAGON"),
    ("мінівен", "MINIVAN"), ("мікроавтобус", "VAN"), ("фургон", "VAN"),
    ("пікап", "TRUCK"), ("пикап", "TRUCK"), ("suv", "SUV"), ("кросовер", "SUV"),
    ("позашляховик", "SUV"), ("седан", "SEDAN"), ("лімузин", "SEDAN"),
]
GEARBOX_MANUAL = ("ручна", "механ")
GEARBOX_AUTO = ("автомат", "типтрон", "робот", "варіатор", "cvt", "dsg")
DRIVE_RULES = [("перед", "FWD"), ("задн", "RWD"), ("повн", "AWD"), ("4x4", "4X4"), ("awd", "AWD")]


def map_rules(value, rules, default=None):
    v = (value or "").lower()
    for key, out in rules:
        if key in v:
            return out
    return default


def map_gearbox(value):
    v = (value or "").lower()
    if not v:
        return None
    if any(k in v for k in GEARBOX_MANUAL):
        return "MANUAL"
    if any(k in v for k in GEARBOX_AUTO):
        return "AUTOMATIC"
    return "OTHER"


def parse_listing(html, url):
    """Витягує дані з HTML сторінки оголошення. Повертає dict або None, якщо даних замало."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(name):
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return clean(tag.get("content")) if tag and tag.get("content") else ""

    page_text = clean(soup.get_text(" "))
    desc = meta("og:description") or meta("description")
    m = re.search(r"-(\d{6,})\.html", url)
    lid = m.group(1) if m else None

    # --- назва, рік
    h1 = soup.find("h1")
    title_raw = clean(h1.get_text()) if h1 else ""
    name, year = title_raw, None
    m = re.match(r"^(.*?),\s*(\d{4})\s*р\.?\s*в\.?", title_raw)
    if m:
        name, year = clean(m.group(1)), m.group(2)
    if not year:
        m = re.search(r"(\d{4})\s*року випуску", desc)
        year = m.group(1) if m else None

    # --- марка / модель / місто із хлібних крихт
    make = model = city = None
    anchors = [(clean(a.get_text()), a["href"]) for a in soup.find_all("a", href=True)]
    mk = [(len(t), t, h) for t, h in anchors
          if re.match(r"^(?:https?://automoto\.ua)?/uk/car/[^/?#]+$", h) and t and name.lower().startswith(t.lower())]
    make_href = None
    if mk:
        _, make, make_href = max(mk)
        md = [(len(t), t, h) for t, h in anchors
              if h.startswith(make_href + "/") and h.count("/") == make_href.count("/") + 1
              and t and name.lower().startswith(t.lower())]
        model_href = None
        if md:
            _, mt, model_href = max(md)
            model = clean(mt[len(make):]) if mt.lower().startswith(make.lower()) else mt
        if model_href:
            ct = [t for t, h in anchors if h.startswith(model_href + "/") and h.count("/") == model_href.count("/") + 1 and t]
            city = ct[0] if ct else None
    if not make and name:
        make = name.split(" ")[0]
    if make and not model and name:
        model = clean(name[len(make):])
    if not city:
        m = re.search(r"UA,\s*[^,]+,\s*([^,]+),", page_text)
        city = clean(m.group(1)) if m else None

    # --- таблиця характеристик (перше входження мітки = "Інформація від власника")
    specs = {}
    for tr in soup.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        if len(cells) == 2 and cells[0] and cells[1]:
            specs.setdefault(cells[0], cells[1])

    def from_desc(label, stop=r"\.(?:\s|$)"):
        m = re.search(label + r"\s+(.+?)" + stop, desc)
        return clean(m.group(1)) if m else None

    gearbox = specs.get("Коробка передач") or from_desc("Коробка передач")
    drive = specs.get("Привід") or from_desc("Привід")
    color = specs.get("Колір") or from_desc("Колір")
    body = specs.get("Тип кузова")
    fuel = specs.get("Тип палива")
    engine = specs.get("Двигун")
    if not (fuel and engine):
        m = re.search(r"Двигун\s+([\d.,]+)\s+(.+?)\.(?:\s|$)", desc)
        if m:
            engine = engine or m.group(1)
            fuel = fuel or clean(m.group(2))
    m = re.search(r"([\d.,]+)", engine or "")
    engine_l = m.group(1).replace(",", ".") if m else None

    # --- пробіг (км)
    mileage = None
    m = (re.search(r"Пробіг\s+([\d\s]+?)\s*тис", desc)
         or re.search(r"Пробіг від продавця\s*([\d\s]+?)\s*тис", page_text)
         or re.search(r"пробігом\s+([\d\s]+?)\s*тис", page_text))
    if m and to_int(m.group(1)) is not None:
        mileage = to_int(m.group(1)) * 1000

    # --- ціна
    price_usd = price_uah = None
    m = re.search(r"за ціною\s*([\d\s ]+?)\s*\$", desc) or re.search(r"(\d[\d\s ]*?)\s*\$", page_text)
    if m:
        price_usd = to_int(m.group(1))
    m = re.search(r"(\d[\d\s ]*?)\s*грн(?!\s*/)", page_text)
    if m:
        price_uah = to_int(m.group(1))

    # --- фото
    image = meta("og:image")
    if not image:
        img = soup.find("img", src=re.compile(r"img\.automoto\.ua/auto/"))
        image = img["src"] if img else None
    if image and image.startswith("//"):
        image = "https:" + image

    if not (lid and name and (price_usd or price_uah) and image):
        return None
    return {
        "id": lid, "url": url, "name": name, "make": make, "model": model, "year": year,
        "price_usd": price_usd, "price_uah": price_uah, "city": city, "mileage_km": mileage,
        "fuel": fuel, "engine_l": engine_l, "gearbox": gearbox, "drive": drive,
        "body": body, "color": color, "image": image,
    }


# ───────────────────────── дані → рядок фіду ─────────────────────────
def with_utm(url):
    if not UTM:
        return url
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    for k, v in UTM.items():
        q.setdefault(k, v)
    return urlunparse(p._replace(query=urlencode(q)))


def build_row(d, labels):
    """dict оголошення → dict {назва_колонки_Meta: значення}."""
    cur, price = PRICE_CURRENCY, d["price_usd"] if PRICE_CURRENCY == "USD" else d["price_uah"]
    if not price:  # немає ціни в потрібній валюті - беремо іншу, але чесно підписуємо валюту
        cur, price = ("UAH", d["price_uah"]) if PRICE_CURRENCY == "USD" else ("USD", d["price_usd"])
    title = clean(" ".join(x for x in [d["make"], d["model"], d["year"]] if x)) or d["name"]

    parts = [f"{d['make']} {d['model']}".strip() + (f" {d['year']} р.в." if d["year"] else "")]
    if d["city"]:
        parts.append(d["city"])
    if d["mileage_km"] is not None:
        parts.append(f"пробіг {d['mileage_km'] // 1000} тис. км")
    engine_part = ", ".join(x for x in [d["fuel"], f"{d['engine_l']} л" if d["engine_l"] else None] if x)
    if engine_part:
        parts.append(engine_part)
    for x in (d["gearbox"], d["drive"] and f"{d['drive'].lower()} привід"):
        if x:
            parts.append(x)

    row = {
        "title": title,
        "availability": "AVAILABLE",
        "price": f"{price} {cur}",
        "image[0].url": d["image"],
        "image[0].tag[0]": IMAGE_TAG,
        "vehicle_offer_id": d["id"],
        "offer_description": " · ".join(parts),
        "url": with_utm(d["url"]),
        "custom_label_0": d["city"] or "",
        "custom_label_1": labels[0] if labels else "",
        "custom_number_0": str(d["mileage_km"]) if d["mileage_km"] is not None else "",
        "custom_number_1": str(d["price_uah"]) if d["price_uah"] else "",
        "offer_type": OFFER_TYPE,
        "make": d["make"] or "",
        "model": d["model"] or "",
        "year": d["year"] or "",
        "fuel_type": map_rules(d["fuel"], FUEL_RULES, "OTHER") if d["fuel"] else "",
        "drivetrain": map_rules(d["drive"], DRIVE_RULES) or "",
        "transmission": map_gearbox(d["gearbox"]) or "",
        "body_style": map_rules(d["body"], BODY_RULES, "OTHER") if d["body"] else "",
        "exterior_color": d["color"] or "",
    }
    for i, lab in enumerate(labels[:2]):
        row[f"product_tags[{i}]"] = lab
    return row


# ───────────────────────── запис CSV / XML ─────────────────────────
def read_template_header():
    with open(TEMPLATE_CSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1]  # (рядок-коментарі, рядок-назви колонок)


def write_csv(path, rows):
    comments, columns = read_template_header()
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(comments)
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") for c in columns])


def write_xml(path, rows):
    _, columns = read_template_header()
    root = ET.Element("listings")
    ET.SubElement(root, "title").text = FEED_TITLE
    for r in rows:
        li = ET.SubElement(root, "listing")
        img = ET.SubElement(li, "image")
        ET.SubElement(img, "url").text = r["image[0].url"]
        ET.SubElement(img, "tag").text = r["image[0].tag[0]"]
        for c in columns:
            if c.startswith(("image[", "video[", "applink.")):
                continue
            v = r.get(c, "")
            if v == "":
                continue
            tag = re.sub(r"\[\d+\]$", "", c)  # product_tags[0] -> <product_tags>
            ET.SubElement(li, tag).text = str(v)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# ───────────────────────── кеш / стан ─────────────────────────
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_catalog_lines(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, label = line.partition("|")
        url, label = url.strip(), label.strip()
        if not label:
            label = [s for s in urlparse(url).path.split("/") if s][-1] if urlparse(url).path.strip("/") else "all"
        out.append((url, label))
    return out


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogs", default=str(ROOT / "catalogs.txt"))
    ap.add_argument("--debug", action="store_true", help="зберегти HTML перших сторінок каталогів у debug/")
    ap.add_argument("--force", action="store_true", help="записати фід навіть якщо оголошень різко поменшало")
    args = ap.parse_args()

    catalogs = parse_catalog_lines(args.catalogs)
    if not catalogs:
        log("catalogs.txt порожній - додай хоча б одне посилання на каталог.")
        return 1

    # 1. зібрати оголошення з усіх каталогів (одне оголошення може бути у кількох - збираємо мітки)
    listings, labels_by_id = {}, {}
    for url, label in catalogs:
        log(f"\nКаталог: {url}  [мітка: {label}]")
        found = collect_catalog(url, debug=args.debug)
        log(f"  разом із каталогу: {len(found)}")
        for lid, lurl in found:
            listings.setdefault(lid, lurl)
            labels_by_id.setdefault(lid, [])
            if label not in labels_by_id[lid]:
                labels_by_id[lid].append(label)
    if not listings:
        log("\nЖодного оголошення не знайдено. Запусти з --debug і перевір debug/*.html")
        return 2

    # 2. деталі кожного оголошення (з кешем)
    cache = load_json(CACHE_FILE, {})
    now = datetime.now(timezone.utc)
    rows, skipped = [], []
    log(f"\nОбробляю {len(listings)} оголошень...")
    for i, (lid, lurl) in enumerate(listings.items(), 1):
        entry = cache.get(lid)
        age = now - datetime.fromisoformat(entry["fetched"]) if entry else None
        data = None
        if entry and age < timedelta(days=DETAIL_REFRESH_DAYS):
            data = entry["data"]
        else:
            status, html = fetch(lurl)
            time.sleep(REQUEST_DELAY)
            if html:
                data = parse_listing(html, lurl)
                if data:
                    cache[lid] = {"fetched": now.isoformat(), "data": data}
                else:
                    skipped.append((lid, "не вдалось розібрати сторінку"))
                    continue
            elif status in (404, 410):
                cache.pop(lid, None)
                skipped.append((lid, f"оголошення видалено ({status})"))
                continue
            elif entry and age < timedelta(days=STALE_CACHE_MAX_DAYS):
                data = entry["data"]  # сайт тимчасово не відповів - беремо старі дані
            else:
                skipped.append((lid, f"немає відповіді (status={status})"))
                continue
        rows.append(build_row(data, labels_by_id[lid]))
        if i % 25 == 0:
            log(f"  {i}/{len(listings)}")

    # прибрати з кешу оголошення, яких більше немає в каталогах
    for lid in list(cache):
        if lid not in listings:
            del cache[lid]

    log(f"\nУ фіді: {len(rows)} оголошень, пропущено: {len(skipped)}")
    for lid, why in skipped[:20]:
        log(f"  - {lid}: {why}")

    # 3. захист від «порожнього» або обваленого фіду
    prev = load_json(STATE_FILE, {}).get("count", 0)
    if not rows:
        log("Фід порожній - попередній файл не перезаписано.")
        return 3
    if prev >= 20 and len(rows) < prev * MIN_RATIO_VS_PREVIOUS and not args.force:
        log(f"Оголошень стало {len(rows)} проти {prev} минулого разу - схоже на збій сайту. "
            f"Попередній фід збережено. Для примусового запису: --force")
        return 4

    OUT_DIR.mkdir(exist_ok=True)
    write_csv(OUT_DIR / "catalog_vehicle_offer.csv", rows)
    write_xml(OUT_DIR / "catalog_vehicle_offer.xml", rows)
    save_json(CACHE_FILE, cache)
    save_json(STATE_FILE, {"count": len(rows), "updated": now.isoformat()})
    log(f"Готово: {OUT_DIR}/catalog_vehicle_offer.csv та .xml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
