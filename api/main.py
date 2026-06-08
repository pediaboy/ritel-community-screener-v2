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
GOAPI_KEY    = os.getenv("GOAPI_KEY")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "pedia123")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# GoAPI: WAJIB pakai X-API-Key header
GOAPI_HEADERS = {
    "X-API-Key": GOAPI_KEY,
    "accept": "application/json"
}
GOAPI_BASE = "https://api.goapi.io/stock/idx"

# ─── In-memory settings (fallback; persists per warm Lambda) ─────────────────
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
    phone_number: str  # maps to `whatsapp` column in Supabase

# ─── GoAPI Helpers ────────────────────────────────────────────────────────────

def fetch_lq45_tickers() -> list:
    """Returns list of ticker strings e.g. ['BBCA', 'BMRI', ...]"""
    url = f"{GOAPI_BASE}/index/LQ45/items"
    resp = requests.get(url, headers=GOAPI_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    # shape: {"status":"success","data":{"results":["AADI","ADMR",...]}}
    results = data.get("data", {}).get("results", [])
    if results and isinstance(results[0], dict):
        return [r.get("symbol", "") for r in results if r.get("symbol")]
    return [r for r in results if isinstance(r, str)]


def fetch_prices_batch(symbols: list) -> dict:
    """
    Returns dict: ticker -> {"last": price, "volume": vol, "change_pct": pct}
    GoAPI prices endpoint: GET /stock/idx/prices?symbols=A,B,C
    Response: {"data":{"results":[{"symbol":"BBCA","close":4960,"volume":...,"change_pct":-2.27},...]}}
    """
    all_prices = {}
    for i in range(0, len(symbols), 50):
        batch = ",".join(symbols[i:i+50])
        url = f"{GOAPI_BASE}/prices?symbols={batch}"
        try:
            resp = requests.get(url, headers=GOAPI_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("results", [])
            for item in results:
                sym = item.get("symbol", "")
                if sym:
                    all_prices[sym] = {
                        "last": item.get("close", item.get("price", 0)),
                        "volume": item.get("volume", 0),
                        "change_pct": item.get("change_pct", 0),
                    }
        except Exception as e:
            print(f"[WARN] prices batch error: {e}")
    return all_prices


def compute_indicators_for(ticker: str):
    """
    GET /stock/idx/{ticker}/historical?period=daily&limit=60
    Response: {"data":{"results":[{"symbol":"BBCA","date":"..","close":4960,...},...]}}
    Returns (ma20, macd_line, signal_line) or (None, None, None)
    """
    try:
        url = f"{GOAPI_BASE}/{ticker}/historical?period=daily&limit=60"
        resp = requests.get(url, headers=GOAPI_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("results", [])
        closes = [float(r["close"]) for r in results if r.get("close") is not None]
        if len(closes) < 26:
            return None, None, None
        # reverse so oldest first (GoAPI returns newest first)
        closes = closes[::-1]
        s = pd.Series(closes)
        ma20 = float(s.rolling(window=20).mean().iloc[-1])
        ema12 = s.ewm(span=12, adjust=False).mean()
        ema26 = s.ewm(span=26, adjust=False).mean()
        macd_line = float((ema12 - ema26).iloc[-1])
        signal_line = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])
        return round(ma20, 2), round(macd_line, 4), round(signal_line, 4)
    except Exception as e:
        print(f"[WARN] indicator {ticker}: {e}")
        return None, None, None

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "goapi_key_set": bool(GOAPI_KEY),
        "supabase_url": SUPABASE_URL
    }


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
    """Live screener: fetch LQ45 tickers + prices, join dengan indicators DB"""
    if not GOAPI_KEY:
        raise HTTPException(status_code=500, detail="GOAPI_KEY belum diset")
    try:
        tickers = fetch_lq45_tickers()
        if not tickers:
            raise ValueError("Tidak bisa ambil ticker LQ45")

        prices = fetch_prices_batch(tickers)
        if not prices:
            raise ValueError("Tidak bisa ambil harga saham dari GoAPI")

        # Ambil cached indicators dari Supabase
        indicators_map = {}
        try:
            ind_res = supabase.table("indicators").select(
                "ticker,ma20,macd_line,signal_line"
            ).execute()
            for row in (ind_res.data or []):
                indicators_map[row["ticker"]] = row
        except Exception as e:
            print(f"[WARN] indicators fetch: {e}")

        result = []
        for ticker, pd_data in prices.items():
            ind = indicators_map.get(ticker, {})
            result.append({
                "ticker": ticker,
                "price": pd_data.get("last", 0),
                "volume": pd_data.get("volume", 0),
                "change_pct": pd_data.get("change_pct", 0),
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


@app.post("/api/admin/refresh-stocks")
def refresh_stocks(x_admin_token: str = Header(None)):
    """Fetch live data dan simpan ke DB + hitung indicators"""
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        tickers = fetch_lq45_tickers()
        prices = fetch_prices_batch(tickers)
        upserted_stocks = 0
        upserted_indicators = 0

        for ticker, pd_data in prices.items():
            try:
                supabase.table("stocks_data").upsert({
                    "ticker": ticker,
                    "price": pd_data.get("last", 0),
                    "volume": pd_data.get("volume", 0),
                    "updated_at": datetime.utcnow().isoformat()
                }).execute()
                upserted_stocks += 1
            except Exception as e:
                print(f"[WARN] upsert stocks {ticker}: {e}")

        # Hitung indicators untuk 10 saham teratas
        for ticker in tickers[:10]:
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
            "tickers_fetched": len(tickers),
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
        users = [{
            "id": u.get("id"),
            "name": u.get("name"),
            "phone_number": u.get("whatsapp"),
            "status": u.get("status", "Free"),
            "created_at": u.get("created_at"),
        } for u in (res.data or [])]
        return {"data": users}
    except Exception as e:
        return {"data": [], "error": str(e)}


@app.post("/api/admin/upgrade-vip")
def upgrade_vip(body: UserVipUpgrade, x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        res = supabase.table("users").update({"status": "VIP"}).eq("whatsapp", body.phone_number).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail=f"User WA {body.phone_number} tidak ditemukan")
        return {"success": True, "data": res.data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/goapi-test")
def debug_goapi():
    """Debug: test GoAPI connection dan response structure"""
    results = {}
    try:
        r = requests.get(f"{GOAPI_BASE}/index/LQ45/items", headers=GOAPI_HEADERS, timeout=10)
        results["lq45"] = {"status": r.status_code, "sample": str(r.json())[:300]}
    except Exception as e:
        results["lq45"] = {"error": str(e)}
    try:
        r = requests.get(f"{GOAPI_BASE}/prices?symbols=BBCA,BMRI", headers=GOAPI_HEADERS, timeout=10)
        results["prices"] = {"status": r.status_code, "sample": str(r.json())[:300]}
    except Exception as e:
        results["prices"] = {"error": str(e)}
    return results
