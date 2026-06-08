from http.server import BaseHTTPRequestHandler
import json, os, re
from urllib.parse import urlparse, parse_qs
import requests
import pandas as pd
from datetime import datetime, timezone

try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except:
    SUPABASE_AVAILABLE = False

SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")
GOAPI_KEY     = os.getenv("GOAPI_KEY", "")
ADMIN_SECRET  = os.getenv("ADMIN_SECRET", "pedia123")

SYMBOLS = "BBRI,BBCA,TLKM,GOTO,BMRI,ASII,UNVR,BYAN,MDKA,INDF,ICBP,PGAS,ANTM,KLBF,HMSP,BRIS,EXCL,CPIN,EMTK,MIKA"

def get_db():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def calc_indicators(prices):
    if len(prices) < 26:
        return None, None, None
    s = pd.Series(prices)
    ma20 = round(float(s.rolling(20).mean().iloc[-1]), 2) if len(prices)>=20 else None
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal    = macd_line.ewm(span=9, adjust=False).mean()
    return ma20, round(float(macd_line.iloc[-1]),4), round(float(signal.iloc[-1]),4)

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-admin-secret",
        "Content-Type": "application/json"
    }

def json_response(data, status=200):
    return {"statusCode": status, "headers": cors_headers(), "body": json.dumps(data)}

def handle_request(method, path, query, headers, body):
    admin = headers.get("x-admin-secret","") == ADMIN_SECRET

    # CORS preflight
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": cors_headers(), "body": ""}

    # ===== HEALTH =====
    if path == "/api/health":
        return json_response({"status":"ok","time":datetime.now(timezone.utc).isoformat()})

    # ===== STOCKS =====
    if path == "/api/stocks":
        try:
            db = get_db()
            limit = min(int(query.get("limit",["20"])[0]), 50)
            res = db.table("stocks_data").select("*").order("updated_at", desc=True).limit(limit).execute()
            return json_response({"data": res.data, "count": len(res.data)})
        except Exception as e:
            return json_response({"data":[], "count":0, "error":str(e)})

    # ===== SETTINGS =====
    if path == "/api/settings" and method == "GET":
        try:
            db = get_db()
            res = db.table("global_settings").select("*").eq("id",1).execute()
            return json_response(res.data[0] if res.data else {})
        except Exception as e:
            return json_response({"error":str(e)})

    # ===== CHART =====
    m = re.match(r'^/api/chart/([A-Z]+)$', path.upper())
    if m:
        ticker = m.group(1)
        try:
            url = f"https://api.goapi.io/stock/idx/prices?symbols={ticker}"
            r = requests.get(url, headers={"Authorization": GOAPI_KEY, "accept":"application/json"}, timeout=10)
            raw = r.json()
            symbol_data = raw.get("data",{}).get(ticker,{})
            prices_raw = symbol_data.get("prices",[])
            labels = [p.get("date","") for p in prices_raw[-60:]]
            closes = [float(p.get("close",0)) for p in prices_raw[-60:]]
            return json_response({"ticker":ticker,"labels":labels,"prices":closes})
        except Exception as e:
            import math, random
            base = 4200
            lbs = [f"D{i+1}" for i in range(60)]
            cls = [round(base+math.sin(i*0.3)*200+random.uniform(-50,50),0) for i in range(60)]
            return json_response({"ticker":ticker,"labels":lbs,"prices":cls,"note":"demo"})

    # ===== ADMIN: SCRAPE =====
    if path == "/api/admin/scrape" and method == "POST":
        if not admin:
            return json_response({"error":"Unauthorized"}, 401)
        try:
            db = get_db()
            url = f"https://api.goapi.io/stock/idx/prices?symbols={SYMBOLS}"
            r = requests.get(url, headers={"Authorization":GOAPI_KEY,"accept":"application/json"}, timeout=25)
            raw = r.json()
            updated = []
            for ticker, info in raw.get("data",{}).items():
                prices_list = info.get("prices",[])
                closes = [float(p.get("close",0)) for p in prices_list if p.get("close")]
                if not closes: continue
                latest = prices_list[-1] if prices_list else {}
                price  = float(latest.get("close",0))
                volume = float(latest.get("volume",0))
                open_p = float(latest.get("open",price))
                change_pct = round(((price-open_p)/open_p)*100,2) if open_p else 0
                ma20, macd, signal = calc_indicators(closes)
                row = {"ticker":ticker,"price":price,"volume":volume,"change_pct":change_pct,
                       "ma20":ma20,"macd":macd,"macd_signal":signal,
                       "updated_at":datetime.now(timezone.utc).isoformat()}
                db.table("stocks_data").upsert(row, on_conflict="ticker").execute()
                updated.append(ticker)
            return json_response({"success":True,"updated":updated,"count":len(updated)})
        except Exception as e:
            return json_response({"error":str(e)}, 500)

    # ===== ADMIN: UPDATE SETTINGS =====
    if path == "/api/admin/settings" and method == "PUT":
        if not admin:
            return json_response({"error":"Unauthorized"}, 401)
        try:
            db = get_db()
            allowed = ["vip_price","bank_account","wa_channel","wa_group","ig_link"]
            clean = {k:v for k,v in body.items() if k in allowed}
            db.table("global_settings").upsert({**clean,"id":1}, on_conflict="id").execute()
            return json_response({"success":True})
        except Exception as e:
            return json_response({"error":str(e)}, 500)

    # ===== ADMIN: UPGRADE USER =====
    if path == "/api/admin/upgrade-user" and method == "POST":
        if not admin:
            return json_response({"error":"Unauthorized"}, 401)
        try:
            db = get_db()
            phone = body.get("phone_number","").strip()
            if not phone:
                return json_response({"error":"phone_number required"}, 400)
            existing = db.table("users").select("id").eq("phone_number",phone).execute()
            if existing.data:
                db.table("users").update({"status":"VIP"}).eq("phone_number",phone).execute()
                action = "upgraded"
            else:
                db.table("users").insert({"phone_number":phone,"name":body.get("name","User"),"status":"VIP"}).execute()
                action = "created_vip"
            return json_response({"success":True,"action":action,"phone":phone})
        except Exception as e:
            return json_response({"error":str(e)}, 500)

    # ===== ADMIN: DOWNGRADE USER =====
    if path == "/api/admin/downgrade-user" and method == "POST":
        if not admin:
            return json_response({"error":"Unauthorized"}, 401)
        try:
            db = get_db()
            phone = body.get("phone_number","").strip()
            db.table("users").update({"status":"Free"}).eq("phone_number",phone).execute()
            return json_response({"success":True,"phone":phone,"status":"Free"})
        except Exception as e:
            return json_response({"error":str(e)}, 500)

    # ===== ADMIN: LIST USERS =====
    if path == "/api/admin/users" and method == "GET":
        if not admin:
            return json_response({"error":"Unauthorized"}, 401)
        try:
            db = get_db()
            res = db.table("users").select("*").order("created_at",desc=True).execute()
            return json_response({"data":res.data})
        except Exception as e:
            return json_response({"error":str(e)}, 500)

    # ===== SETUP =====
    if path == "/api/setup":
        secret = query.get("secret",[""])[0]
        if secret != ADMIN_SECRET:
            return json_response({"error":"Unauthorized"}, 401)
        try:
            db = get_db()
            db.table("global_settings").upsert({"id":1,"vip_price":99000,"bank_account":"BCA 1234567890 a.n. Thirafi Thariq","wa_channel":"#","wa_group":"#","ig_link":"#"}, on_conflict="id").execute()
            return json_response({"success":True,"message":"Database ready!"})
        except Exception as e:
            return json_response({"error":str(e),"hint":"Jalankan supabase_schema.sql di Supabase SQL Editor"})

    return json_response({"error":"Not Found","path":path}, 404)


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logs

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        
        # Headers
        hdrs = {}
        for k in self.headers:
            hdrs[k.lower()] = self.headers[k]
        
        # Body
        body = {}
        if method in ("POST","PUT"):
            try:
                length = int(self.headers.get("Content-Length",0))
                if length:
                    raw_body = self.rfile.read(length)
                    body = json.loads(raw_body)
            except:
                pass
        
        response = handle_request(method, path, query, hdrs, body)
        
        self.send_response(response.get("statusCode",200))
        for k, v in response.get("headers",{}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(response.get("body","").encode())

    def do_GET(self):    self._handle("GET")
    def do_POST(self):   self._handle("POST")
    def do_PUT(self):    self._handle("PUT")
    def do_OPTIONS(self): self._handle("OPTIONS")
