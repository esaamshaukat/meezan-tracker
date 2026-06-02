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

def is_valid_nav(val):
    """NAVs for these funds are between 10 and 10000"""
    try:
        n = float(val.replace(',', '').strip())
        return 10 < n < 10000
    except:
        return False

def try_parse(val):
    try:
        return float(val.replace(',', '').strip())
    except:
        return None

# ── Try multiple MUFAP URLs in case one changes
URLS = [
    "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=1",
    "https://www.mufap.com.pk/nav_returns_data.php?tab=daily_return",
]

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

navs = {}
error_log = []

for url in URLS:
    if len(navs) >= len(KNOWN_FUNDS):
        break
    try:
        print(f"Trying: {url}")
        r = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')

        # Try every table on the page
        for table in soup.find_all('table'):
            rows = table.find_all('tr')

            # ── Detect header row to find NAV column index
            nav_col = None
            name_col = None
            for row in rows[:5]:  # check first 5 rows for header
                cells = row.find_all(['th', 'td'])
                for i, cell in enumerate(cells):
                    txt = cell.get_text(strip=True).lower()
                    if 'nav' in txt and nav_col is None:
                        nav_col = i
                    if 'fund' in txt and 'name' in txt and name_col is None:
                        name_col = i

            # ── Scan every row for fund matches
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 3:
                    continue

                # Get all cell texts
                texts = [c.get_text(strip=True) for c in cells]
                row_text = ' '.join(texts).lower()

                # Check if this row contains one of our funds
                matched_fund = None
                for fund in KNOWN_FUNDS:
                    if fund.lower() in row_text:
                        matched_fund = fund
                        break

                if not matched_fund or matched_fund in navs:
                    continue

                # Try detected NAV column first
                if nav_col is not None and nav_col < len(texts):
                    val = try_parse(texts[nav_col])
                    if val and 10 < val < 10000:
                        navs[matched_fund] = val
                        print(f"  ✓ {matched_fund}: {val} (col {nav_col})")
                        continue

                # Fallback: scan all cells for a valid NAV-like number
                for i, txt in enumerate(texts):
                    val = try_parse(txt)
                    if val and 10 < val < 10000:
                        navs[matched_fund] = val
                        print(f"  ✓ {matched_fund}: {val} (col {i} fallback)")
                        break

        if len(navs) > 0:
            print(f"Found {len(navs)} NAVs from {url}")
            break

    except Exception as e:
        error_log.append(str(e))
        print(f"  Error with {url}: {e}")
        continue

# ── Write to Firestore
try:
    db.collection('meezan').document('navs').set({
        'navs': navs,
        'updatedAt': datetime.utcnow().isoformat(),
        'success': len(navs) > 0,
        'error': '; '.join(error_log) if error_log else None
    })
    print(f"\n✅ Written to Firestore: {len(navs)} NAVs")
    print(json.dumps(navs, indent=2))
except Exception as e:
    print(f"❌ Firestore write failed: {e}")
