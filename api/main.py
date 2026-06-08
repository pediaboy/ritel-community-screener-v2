"""
RITELCOMMUNITY.ID SCREENER — Backend API
Uses Supabase REST API directly (no supabase-py dependency)
"""
from http.server import BaseHTTPRequestHandler
import json, os, re, requests, pandas as pd
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

SUPABASE_URL  = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")
GOAPI_KEY     = os.getenv("GOAPI_KEY", "")
ADMIN_SECRET  = os.getenv("ADMIN_SECRET", "pedia123")

SYMBOLS = "BBRI,BBCA,TLKM,GOTO,BMRI,ASII,UNVR,BYAN,MDKA,INDF,ICBP,PGAS,ANTM,KLBF,HMSP,BRIS,EXCL,CPIN,EMTK,MIKA"

# ===== SUPABASE REST HELPERS =====
def supa_headers(extra=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    if extra: h.update(extra)
    return h

def supa_select(table, params="", headers_extra=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    r = requests.get(url, headers=supa_headers(headers_extra), timeout=10)
    return r.json()

def supa_upsert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=supa_headers({"Prefer":"resolution=merge-duplicates,return=minimal"}), json=data, timeout=10)
    return r.status_code

def supa_update(table, match_key, match_val, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match_key}=eq.{match_val}"
    r = requests.patch(url, headers=supa_headers({"Prefer":"return=minimal"}), json=data, timeout=10)
    return r.status_code

def supa_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=supa_headers({"Prefer":"return=minimal"}), json=data, timeout=10)
    return r.status_code

# ===== INDICATORS =====
def calc_indicators(prices):
    if len(prices) < 26: return None, None, None
    s = pd.Series(prices)
    ma20 = round(float(s.rolling(20).mean().iloc[-1]), 2)
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    return ma20, round(float(macd_line.iloc[-1]), 4), round(float(signal.iloc[-1]), 4)

# ===== RESPONSE HELPERS =====
def cors():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-admin-secret",
        "Content-Type": "application/json"
    }

def ok(data): return {"statusCode": 200, "headers": cors(), "body": json.dumps(data)}
def err(msg, code=500): return {"statusCode": code, "headers": cors(), "body": json.dumps({"error": msg})}

# ===== ROUTE HANDLER =====
def handle(method, path, query, headers, body):
    admin = headers.get("x-admin-secret","") == ADMIN_SECRET
    if method == "OPTIONS": return {"statusCode":200,"headers":cors(),"body":""}

    # HEALTH
    if path == "/api/health":
        return ok({"status":"ok","time":datetime.now(timezone.utc).isoformat(),"supabase":bool(SUPABASE_URL),"goapi":bool(GOAPI_KEY)})

    # STOCKS
    if path == "/api/stocks":
        try:
            limit = min(int(query.get("limit",["20"])[0]),50)
            data = supa_select("stocks_data", f"order=updated_at.desc&limit={limit}")
            if isinstance(data, list):
                return ok({"data": data, "count": len(data)})
            return ok({"data":[], "count":0, "error": str(data)})
        except Exception as e:
            return ok({"data":[], "count":0, "error": str(e)})

    # SETTINGS
    if path == "/api/settings" and method == "GET":
        try:
            data = supa_select("global_settings", "id=eq.1&limit=1")
            return ok(data[0] if isinstance(data,list) and data else {})
        except Exception as e:
            return err(str(e))

    # CHART
    m = re.match(r'^/api/chart/([A-Za-z]+)$', path)
    if m:
        ticker = m.group(1).upper()
        try:
            url = f"https://api.goapi.io/stock/idx/prices?symbols={ticker}"
            r = requests.get(url, headers={"Authorization":GOAPI_KEY,"accept":"application/json"}, timeout=12)
            raw = r.json()
            symbol_data = raw.get("data",{}).get(ticker,{})
            prices_raw = symbol_data.get("prices",[])[-60:]
            labels = [p.get("date","") for p in prices_raw]
            closes = [float(p.get("close",0)) for p in prices_raw]
            return ok({"ticker":ticker,"labels":labels,"prices":closes})
        except Exception as e:
            import math, random
            lbs = [f"D{i+1}" for i in range(60)]
            cls = [round(4200+math.sin(i*0.3)*250+random.uniform(-80,80),0) for i in range(60)]
            return ok({"ticker":ticker,"labels":lbs,"prices":cls,"note":"demo_data"})

    # ADMIN: FORCE SCRAPE
    if path == "/api/admin/scrape" and method == "POST":
        if not admin: return err("Unauthorized", 401)
        try:
            url = f"https://api.goapi.io/stock/idx/prices?symbols={SYMBOLS}"
            r = requests.get(url, headers={"Authorization":GOAPI_KEY,"accept":"application/json"}, timeout=25)
            raw = r.json()
            updated, rows = [], []
            for ticker, info in raw.get("data",{}).items():
                pl = info.get("prices",[])
                closes = [float(p.get("close",0)) for p in pl if p.get("close")]
                if not closes: continue
                latest = pl[-1] if pl else {}
                price = float(latest.get("close",0))
                volume = float(latest.get("volume",0))
                open_p = float(latest.get("open",price))
                change_pct = round(((price-open_p)/open_p)*100,2) if open_p else 0
                ma20, macd, signal = calc_indicators(closes)
                rows.append({"ticker":ticker,"price":price,"volume":volume,"change_pct":change_pct,
                              "ma20":ma20,"macd":macd,"macd_signal":signal,
                              "updated_at":datetime.now(timezone.utc).isoformat()})
                updated.append(ticker)
            # Upsert batch
            if rows: supa_upsert("stocks_data", rows)
            return ok({"success":True,"updated":updated,"count":len(updated)})
        except Exception as e:
            return err(str(e))

    # ADMIN: UPDATE SETTINGS
    if path == "/api/admin/settings" and method == "PUT":
        if not admin: return err("Unauthorized", 401)
        try:
            allowed = ["vip_price","bank_account","wa_channel","wa_group","ig_link"]
            clean = {k:v for k,v in body.items() if k in allowed}
            clean["id"] = 1
            supa_upsert("global_settings", clean)
            return ok({"success":True})
        except Exception as e:
            return err(str(e))

    # ADMIN: UPGRADE USER
    if path == "/api/admin/upgrade-user" and method == "POST":
        if not admin: return err("Unauthorized", 401)
        try:
            phone = body.get("phone_number","").strip()
            if not phone: return err("phone_number required", 400)
            existing = supa_select("users", f"phone_number=eq.{phone}&limit=1")
            if isinstance(existing,list) and existing:
                supa_update("users","phone_number",phone,{"status":"VIP"})
                action = "upgraded"
            else:
                supa_insert("users",{"phone_number":phone,"name":body.get("name","User"),"status":"VIP"})
                action = "created_vip"
            return ok({"success":True,"action":action,"phone":phone})
        except Exception as e:
            return err(str(e))

    # ADMIN: DOWNGRADE USER
    if path == "/api/admin/downgrade-user" and method == "POST":
        if not admin: return err("Unauthorized", 401)
        try:
            phone = body.get("phone_number","").strip()
            supa_update("users","phone_number",phone,{"status":"Free"})
            return ok({"success":True,"phone":phone,"status":"Free"})
        except Exception as e:
            return err(str(e))

    # ADMIN: LIST USERS
    if path == "/api/admin/users" and method == "GET":
        if not admin: return err("Unauthorized", 401)
        try:
            data = supa_select("users","order=created_at.desc&limit=100")
            return ok({"data": data if isinstance(data,list) else []})
        except Exception as e:
            return err(str(e))

    # SETUP (create default settings row)
    if path == "/api/setup":
        secret = query.get("secret",[""])[0]
        if secret != ADMIN_SECRET: return err("Unauthorized", 401)
        try:
            supa_upsert("global_settings",{"id":1,"vip_price":99000,"bank_account":"BCA 1234567890 a.n. Thirafi Thariq","wa_channel":"#","wa_group":"#","ig_link":"#"})
            return ok({"success":True,"message":"Settings row created/updated!"})
        except Exception as e:
            return err(str(e)+". Jalankan supabase_schema.sql di Supabase SQL Editor dulu.")

    return err("Not found: "+path, 404)


# ===== VERCEL HANDLER =====
class handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def _run(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        hdrs = {k.lower(): self.headers[k] for k in self.headers}
        body = {}
        if method in ("POST","PUT"):
            try:
                length = int(self.headers.get("Content-Length",0))
                if length: body = json.loads(self.rfile.read(length))
            except: pass
        response = handle(method, parsed.path, query, hdrs, body)
        self.send_response(response["statusCode"])
        for k,v in response["headers"].items(): self.send_header(k,v)
        self.end_headers()
        self.wfile.write(response["body"].encode())

    def do_GET(self):     self._run("GET")
    def do_POST(self):    self._run("POST")
    def do_PUT(self):     self._run("PUT")
    def do_OPTIONS(self): self._run("OPTIONS")
