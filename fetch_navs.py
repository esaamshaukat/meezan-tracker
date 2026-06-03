#!/usr/bin/env python3
"""
Wafrah NAV Fetcher — Daily GitHub Action
Runs on schedule to:
  1. Fetch latest NAVs from MUFAP (tries multiple methods)
  2. Save to meezan/navs with timestamp on EVERY run
  3. Append today's NAV to meezan/portfolio.navHistory
  4. Update meezan/fyHistory rolling FY high/low
"""

import os
import json
import time
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import date, datetime
from io import StringIO

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print('WARNING: beautifulsoup4 not installed')

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print('WARNING: pandas not installed')

# ── CONFIG ─────────────────────────────────────────────────────────────────────
ALL_TRACKED_FUNDS = [
    'Meezan Islamic Fund',
    'Meezan Balanced Fund',
    'Meezan Islamic Income Fund',
    'Meezan Cash Fund',
    'KSE Meezan Index Fund',
    'Al Meezan Mutual Fund',
    'Meezan Sovereign Fund',
    'Meezan Financial Planning Fund of Funds (Moderate)',
]

PLAN_FUNDS = ALL_TRACKED_FUNDS[:4]
MUFAP_URL  = 'https://www.mufap.com.pk/nav-prices-returns.php'
HEADERS    = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.mufap.com.pk/',
}

# ── FIREBASE ───────────────────────────────────────────────────────────────────
def init_firebase():
    # Option 1: full JSON in one secret
    cred_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    # Option 2: three separate secrets (existing GitHub setup)
    elif os.environ.get('FIREBASE_PROJECT_ID'):
        cred_dict = {
            'type': 'service_account',
            'project_id':   os.environ['FIREBASE_PROJECT_ID'],
            'client_email': os.environ['FIREBASE_CLIENT_EMAIL'],
            'private_key':  os.environ['FIREBASE_PRIVATE_KEY'].replace('\\n', '\n'),
            'token_uri':    'https://oauth2.googleapis.com/token',
        }
        cred = credentials.Certificate(cred_dict)
    # Option 3: local file
    elif os.path.exists('serviceAccount.json'):
        cred = credentials.Certificate('serviceAccount.json')
    else:
        raise RuntimeError('No Firebase credentials found.')
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ── HELPERS ────────────────────────────────────────────────────────────────────
def get_current_fy():
    today = date.today()
    if today.month >= 7:
        return f'FY{today.year}-{str(today.year + 1)[2:]}'
    return f'FY{today.year - 1}-{str(today.year)[2:]}'

def match_fund(nav_dict, target_fund):
    if target_fund in nav_dict:
        return nav_dict[target_fund]
    tl = target_fund.lower()
    for k, v in nav_dict.items():
        if tl in k.lower() or k.lower() in tl:
            return v
    return None

# ── MUFAP PARSERS ──────────────────────────────────────────────────────────────
def parse_bs4(html):
    """Parse using BeautifulSoup — more reliable on dynamic HTML."""
    soup = BeautifulSoup(html, 'lxml')
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        if len(rows) < 5:
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(['th', 'td'])]
        fund_col = next((i for i,h in enumerate(headers) if any(k in h for k in ['fund','name','scheme'])), 0)
        nav_col  = next((i for i,h in enumerate(headers) if any(k in h for k in ['nav','offer','repurchase','sale'])), 1)
        result = {}
        for row in rows[1:]:
            cells = row.find_all(['td', 'th'])
            if len(cells) <= max(fund_col, nav_col):
                continue
            name = cells[fund_col].get_text(strip=True)
            if not name or name.lower() in ('fund name','name','scheme name',''):
                continue
            try:
                nav = float(cells[nav_col].get_text(strip=True).replace(',','').strip())
                if nav > 0:
                    result[name] = nav
            except (ValueError, TypeError):
                pass
        if len(result) >= 5:
            return result
    return {}

def parse_pandas(html):
    """Parse using pandas read_html as fallback."""
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        return {}
    for df in tables:
        if len(df) < 5:
            continue
        cols = [str(c).lower().strip() for c in df.columns]
        fc = next((i for i,c in enumerate(cols) if any(k in c for k in ['fund','name','scheme'])), 0)
        nc = next((i for i,c in enumerate(cols) if any(k in c for k in ['nav','offer','repurchase','sale'])), 1 if len(cols)>1 else 0)
        result = {}
        for _, row in df.iterrows():
            name = str(row.iloc[fc]).strip()
            if not name or name.lower() in ('nan','fund name','name','scheme name'):
                continue
            try:
                nav = float(str(row.iloc[nc]).replace(',','').strip())
                if nav > 0:
                    result[name] = nav
            except (ValueError, TypeError):
                pass
        if len(result) >= 5:
            return result
    return {}

def parse_html_navs(html):
    """Try bs4 first, fall back to pandas."""
    if HAS_BS4:
        result = parse_bs4(html)
        if result:
            return result
    if HAS_PANDAS:
        return parse_pandas(html)
    return {}

# ── MUFAP FETCH ────────────────────────────────────────────────────────────────
def fetch_today_navs():
    today    = date.today()
    date_ymd = today.strftime('%Y-%m-%d')
    date_dmy = today.strftime('%d/%m/%Y')

    attempts = [
        # Plain GET — same as visiting MUFAP in browser (returns latest available NAVs)
        lambda: requests.get(MUFAP_URL, headers=HEADERS, timeout=20),
        # POST with today's date
        lambda: requests.post(MUFAP_URL,
            data={'nav_date': date_ymd, 'date': date_ymd, 'filter_date': date_ymd},
            headers=HEADERS, timeout=20),
        lambda: requests.post(MUFAP_URL,
            data={'nav_date': date_dmy, 'date': date_dmy},
            headers=HEADERS, timeout=20),
        lambda: requests.get(MUFAP_URL,
            params={'nav_date': date_ymd},
            headers=HEADERS, timeout=20),
    ]

    for i, attempt in enumerate(attempts):
        try:
            resp = attempt()
            if resp.status_code == 200 and len(resp.content) > 1000:
                result = parse_html_navs(resp.text)
                if result:
                    print(f'    MUFAP fetch succeeded (attempt {i+1})')
                    return result
        except Exception as e:
            print(f'    Attempt {i+1} failed: {e}')
        time.sleep(0.3)

    return {}

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    print('=== Wafrah Daily NAV Fetcher ===')
    db    = init_firebase()
    today = date.today()
    fy    = get_current_fy()
    iso   = today.isoformat()
    now   = datetime.utcnow().isoformat() + 'Z'
    print(f'Date: {today} | FY: {fy}')

    # ── Step 1: Fetch NAVs from MUFAP ─────────────────────────────────────────
    print('\n[1] Fetching NAVs from MUFAP...')
    raw_navs = fetch_today_navs()
    if raw_navs:
        print(f'    Got {len(raw_navs)} funds from MUFAP')
    else:
        print('    No data from MUFAP')

    # Match to tracked funds
    tracked = {}
    for fund in ALL_TRACKED_FUNDS:
        nav = match_fund(raw_navs, fund)
        if nav:
            tracked[fund] = nav

    # ── Step 2: Always write to meezan/navs ───────────────────────────────────
    # Write on EVERY run so the timestamp always reflects last check time
    print(f'\n[2] Writing to meezan/navs (always, every run)...')
    navs_ref = db.collection('meezan').document('navs')
    if tracked:
        navs_ref.set({
            'navs':        tracked,
            'success':     True,
            'updatedAt':   now,
            'lastChecked': now,
        })
        print(f'    Saved {len(tracked)} NAVs with timestamp {now}')
        for f, v in tracked.items():
            print(f'      {f}: {v}')
    else:
        # No new data — write lastChecked so app knows the script ran
        existing = navs_ref.get()
        if existing.exists:
            navs_ref.update({'lastChecked': now, 'success': False})
            print(f'    No NAVs — updated lastChecked only: {now}')
        else:
            navs_ref.set({'navs': {}, 'success': False, 'lastChecked': now, 'updatedAt': now})
            print(f'    No NAVs — created empty doc with timestamp: {now}')

    # ── Step 3: Update navHistory ──────────────────────────────────────────────
    if tracked:
        print(f'\n[3] Updating navHistory for {iso}...')
        try:
            port_ref = db.collection('meezan').document('portfolio')
            port_doc = port_ref.get()
            nav_history = port_doc.to_dict().get('navHistory', {}) if port_doc.exists else {}
            if iso not in nav_history:
                nav_history[iso] = {}
            for fund, nav in tracked.items():
                if nav > nav_history[iso].get(fund, 0):
                    nav_history[iso][fund] = nav
            port_ref.update({'navHistory': nav_history})
            print(f'    navHistory updated. Total dates: {len(nav_history)}')
        except Exception as e:
            print(f'    WARNING: navHistory update failed: {e}')

    # ── Step 4: Update fyHistory ───────────────────────────────────────────────
    print(f'\n[4] Updating FY high/low for {fy}...')
    fy_ref = db.collection('meezan').document('fyHistory')
    fy_doc = fy_ref.get()
    all_fy  = fy_doc.to_dict() if fy_doc.exists else {}
    cur_fy  = all_fy.get(fy, {})

    for fund in ALL_TRACKED_FUNDS:
        nav = match_fund(tracked, fund)
        if not nav or nav <= 0:
            continue
        if fund not in cur_fy:
            cur_fy[fund] = {'high': nav, 'low': nav}
        else:
            if nav > cur_fy[fund]['high']: cur_fy[fund]['high'] = nav
            if nav < cur_fy[fund]['low']:  cur_fy[fund]['low']  = nav

    all_fy[fy] = cur_fy
    fy_ref.set(all_fy)

    print('    FY high/low:')
    for fund in PLAN_FUNDS:
        if fund in cur_fy:
            s = cur_fy[fund]
            print(f'      {fund}: Low={s["low"]:.4f}  High={s["high"]:.4f}')

    print('\nDone.')

if __name__ == '__main__':
    main()
