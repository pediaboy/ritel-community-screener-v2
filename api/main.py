from fastapi import FastAPI, HTTPException, Header, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os, requests, pandas as pd
from datetime import datetime, timezone
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

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
GOAPI_KEY    = os.getenv("GOAPI_KEY", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "pedia123")

SYMBOLS = "BBRI,BBCA,TLKM,GOTO,BMRI,ASII,UNVR,BYAN,MDKA,INDF,ICBP,PGAS,ANTM,KLBF,HMSP,BRIS,EXCL,CPIN,EMTK,MIKA"

def get_db() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def calc_indicators(prices: list[float]):
    """Hitung MA20 dan MACD dari list harga pakai pandas murni"""
    if len(prices) < 26:
        return None, None, None
    s = pd.Series(prices)
    ma20 = round(float(s.rolling(20).mean().iloc[-1]), 2)
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal    = macd_line.ewm(span=9, adjust=False).mean()
    macd_val  = round(float(macd_line.iloc[-1]), 4)
    signal_val = round(float(signal.iloc[-1]), 4)
    return ma20, macd_val, signal_val

def verify_admin(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ==============================
# PUBLIC ENDPOINTS
# ==============================

@app.get("/api/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

@app.get("/api/stocks")
def get_stocks(limit: int = Query(20, le=50)):
    """Ambil data saham dari Supabase"""
    try:
        db = get_db()
        res = db.table("stocks_data").select("*").order("updated_at", desc=True).limit(limit).execute()
        return {"data": res.data, "count": len(res.data)}
    except Exception as e:
        return {"data": [], "count": 0, "error": str(e)}

@app.get("/api/settings")
def get_settings():
    """Ambil global settings (harga VIP, rekening, dll)"""
    try:
        db = get_db()
        res = db.table("global_settings").select("*").eq("id", 1).single().execute()
        return res.data
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/chart/{ticker}")
def get_chart(ticker: str):
    """Return data historis untuk chart.js"""
    try:
        url = f"https://api.goapi.io/stock/idx/prices?symbols={ticker.upper()}"
        headers = {"Authorization": GOAPI_KEY, "accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=10)
        raw = r.json()
        
        symbol_data = raw.get("data", {}).get(ticker.upper(), {})
        prices_raw = symbol_data.get("prices", [])
        
        labels, closes = [], []
        for p in prices_raw[-60:]:
            labels.append(p.get("date",""))
            closes.append(float(p.get("close", 0)))
        
        return {"ticker": ticker.upper(), "labels": labels, "prices": closes}
    except Exception as e:
        # Return dummy data kalau API gagal
        import random, math
        base = 4200
        labels = [f"2025-{str(i%12+1).zfill(2)}-{str(i%28+1).zfill(2)}" for i in range(60)]
        closes = [round(base + math.sin(i*0.3)*200 + random.uniform(-50,50), 0) for i in range(60)]
        return {"ticker": ticker.upper(), "labels": labels, "prices": closes, "note": "dummy"}

# ==============================
# ADMIN ENDPOINTS (protected)
# ==============================

@app.post("/api/admin/scrape")
def force_scrape(_: bool = Depends(verify_admin)):
    """Force fetch dari GoAPI dan simpan ke Supabase"""
    try:
        db = get_db()
        url = f"https://api.goapi.io/stock/idx/prices?symbols={SYMBOLS}"
        headers = {"Authorization": GOAPI_KEY, "accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=20)
        raw = r.json()
        
        updated = []
        data_map = raw.get("data", {})
        
        for ticker, info in data_map.items():
            prices_list = info.get("prices", [])
            closes = [float(p.get("close", 0)) for p in prices_list if p.get("close")]
            
            if not closes:
                continue
            
            latest = prices_list[-1] if prices_list else {}
            price  = float(latest.get("close", 0))
            volume = float(latest.get("volume", 0))
            open_p = float(latest.get("open", price))
            change_pct = round(((price - open_p) / open_p) * 100, 2) if open_p else 0
            
            ma20, macd, signal = calc_indicators(closes)
            
            row = {
                "ticker": ticker,
                "price": price,
                "volume": volume,
                "change_pct": change_pct,
                "ma20": ma20,
                "macd": macd,
                "macd_signal": signal,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            db.table("stocks_data").upsert(row, on_conflict="ticker").execute()
            updated.append(ticker)
        
        return {"success": True, "updated": updated, "count": len(updated)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/admin/settings")
def update_settings(body: dict, _: bool = Depends(verify_admin)):
    """Update global settings (harga VIP, rekening, dll)"""
    try:
        db = get_db()
        allowed = ["vip_price", "bank_account", "wa_channel", "wa_group", "ig_link"]
        clean = {k: v for k, v in body.items() if k in allowed}
        db.table("global_settings").update(clean).eq("id", 1).execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/upgrade-user")
def upgrade_user(body: dict, _: bool = Depends(verify_admin)):
    """Upgrade user status ke VIP by phone number"""
    try:
        db = get_db()
        phone = body.get("phone_number", "").strip()
        if not phone:
            raise HTTPException(status_code=400, detail="phone_number required")
        
        # Cek apakah user ada
        existing = db.table("users").select("*").eq("phone_number", phone).execute()
        if existing.data:
            db.table("users").update({"status": "VIP"}).eq("phone_number", phone).execute()
            action = "upgraded"
        else:
            db.table("users").insert({"phone_number": phone, "name": body.get("name","User"), "status": "VIP"}).execute()
            action = "created_vip"
        
        return {"success": True, "action": action, "phone": phone}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/downgrade-user")
def downgrade_user(body: dict, _: bool = Depends(verify_admin)):
    try:
        db = get_db()
        phone = body.get("phone_number", "").strip()
        db.table("users").update({"status": "Free"}).eq("phone_number", phone).execute()
        return {"success": True, "phone": phone, "status": "Free"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/users")
def list_users(_: bool = Depends(verify_admin)):
    try:
        db = get_db()
        res = db.table("users").select("*").order("created_at", desc=True).execute()
        return {"data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Vercel handler
from mangum import Mangum
handler = Mangum(app)
