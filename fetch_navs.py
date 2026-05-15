import os
import requests
import json
from bs4 import BeautifulSoup
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# ── Firebase init using GitHub secrets
cred = credentials.Certificate({
    "type": "service_account",
    "project_id": os.environ["FIREBASE_PROJECT_ID"],
    "client_email": os.environ["FIREBASE_CLIENT_EMAIL"],
    "private_key": os.environ["FIREBASE_PRIVATE_KEY"].replace("\\n", "\n"),
    "token_uri": "https://oauth2.googleapis.com/token",
})
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Funds to track
KNOWN_FUNDS = [
    "Meezan Islamic Fund",
    "KSE Meezan Index Fund",
    "Al Meezan Mutual Fund",
    "Meezan Balanced Fund",
    "Meezan Cash Fund",
    "Meezan Islamic Income Fund",
    "Meezan Sovereign Fund",
    "Meezan Financial Planning Fund of Funds (Moderate)"
]

# ── Fetch from MUFAP
url = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=1"
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

try:
    r = requests.get(url, headers=headers, timeout=20)
    soup = BeautifulSoup(r.text, 'html.parser')
    table = soup.find('table')
    navs = {}

    if table:
        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) >= 7:
                fund_name = cells[2].get_text(strip=True)
                nav_text  = cells[6].get_text(strip=True)
                try:
                    nav = float(nav_text.replace(',', '').strip())
                    if nav > 0:
                        for known in KNOWN_FUNDS:
                            if known.lower() in fund_name.lower():
                                navs[known] = nav
                                break
                except:
                    continue

    # ── Write to Firestore
    db.collection('meezan').document('navs').set({
        'navs': navs,
        'updatedAt': datetime.utcnow().isoformat(),
        'success': len(navs) > 0
    })
    print(f"✅ Updated {len(navs)} NAVs: {navs}")

except Exception as e:
    db.collection('meezan').document('navs').set({
        'navs': {},
        'updatedAt': datetime.utcnow().isoformat(),
        'success': False,
        'error': str(e)
    })
    print(f"❌ Error: {e}")
