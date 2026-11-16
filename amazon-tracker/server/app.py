"""
متتبع أسعار امازون مصر - السيرفر
- بيخزن المنتجات وتاريخ الأسعار في SQLite
- بيفحص كل منتج بفواصل عشوائية (افتراضي ١٥ دقيقة) عشان يقلل خطر الحظر
- بيبعت إشعار تليجرام لما السعر يتغير أو يوصل للسعر المستهدف
تشغيل:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import re
import random
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.environ.get("TRACKER_DB", str(Path(__file__).with_name("tracker.db")))
APP_TOKEN = os.environ.get("APP_TOKEN", "")  # اختياري: كلمة سر بسيطة للـ API
MIN_INTERVAL = 30  # ثانية
DEFAULT_INTERVAL = 900  # ١٥ دقيقة

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_db_lock = threading.RLock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row

_state = {"blocked_until": 0.0, "blocks": 0, "last_block_notified": 0.0}


# ----------------------------------------------------------------- database
def init_db():
    with _db_lock:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              asin TEXT UNIQUE NOT NULL,
              url TEXT NOT NULL,
              title TEXT, image TEXT,
              price REAL, available INTEGER DEFAULT 1,
              target_price REAL,
              interval INTEGER,
              last_checked REAL, next_check REAL DEFAULT 0,
              last_change REAL,
              status TEXT DEFAULT 'pending',
              added REAL
            );
            CREATE TABLE IF NOT EXISTS changes(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              kind TEXT NOT NULL,           -- price | stock
              old_price REAL, new_price REAL,
              ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              price REAL, ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
            """
        )
        _db.commit()


def get_setting(key, default=""):
    with _db_lock:
        r = _db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return r["v"] if r else default


def set_setting(key, value):
    with _db_lock:
        _db.execute(
            "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )
        _db.commit()


# ------------------------------------------------------------------ parsing
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)
CAPTCHA_MARKERS = (
    "captchacharacters",
    "validateCaptcha",
    "Enter the characters you see below",
    "Type the characters you see in this image",
    "api-services-support@amazon",
    "اكتب الحروف",
)


class Blocked(Exception):
    pass


def parse_price(text):
    if not text:
        return None
    t = text.translate(_AR_DIGITS)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", t)
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def parse_product_html(html):
    """يرجّع dict فيه title/image/price/available من صفحة منتج امازون."""
    if any(mark in html for mark in CAPTCHA_MARKERS):
        raise Blocked("captcha")
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else None

    image = None
    img = soup.select_one("#landingImage") or soup.select_one("#imgTagWrapperId img")
    if img:
        image = img.get("data-old-hires") or img.get("src")

    price = None
    selectors = [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        ".priceToPay .a-offscreen",
        "span.a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
    ]
    for sel in selectors:
        for el in soup.select(sel):
            parent = el.find_parent(class_="a-price")
            if parent is not None and parent.get("data-a-strike") == "true":
                continue  # السعر القديم المشطوب
            price = parse_price(el.get_text())
            if price:
                break
        if price:
            break
    if price is None:
        whole = soup.select_one(".a-price-whole")
        if whole:
            frac = soup.select_one(".a-price-fraction")
            price = parse_price(whole.get_text() + "." + (frac.get_text() if frac else "0"))

    if title is None and price is None:
        # صفحة مش منتج (404 أو صفحة غريبة)
        raise ValueError("not a product page")
    return {"title": title, "image": image, "price": price, "available": price is not None}


def fetch_product(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    if r.status_code in (429, 503):
        raise Blocked(f"http {r.status_code}")
    if r.status_code == 404:
        raise ValueError("404 - المنتج مش موجود")
    r.raise_for_status()
    return parse_product_html(r.text)


def normalize_url(url):
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if not (host == "amazon.eg" or host.endswith(".amazon.eg")):
        raise HTTPException(400, "الرابط لازم يكون من amazon.eg")
    m = ASIN_RE.search(p.path)
    if not m:
        raise HTTPException(400, "مش لاقي كود المنتج (ASIN) في الرابط - استخدم رابط صفحة منتج")
    asin = m.group(1).upper()
    return asin, f"https://www.amazon.eg/dp/{asin}"


# --------------------------------------------------------------- notifications
def notify(text):
    token, chat = get_setting("telegram_token"), get_setting("telegram_chat")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException:
        pass


def fmt(p):
    return "—" if p is None else f"{p:,.2f} ج.م"


# ------------------------------------------------------------------ checking
def check_product(pid):
    with _db_lock:
        row = _db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not row:
        return
    now = time.time()
    interval = row["interval"] or int(get_setting("default_interval", DEFAULT_INTERVAL))
    interval = max(MIN_INTERVAL, interval)
    jitter = random.uniform(0.75, 1.35)
    try:
        data = fetch_product(row["url"])
    except Blocked as e:
        _on_blocked(str(e))
        with _db_lock:
            _db.execute(
                "UPDATE products SET status=?, next_check=? WHERE id=?",
                ("blocked", _state["blocked_until"], pid),
            )
            _db.commit()
        return
    except Exception as e:  # noqa: BLE001
        with _db_lock:
            _db.execute(
                "UPDATE products SET status=?, last_checked=?, next_check=? WHERE id=?",
                (f"error: {e}"[:200], now, now + interval * jitter, pid),
            )
            _db.commit()
        return

    _state["blocks"] = 0
    old_price, old_avail = row["price"], bool(row["available"])
    new_price, new_avail = data["price"], data["available"]
    title = data["title"] or row["title"]
    changed_at = row["last_change"]

    with _db_lock:
        if row["last_checked"] is None:
            _db.execute("INSERT INTO history(product_id,price,ts) VALUES(?,?,?)", (pid, new_price, now))
        elif new_price != old_price and new_price is not None:
            _db.execute(
                "INSERT INTO changes(product_id,kind,old_price,new_price,ts) VALUES(?,?,?,?,?)",
                (pid, "price", old_price, new_price, now),
            )
            _db.execute("INSERT INTO history(product_id,price,ts) VALUES(?,?,?)", (pid, new_price, now))
            changed_at = now
        if row["last_checked"] is not None and new_avail != old_avail:
            _db.execute(
                "INSERT INTO changes(product_id,kind,old_price,new_price,ts) VALUES(?,?,?,?,?)",
                (pid, "stock", old_price, new_price, now),
            )
            changed_at = now
        _db.execute(
            """UPDATE products SET title=?, image=COALESCE(?,image), price=COALESCE(?,price),
               available=?, last_checked=?, next_check=?, last_change=?, status='ok' WHERE id=?""",
            (title, data["image"], new_price, int(new_avail), now, now + interval * jitter, changed_at, pid),
        )
        _db.commit()

    # إشعارات
    if row["last_checked"] is not None:
        short = (title or row["asin"])[:90]
        if new_price is not None and old_price is not None and new_price != old_price:
            arrow = "📉 نزل" if new_price < old_price else "📈 زاد"
            notify(f"{arrow} السعر\n{short}\nكان {fmt(old_price)} ← بقى {fmt(new_price)}\n{row['url']}")
        tp = row["target_price"]
        if tp and new_price is not None and new_price <= tp and (old_price is None or old_price > tp):
            notify(f"🎯 وصل للسعر المستهدف ({fmt(tp)})\n{short}\nالسعر دلوقتي {fmt(new_price)}\n{row['url']}")
        if new_avail and not old_avail:
            notify(f"✅ رجع متوفر\n{short}\n{fmt(new_price)}\n{row['url']}")


def _on_blocked(reason):
    _state["blocks"] += 1
    cooldown = min(6 * 3600, 900 * 2 ** (_state["blocks"] - 1))
    _state["blocked_until"] = time.time() + cooldown
    if time.time() - _state["last_block_notified"] > 3600:
        _state["last_block_notified"] = time.time()
        notify(f"⚠️ امازون عملت حظر مؤقت ({reason}). المتابعة هتقف {cooldown // 60} دقيقة وتكمل تلقائي.")


def scheduler_loop():
    while True:
        try:
            now = time.time()
            if now >= _state["blocked_until"]:
                with _db_lock:
                    row = _db.execute(
                        "SELECT id FROM products WHERE next_check<=? ORDER BY next_check LIMIT 1", (now,)
                    ).fetchone()
                if row:
                    check_product(row["id"])
                    time.sleep(random.uniform(4, 12))  # فاصل بين كل طلبين
                    continue
        except Exception as e:  # noqa: BLE001
            print("scheduler error:", e)
        time.sleep(3)


# ----------------------------------------------------------------------- API
@asynccontextmanager
async def lifespan(_app):
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="Amazon EG Tracker", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def auth(x_token: str = Header(default="")):
    if APP_TOKEN and x_token != APP_TOKEN:
        raise HTTPException(401, "كلمة السر غلط")


class NewProduct(BaseModel):
    url: str
    target_price: float | None = None
    interval: int | None = None


class ProductPatch(BaseModel):
    target_price: float | None = None
    interval: int | None = None
    clear_target: bool = False


class SettingsIn(BaseModel):
    telegram_token: str | None = None
    telegram_chat: str | None = None
    default_interval: int | None = None


def row_to_dict(r):
    d = dict(r)
    d["available"] = bool(d["available"])
    return d


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    with _db_lock:
        n = _db.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    left = max(0, int(_state["blocked_until"] - time.time()))
    return {"products": n, "blocked_seconds_left": left, "now": time.time()}


@app.get("/api/products", dependencies=[Depends(auth)])
def list_products():
    with _db_lock:
        rows = _db.execute("SELECT * FROM products ORDER BY COALESCE(last_change,0) DESC, id DESC").fetchall()
    return [row_to_dict(r) for r in rows]


@app.post("/api/products", dependencies=[Depends(auth)])
def add_product(p: NewProduct):
    asin, url = normalize_url(p.url)
    interval = max(MIN_INTERVAL, p.interval) if p.interval else None
    with _db_lock:
        try:
            cur = _db.execute(
                "INSERT INTO products(asin,url,target_price,interval,added,next_check) VALUES(?,?,?,?,?,0)",
                (asin, url, p.target_price, interval, time.time()),
            )
            _db.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "المنتج ده متضاف قبل كده")
    return {"id": cur.lastrowid, "asin": asin}


@app.patch("/api/products/{pid}", dependencies=[Depends(auth)])
def patch_product(pid: int, p: ProductPatch):
    with _db_lock:
        if p.clear_target:
            _db.execute("UPDATE products SET target_price=NULL WHERE id=?", (pid,))
        elif p.target_price is not None:
            _db.execute("UPDATE products SET target_price=? WHERE id=?", (p.target_price, pid))
        if p.interval:
            _db.execute("UPDATE products SET interval=? WHERE id=?", (max(MIN_INTERVAL, p.interval), pid))
        _db.commit()
    return {"ok": True}


@app.delete("/api/products/{pid}", dependencies=[Depends(auth)])
def delete_product(pid: int):
    with _db_lock:
        for t, col in (("products", "id"), ("changes", "product_id"), ("history", "product_id")):
            _db.execute(f"DELETE FROM {t} WHERE {col}=?", (pid,))
        _db.commit()
    return {"ok": True}


@app.post("/api/products/{pid}/check", dependencies=[Depends(auth)])
def force_check(pid: int):
    with _db_lock:
        _db.execute("UPDATE products SET next_check=0 WHERE id=?", (pid,))
        _db.commit()
    return {"ok": True}


@app.get("/api/products/{pid}/history", dependencies=[Depends(auth)])
def product_history(pid: int):
    with _db_lock:
        rows = _db.execute("SELECT price, ts FROM history WHERE product_id=? ORDER BY ts", (pid,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/changes", dependencies=[Depends(auth)])
def recent_changes(limit: int = 100):
    with _db_lock:
        rows = _db.execute(
            """SELECT c.*, p.title, p.image, p.url FROM changes c
               JOIN products p ON p.id=c.product_id ORDER BY c.ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/settings", dependencies=[Depends(auth)])
def read_settings():
    tok = get_setting("telegram_token")
    return {
        "telegram_token_set": bool(tok),
        "telegram_chat": get_setting("telegram_chat"),
        "default_interval": int(get_setting("default_interval", DEFAULT_INTERVAL)),
    }


@app.post("/api/settings", dependencies=[Depends(auth)])
def write_settings(s: SettingsIn):
    if s.telegram_token is not None:
        set_setting("telegram_token", s.telegram_token.strip())
    if s.telegram_chat is not None:
        set_setting("telegram_chat", s.telegram_chat.strip())
    if s.default_interval:
        set_setting("default_interval", max(MIN_INTERVAL, s.default_interval))
    return {"ok": True}


@app.post("/api/test-notification", dependencies=[Depends(auth)])
def test_notification():
    notify("✅ إشعار تجريبي من متتبع امازون مصر")
    return {"ok": True}


_www = Path(__file__).resolve().parent.parent / "app" / "www"
if _www.exists():  # نفس الواجهة متاحة من المتصفح كمان
    app.mount("/", StaticFiles(directory=_www, html=True), name="www")
