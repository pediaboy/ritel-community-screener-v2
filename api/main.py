import os
import json
import requests
import pandas as pd
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from typing import Optional

app = FastAPI(title="RITELCOMMUNITY.ID Screener API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ubsowwkgpooexrmwdpii.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GOAPI_KEY = os.getenv("GOAPI_KEY")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "pedia123")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

GOAPI_HEADERS = {
    "Authorization": GOAPI_KEY,
    "accept": "application/json"
}

# In-memory fallback for global_settings
DEFAULT_SETTINGS = {
    "id": 1,
    "vip_price": 99000,
    "bank_account": "-",
    "wa_channel": "#",
    "wa_group": "#",
    "ig_link": "#"
}
_settings_cache = dict(DEFAULT_SETTINGS)
_settings_table_ok = None  # None = unknown, True = exists, False = missing

# ─── Models ───────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    vip_price: Optional[float] = None
    bank_account: Optional[str] = None
    wa_channel: Optional[str] = None
    wa_group: Optional[str] = None
    ig_link: Optional[str] = None

class AdminLogin(BaseModel):
    password: str

class UserVipUpgrade(BaseModel):
    phone_number: str

# ─── Helper: settings table check ────────────────────────────────────────────

def check_settings_table():
    global _settings_table_ok
    if _settings_table_ok is not None:
        return _settings_table_ok
    try:
        res = supabase.table("global_settings").select("id").limit(1).execute()
        _settings_table_ok = True
        return True
    except Exception:
        _settings_table_ok = False
        return False

def get_settings_data():
    global _settings_cache
    if check_settings_table():
        try:
            res = supabase.table("global_settings").select("*").eq("id", 1).limit(1).execute()
            if res.data:
                _settings_cache = res.data[0]
                return _settings_cache
        except Exception:
            pass
    return _settings_cache

def update_settings_data(update_data: dict):
    global _settings_cache, _settings_table_ok
    if check_settings_table():
        try:
            res = supabase.table("global_settings").update(update_data).eq("id", 1).execute()
            if res.data:
                _settings_cache.update(update_data)
                return res.data
        except Exception:
            pass
    # Fallback to in-memory
    _settings_cache.update(update_data)
    return [_settings_cache]

# ─── Helper: fetch & compute ────────────────────────────────────────────────

def fetch_lq45_tickers():
    url = "https://api.goapi.io/stock/idx/index/LQ45/items"
    resp = requests.get(url, headers=GOAPI_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", {}).get("items", [])
    tickers = [item["symbol"] for item in items if "symbol" in item]
    return tickers


def fetch_prices_batch(symbols: list):
    """Fetch prices in batches of 50"""
    all_prices = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        joined = ",".join(batch)
        url = f"https://api.goapi.io/stock/idx/prices?symbols={joined}"
        try:
            resp = requests.get(url, headers=GOAPI_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            prices = data.get("data", {}).get("prices", {})
            all_prices.update(prices)
        except Exception as e:
            print(f"Batch fetch error for {joined}: {e}")
    return all_prices


def compute_indicators(ticker: str):
    """Compute MA20 & MACD using pandas rolling/ewm"""
    try:
        url = f"https://api.goapi.io/stock/idx/historical?symbol={ticker}&period=daily&limit=60"
        resp = requests.get(url, headers=GOAPI_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        closes = [item["close"] for item in data.get("data", {}).get("candles", []) if "close" in item]
        if len(closes) < 26:
            return None, None
        s = pd.Series(closes)
        ma20 = float(s.rolling(window=20).mean().iloc[-1])
        ema12 = s.ewm(span=12, adjust=False).mean()
        ema26 = s.ewm(span=26, adjust=False).mean()
        macd = float((ema12 - ema26).iloc[-1])
        return round(ma20, 2), round(macd, 4)
    except Exception as e:
        print(f"Indicator error for {ticker}: {e}")
        return None, None

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat(), "settings_table": check_settings_table()}


@app.get("/api/settings")
def get_settings():
    return get_settings_data()


@app.post("/api/admin/login")
def admin_login(body: AdminLogin):
    if body.password != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Password salah bro!")
    return {"success": True, "token": "admin-ok"}


@app.post("/api/admin/settings")
def update_settings(body: SettingsUpdate, x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    update_data = {k: v for k, v in body.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="Tidak ada data yang diupdate")
    result = update_settings_data(update_data)
    return {"success": True, "data": result}


@app.get("/api/stocks")
def get_stocks(limit: int = 50):
    try:
        res = supabase.table("stocks_data").select("*").order("change_pct", desc=True).limit(limit).execute()
        return {"data": res.data, "count": len(res.data)}
    except Exception as e:
        return {"data": [], "count": 0, "error": str(e)}


@app.post("/api/admin/refresh-stocks")
def refresh_stocks(x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        tickers = fetch_lq45_tickers()
        prices = fetch_prices_batch(tickers)
        upserted = 0
        for ticker, price_data in prices.items():
            last = price_data.get("last", 0)
            volume = price_data.get("volume", 0)
            change_pct = price_data.get("change_pct", 0)
            row = {
                "ticker": ticker,
                "price": last,
                "volume": volume,
                "change_pct": change_pct,
                "updated_at": datetime.utcnow().isoformat()
            }
            supabase.table("stocks_data").upsert(row).execute()
            upserted += 1
        return {"success": True, "updated": upserted, "tickers": tickers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/screener")
def screener():
    """Live screener: fetch LQ45, get prices, return sorted by change_pct"""
    try:
        tickers = fetch_lq45_tickers()
        prices = fetch_prices_batch(tickers)
        result = []
        for ticker, pd_data in prices.items():
            result.append({
                "ticker": ticker,
                "price": pd_data.get("last", 0),
                "volume": pd_data.get("volume", 0),
                "change_pct": pd_data.get("change_pct", 0),
                "ma20": None,
                "macd": None
            })
        result.sort(key=lambda x: x["change_pct"], reverse=True)
        return {"data": result, "count": len(result), "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/users")
def get_users(x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        res = supabase.table("users").select("*").execute()
        return {"data": res.data}
    except Exception as e:
        return {"data": [], "error": str(e)}


@app.post("/api/admin/upgrade-vip")
def upgrade_vip(body: UserVipUpgrade, x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        res = supabase.table("users").update({"status": "VIP"}).eq("phone_number", body.phone_number).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="User tidak ditemukan")
        return {"success": True, "data": res.data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
