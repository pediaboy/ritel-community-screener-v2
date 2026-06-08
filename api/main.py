import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from typing import Optional, List

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

# GoAPI: header WAJIB X-API-Key (bukan Authorization)
GOAPI_HEADERS = {"X-API-Key": GOAPI_KEY, "accept": "application/json"}
GOAPI_BASE    = "https://api.goapi.io/stock/idx"

# LQ45 static fallback kalau endpoint GoAPI kena limit
LQ45_STATIC = [
    "AADI","ADMR","ADRO","AKRA","AMMN","AMRT","ANTM","ASII","BBCA","BBNI",
    "BBRI","BBTN","BMRI","BREN","BRPT","BUMI","CPIN","CTRA","DSSA","EMTK",
    "EXCL","GOTO","HEAL","ICBP","INCO","INDF","INKP","ISAT","ITMG","JPFA",
    "KLBF","MBMA","MDKA","MEDC","MIKA","MNCN","NCKL","PGEO","PGAS","PTBA",
    "SMGR","TBIG","TLKM","TOWR","UNTR"
]

# In-memory settings
_settings_cache = {
    "id": 1,
    "vip_price": float(os.getenv("SETTING_VIP_PRICE", "99000")),
    "bank_account": os.getenv("SETTING_BANK_ACCOUNT", "-"),
    "wa_channel": os.getenv("SETTING_WA_CHANNEL", "#"),
    "wa_group": os.getenv("SETTING_WA_GROUP", "#"),
    "ig_link": os.getenv("SETTING_IG_LINK", "#"),
}

# ─── Models ────────────────────────────────────────────────────────────────────
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

# ─── GoAPI helpers ─────────────────────────────────────────────────────────────

def _goapi_limited(d: dict) -> bool:
    return d.get("status") == "error" and "limit" in d.get("message","").lower()

def goapi_get_tickers() -> Optional[List[str]]:
    try:
        r = requests.get(f"{GOAPI_BASE}/index/LQ45/items", headers=GOAPI_HEADERS, timeout=15)
        d = r.json()
        if _goapi_limited(d) or d.get("status") != "success":
            return None
        results = d.get("data", {}).get("results", [])
        if results and isinstance(results[0], str):
            return results
        return [x["symbol"] for x in results if x.get("symbol")]
    except Exception as e:
        print(f"[GoAPI] tickers error: {e}")
        return None

def goapi_get_prices(tickers: List[str]) -> Optional[List[dict]]:
    """
    GET /stock/idx/prices?symbols=A,B,C
    Response: {"data":{"results":[{"symbol","close","volume","change_pct"}]}}
    """
    all_data = []
    for i in range(0, len(tickers), 50):
        batch = ",".join(tickers[i:i+50])
        try:
            r = requests.get(f"{GOAPI_BASE}/prices?symbols={batch}", headers=GOAPI_HEADERS, timeout=15)
            d = r.json()
            if _goapi_limited(d) or d.get("status") != "success":
                return None
            for item in d.get("data", {}).get("results", []):
                sym = item.get("symbol","")
                if sym:
                    all_data.append({
                        "ticker": sym,
                        "price": float(item.get("close", 0)),
                        "volume": int(item.get("volume", 0)),
                        "change_pct": float(item.get("change_pct", 0)),
                    })
        except Exception as e:
            print(f"[GoAPI] prices error: {e}")
            return None
    return all_data or None

def goapi_get_historical(ticker: str, limit: int = 60) -> Optional[List[float]]:
    """
    GET /stock/idx/{TICKER}/historical?period=daily&limit=N
    Response: {"data":{"results":[{"symbol","date","open","high","low","close","volume"}]}}
    Returns closes oldest→newest.
    """
    try:
        r = requests.get(
            f"{GOAPI_BASE}/{ticker}/historical?period=daily&limit={limit}",
            headers=GOAPI_HEADERS, timeout=15
        )
        d = r.json()
        if _goapi_limited(d) or d.get("status") != "success":
            return None
        results = d.get("data", {}).get("results", [])
        closes = [float(x["close"]) for x in results if x.get("close") is not None]
        return closes[::-1]  # GoAPI: newest-first → reverse to oldest-first
    except Exception as e:
        print(f"[GoAPI] historical {ticker} error: {e}")
        return None

# ─── yfinance fallback ─────────────────────────────────────────────────────────

def yf_get_prices(tickers: List[str]) -> List[dict]:
    """Real-time prices via Yahoo Finance (.JK). Gratis, no limit."""
    yf_syms = [t + ".JK" for t in tickers]
    try:
        data = yf.download(yf_syms, period="2d", interval="1d", progress=False, auto_adjust=True)
        closes = data["Close"].iloc[-1]
        prevs  = data["Close"].iloc[-2] if len(data) > 1 else closes
        vols   = data["Volume"].iloc[-1]
        result = []
        for t in tickers:
            sym   = t + ".JK"
            close = closes.get(sym)
            prev  = prevs.get(sym)
            vol   = vols.get(sym)
            if close is not None and not pd.isna(close):
                chg = ((close - prev) / prev * 100) if (prev and not pd.isna(prev) and prev > 0) else 0.0
                result.append({
                    "ticker": t,
                    "price": float(close),
                    "volume": int(vol) if (vol and not pd.isna(vol)) else 0,
                    "change_pct": round(float(chg), 4),
                })
        return result
    except Exception as e:
        print(f"[yfinance] prices error: {e}")
        return []

def yf_get_historical(ticker: str, limit: int = 60) -> Optional[List[float]]:
    """Historical closes via Yahoo Finance."""
    try:
        data = yf.download(f"{ticker}.JK", period=f"{limit}d", interval="1d",
                           progress=False, auto_adjust=True)
        closes = data["Close"].dropna().tolist()
        return [float(c) for c in closes[-limit:]]
    except Exception as e:
        print(f"[yfinance] historical {ticker}: {e}")
        return None

# ─── Indicators ────────────────────────────────────────────────────────────────

def compute_indicators(closes: List[float]):
    """Returns (ma20, macd_line, signal_line) or (None, None, None)."""
    if len(closes) < 26:
        return None, None, None
    s = pd.Series(closes)
    ma20   = float(s.rolling(20).mean().iloc[-1])
    ema12  = s.ewm(span=12, adjust=False).mean()
    ema26  = s.ewm(span=26, adjust=False).mean()
    macd   = float((ema12 - ema26).iloc[-1])
    signal = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])
    return round(ma20, 2), round(macd, 4), round(signal, 4)

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat(),
            "goapi_key_set": bool(GOAPI_KEY)}

@app.get("/api/settings")
def get_settings():
    return _settings_cache

@app.post("/api/admin/login")
def admin_login(body: AdminLogin):
    if body.password != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Password salah!")
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
    """Live screener LQ45 — GoAPI primary, yfinance fallback. NO DUMMY DATA."""
    source = "goapi"

    # Tickers
    tickers = goapi_get_tickers() or LQ45_STATIC

    # Prices
    prices = goapi_get_prices(tickers)
    if not prices:
        print("[screener] GoAPI limit → fallback yfinance")
        prices = yf_get_prices(tickers)
        source = "yfinance"
        if not prices:
            raise HTTPException(status_code=503, detail="Semua sumber data tidak tersedia")

    # Indicators dari Supabase cache
    indicators_map = {}
    try:
        res = supabase.table("indicators").select("ticker,ma20,macd_line,signal_line").execute()
        for row in (res.data or []):
            indicators_map[row["ticker"]] = row
    except Exception as e:
        print(f"[screener] indicators: {e}")

    result = []
    for item in prices:
        ticker = item["ticker"]
        ind    = indicators_map.get(ticker, {})
        result.append({
            "ticker":     ticker,
            "price":      item["price"],
            "volume":     item["volume"],
            "change_pct": item["change_pct"],
            "ma20":       ind.get("ma20"),
            "macd":       ind.get("macd_line"),
            "signal":     ind.get("signal_line"),
        })

    result.sort(key=lambda x: (x["change_pct"] or 0), reverse=True)
    return {"data": result, "count": len(result), "source": source,
            "timestamp": datetime.utcnow().isoformat()}

@app.post("/api/admin/refresh-stocks")
def refresh_stocks(x_admin_token: str = Header(None)):
    """Refresh prices + hitung indicators → simpan ke Supabase."""
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")

    source  = "goapi"
    tickers = goapi_get_tickers() or LQ45_STATIC
    prices  = goapi_get_prices(tickers)
    if not prices:
        prices = yf_get_prices(tickers)
        source = "yfinance"
        if not prices:
            raise HTTPException(status_code=503, detail="Semua sumber data gagal")

    upserted_stocks = 0
    for item in prices:
        try:
            supabase.table("stocks_data").upsert({
                "ticker": item["ticker"], "price": item["price"],
                "volume": item["volume"], "updated_at": datetime.utcnow().isoformat()
            }).execute()
            upserted_stocks += 1
        except Exception as e:
            print(f"[refresh] stocks {item['ticker']}: {e}")

    upserted_ind = 0
    for ticker in tickers:
        closes = goapi_get_historical(ticker) or yf_get_historical(ticker)
        if closes and len(closes) >= 26:
            ma20, macd, signal = compute_indicators(closes)
            if ma20 is not None:
                try:
                    supabase.table("indicators").upsert({
                        "ticker": ticker, "ma20": ma20, "macd_line": macd,
                        "signal_line": signal, "calculated_at": datetime.utcnow().isoformat()
                    }).execute()
                    upserted_ind += 1
                except Exception as e:
                    print(f"[refresh] indicators {ticker}: {e}")

    return {"success": True, "source": source, "tickers": len(tickers),
            "stocks_updated": upserted_stocks, "indicators_updated": upserted_ind}

@app.get("/api/users")
def get_users(x_admin_token: str = Header(None)):
    if x_admin_token != "admin-ok":
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        res = supabase.table("users").select("id,name,whatsapp,status,created_at").execute()
        users = [{"id": u["id"], "name": u.get("name"), "phone_number": u.get("whatsapp"),
                  "status": u.get("status","Free"), "created_at": u.get("created_at")}
                 for u in (res.data or [])]
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
            raise HTTPException(status_code=404, detail=f"User {body.phone_number} tidak ditemukan")
        return {"success": True, "data": res.data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/debug/goapi-test")
def debug_goapi():
    results = {}
    try:
        r = requests.get(f"{GOAPI_BASE}/index/LQ45/items", headers=GOAPI_HEADERS, timeout=10)
        d = r.json()
        results["goapi_lq45"] = {"http": r.status_code, "status": d.get("status"),
                                  "message": d.get("message","")[:100],
                                  "tickers": len(d.get("data",{}).get("results",[]))}
    except Exception as e:
        results["goapi_lq45"] = {"error": str(e)}
    try:
        r = requests.get(f"{GOAPI_BASE}/prices?symbols=BBCA,BMRI", headers=GOAPI_HEADERS, timeout=10)
        d = r.json()
        results["goapi_prices"] = {"http": r.status_code, "status": d.get("status"),
                                    "message": d.get("message","")[:100],
                                    "count": len(d.get("data",{}).get("results",[]))}
    except Exception as e:
        results["goapi_prices"] = {"error": str(e)}
    try:
        data = yf.download("BBCA.JK", period="2d", interval="1d", progress=False, auto_adjust=True)
        results["yfinance"] = {"status": "ok", "BBCA_price": float(data["Close"].iloc[-1]) if not data.empty else None}
    except Exception as e:
        results["yfinance"] = {"error": str(e)}
    return results
