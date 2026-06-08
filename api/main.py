import os
import json
import requests
import pandas as pd
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, Request
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

# ─── In-memory settings (persists per Lambda warm instance) ───────────────────
_settings_cache = {
    "id": 1,
    "vip_price": float(os.getenv("SETTING_VIP_PRICE", "99000")),
    "bank_account": os.getenv("SETTING_BANK_ACCOUNT", "-"),
    "wa_channel": os.getenv("SETTING_WA_CHANNEL", "#"),
    "wa_group": os.getenv("SETTING_WA_GROUP", "#"),
    "ig_link": os.getenv("SETTING_IG_LINK", "#"),
}

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
    phone_number: str  # maps to whatsapp column

# ─── GoAPI Helpers ────────────────────────────────────────────────────────────

def fetch_lq45_tickers():
    url = "https://api.goapi.io/stock/idx/index/LQ45/items"
    resp = requests.get(url, headers=GOAPI_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    # response shape: {"data": {"items": [{"symbol": "BBCA"}, ...]}}
    items = data.get("data", {}).get("items", [])
    if not items:
        # fallback shape
        items = data.get("items", [])
    tickers = [item.get("symbol") or item.get("ticker","") for item in items if item.get("symbol") or item.get("ticker")]
    return tickers


def fetch_prices_batch(symbols: list):
    """Fetch prices in batches of 50. Returns dict ticker -> price_data"""
    all_prices = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        joined = ",".join(batch)
        url = f"https://api.goapi.io/stock/idx/prices?symbols={joined}"
        try:
            resp = requests.get(url, headers=GOAPI_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            # response shape: {"data": {"prices": {"BBCA": {"last": 9000, "volume": 1e6, "change_pct": 1.2}}}}
            prices = data.get("data", {}).get("prices", {})
            if not prices:
                # fallback: flat array
                arr = data.get("data", [])
                if isinstance(arr, list):
                    for item in arr:
                        sym = item.get("symbol","")
                        if sym:
                            prices[sym] = {"last": item.get("price",0), "volume": item.get("volume",0), "change_pct": item.get("change_pct",0)}
            all_prices.update(prices)
        except Exception as e:
            print(f"[WARN] Batch error {joined[:30]}: {e}")
    return all_prices


def compute_indicators_for(ticker: str):
    """Compute MA20 & MACD from historical candles via pandas"""
    try:
        url = f"https://api.goapi.io/stock/idx/historical?symbol={ticker}&period=daily&limit=60"
        resp = requests.get(url, headers=GOAPI_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        candles = data.get("data", {}).get("candles", data.get("candles", []))
        closes = [float(c["close"]) for c in candles if c.get("close") is not None]
        if len(closes) < 26:
            return None, None, None
        s = pd.Series(closes)
        ma20 = float(s.rolling(window=20).mean().iloc[-1])
        ema12 = s.ewm(span=12, adjust=False).mean()
        ema26 = s.ewm(span=26, adjust=False).mean()
        macd_line = float((ema12 - ema26).iloc[-1])
        signal_line = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])
        return round(ma20, 2), round(macd_line, 4), round(signal_line, 4)
    except Exception as e:
        print(f"[WARN] Indicator error {ticker}: {e}")
        return None, None, None

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat(), "goapi_key_set": bool(GOAPI_KEY)}


@app.get("/api/settings")
def get_settings():
    return _settings_cache


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
        raise HTTPException(status_code=400, detail="Tidak ada data")
    _settings_cache.update(update_data)
    return {"success": True, "data": _settings_cache}


@app.get("/api/screener")
def screener():
    """Live screener: fetch LQ45 tickers, get prices, return sorted by change_pct"""
    if not GOAPI_KEY:
        raise HTTPException(status_code=500, detail="GOAPI_KEY tidak di-set")
    try:
        tickers = fetch_lq45_tickers()
        if not tickers:
            raise ValueError("Tidak bisa ambil ticker LQ45")
        prices = fetch_prices_batch(tickers)
        if not prices:
            raise ValueError("Tidak bisa ambil harga saham")

        # Ambil indicators dari Supabase
        indicators_map = {}
        try:
            ind_res = supabase.table("indicators").select("ticker,ma20,macd_line,signal_line").execute()
            for row in (ind_res.data or []):
                indicators_map[row["ticker"]] = row
        except Exception as e:
            print(f"[WARN] Indicator fetch: {e}")

        result = []
        for ticker, pd_data in prices.items():
            ind = indicators_map.get(ticker, {})
            result.append({
                "ticker": ticker,
                "price": pd_data.get("last", pd_data.get("close", 0)),
                "volume": pd_data.get("volume", 0),
                "change_pct": pd_data.get("change_pct", pd_data.get("changePercent", 0)),
                "ma20": ind.get("ma20"),
                "macd": ind.get("macd_line"),
                "signal": ind.get("signal_line"),
            })
        result.sort(key=lambda x: (x["change_pct"] or 0), reverse=True)
        return {
            "data": result,
            "count": len(result),
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stocks")
def get_stocks_from_db(limit: int = 50):
    """Ambil dari DB (historical cache)"""
    try:
        res = supabase.table("stocks_data").select("*").limit(limit).execute()
        return {"data": res.data or [], "count": len(res.data or [])}
    except Exception as e:
        return {"data": [], "count": 0, "error": str(e)}


@app.post("/api/admin/refresh-stocks")
def refresh_stocks(x_admin_token: str = Header(None)):
    """Fetch data live dan simpan ke stocks_data + indicators"""
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        tickers = fetch_lq45_tickers()
        prices = fetch_prices_batch(tickers)
        upserted_stocks = 0
        upserted_indicators = 0

        for ticker, pd_data in prices.items():
            last = pd_data.get("last", pd_data.get("close", 0))
            volume = pd_data.get("volume", 0)
            try:
                supabase.table("stocks_data").upsert({
                    "ticker": ticker,
                    "price": last,
                    "volume": volume,
                    "updated_at": datetime.utcnow().isoformat()
                }).execute()
                upserted_stocks += 1
            except Exception as e:
                print(f"[WARN] upsert stocks {ticker}: {e}")

        # Compute indicators for each ticker (batched, 1-2s per ticker)
        for ticker in tickers[:10]:  # limit 10 untuk avoid timeout
            ma20, macd_line, signal_line = compute_indicators_for(ticker)
            if ma20 is not None:
                try:
                    supabase.table("indicators").upsert({
                        "ticker": ticker,
                        "ma20": ma20,
                        "macd_line": macd_line,
                        "signal_line": signal_line,
                        "calculated_at": datetime.utcnow().isoformat()
                    }).execute()
                    upserted_indicators += 1
                except Exception as e:
                    print(f"[WARN] upsert indicators {ticker}: {e}")

        return {
            "success": True,
            "tickers": len(tickers),
            "stocks_updated": upserted_stocks,
            "indicators_updated": upserted_indicators
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/users")
def get_users(x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        res = supabase.table("users").select("id,name,whatsapp,status,created_at").execute()
        # normalize: expose whatsapp as phone_number for frontend
        users = []
        for u in (res.data or []):
            users.append({
                "id": u.get("id"),
                "name": u.get("name"),
                "phone_number": u.get("whatsapp"),
                "status": u.get("status","Free"),
                "created_at": u.get("created_at"),
            })
        return {"data": users}
    except Exception as e:
        return {"data": [], "error": str(e)}


@app.post("/api/admin/upgrade-vip")
def upgrade_vip(body: UserVipUpgrade, x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        # phone_number maps to whatsapp column
        res = supabase.table("users").update({"status": "VIP"}).eq("whatsapp", body.phone_number).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail=f"User dengan WA {body.phone_number} tidak ditemukan")
        return {"success": True, "data": res.data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/goapi-test")
def debug_goapi():
    """Debug endpoint untuk test GoAPI connection"""
    try:
        url = "https://api.goapi.io/stock/idx/index/LQ45/items"
        resp = requests.get(url, headers=GOAPI_HEADERS, timeout=10)
        raw = resp.json()
        return {
            "status_code": resp.status_code,
            "keys": list(raw.keys()),
            "data_keys": list(raw.get("data",{}).keys()) if isinstance(raw.get("data"),dict) else str(raw.get("data",""))[:100],
            "sample": str(raw)[:500]
        }
    except Exception as e:
        return {"error": str(e)}
