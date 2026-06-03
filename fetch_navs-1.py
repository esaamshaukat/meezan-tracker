#!/usr/bin/env python3
"""
Wafrah NAV Fetcher — GitHub Action Script
Runs daily at 6PM PKT to:
  1. Fetch latest NAVs from MUFAP
  2. Track true FY high/low per fund in Firestore  (meezan/fyHistory)
  3. Update meezan/navs with today's prices for the app
"""

import os
import sys
import json
import time
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import date, timedelta, datetime
from io import StringIO

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print('WARNING: pandas not installed — HTML table parsing unavailable')

# ── CONFIG ────────────────────────────────────────────────────────────────────
PLAN_FUNDS = [
    'Meezan Islamic Fund',
    'Meezan Balanced Fund',
    'Meezan Islamic Income Fund',
    'Meezan Cash Fund',
]

ALL_TRACKED_FUNDS = PLAN_FUNDS + [
    'KSE Meezan Index Fund',
    'Al Meezan Mutual Fund',
    'Meezan Sovereign Fund',
    'Meezan Financial Planning Fund of Funds (Moderate)',
]

MUFAP_NAV_URL = 'https://www.mufap.com.pk/nav-prices-returns.php'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.mufap.com.pk/',
}

# ── FIREBASE ──────────────────────────────────────────────────────────────────
def init_firebase():
    # Option 1: full JSON via FIREBASE_SERVICE_ACCOUNT
    cred_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    # Option 2: separate env vars (existing GitHub secrets)
    elif os.environ.get('FIREBASE_PROJECT_ID'):
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id":   os.environ['FIREBASE_PROJECT_ID'],
            "client_email": os.environ['FIREBASE_CLIENT_EMAIL'],
            "private_key":  os.environ['FIREBASE_PRIVATE_KEY'].replace('\\n', '\n'),
            "token_uri":    "https://oauth2.googleapis.com/token",
        })
    # Option 3: local service account file
    elif os.path.exists('serviceAccount.json'):
        cred = credentials.Certificate('serviceAccount.json')
    else:
        raise RuntimeError('No Firebase credentials found. Set FIREBASE_SERVICE_ACCOUNT or FIREBASE_PROJECT_ID env vars.')
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ── DATE / FY HELPERS ─────────────────────────────────────────────────────────
def get_current_fy():
    today = date.today()
    if today.month >= 7:
        return f'FY{today.year}-{str(today.year + 1)[2:]}'
    return f'FY{today.year - 1}-{str(today.year)[2:]}'

def get_fy_start(fy_str):
    year = int(fy_str[2:6])
    return date(year, 7, 1)

def business_days_between(start, end):
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon–Fri only
            yield current
        current += timedelta(days=1)

# ── MUFAP FETCHER ─────────────────────────────────────────────────────────────
def fetch_navs_for_date(target_date):
    """
    Fetch NAV data from MUFAP for a specific date.
    Returns dict: { fund_name: nav_float }
    """
    date_str = target_date.strftime('%Y-%m-%d')
    date_str2 = target_date.strftime('%d/%m/%Y')

    attempts = [
        # Try POST with various date param names
        lambda: requests.post(MUFAP_NAV_URL,
            data={'nav_date': date_str, 'date': date_str, 'filter_date': date_str},
            headers=HEADERS, timeout=20),
        lambda: requests.post(MUFAP_NAV_URL,
            data={'nav_date': date_str2, 'date': date_str2},
            headers=HEADERS, timeout=20),
        # Try GET
        lambda: requests.get(MUFAP_NAV_URL,
            params={'nav_date': date_str},
            headers=HEADERS, timeout=20),
    ]

    for attempt in attempts:
        try:
            resp = attempt()
            if resp.status_code == 200 and len(resp.content) > 1000:
                result = parse_html_navs(resp.text)
                if result:
                    return result
        except Exception:
            pass
        time.sleep(0.3)

    return {}

def parse_html_navs(html):
    """Parse HTML from MUFAP, auto-detecting fund name and NAV columns."""
    if not HAS_PANDAS:
        return {}
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        return {}

    for df in tables:
        if len(df) < 3:
            continue

        cols_lower = [str(c).lower().strip() for c in df.columns]

        fund_col = next((i for i, c in enumerate(cols_lower)
                         if any(k in c for k in ['fund', 'name', 'scheme', 'amc'])), None)
        nav_col  = next((i for i, c in enumerate(cols_lower)
                         if any(k in c for k in ['nav', 'offer price', 'repurchase', 'sale price'])), None)

        # Fallback: if column headers not found, try first two columns
        if fund_col is None:
            fund_col = 0
        if nav_col is None:
            nav_col = 1 if len(df.columns) > 1 else 0

        result = {}
        for _, row in df.iterrows():
            name = str(row.iloc[fund_col]).strip()
            if not name or name.lower() in ('nan', 'fund name', 'name', 'scheme name'):
                continue
            try:
                nav = float(str(row.iloc[nav_col]).replace(',', '').strip())
                if nav > 0:
                    result[name] = nav
            except (ValueError, TypeError):
                pass

        if len(result) >= 5:
            return result

    return {}

def match_fund(nav_dict, target_fund):
    """Find NAV for target_fund using exact then partial matching."""
    if target_fund in nav_dict:
        return nav_dict[target_fund]
    target_lower = target_fund.lower()
    for k, v in nav_dict.items():
        if target_lower in k.lower() or k.lower() in target_lower:
            return v
    return None

# ── HISTORY BUILDER ───────────────────────────────────────────────────────────
def build_fy_history(fy, existing_fund_data):
    """
    Fetch historical NAV data for every business day of the FY.
    Only fetches dates not already recorded (incremental).
    Returns updated fund high/low dict.
    """
    fy_start = get_fy_start(fy)
    today    = date.today()
    history  = dict(existing_fund_data)
    fetched  = history.pop('_fetched_dates', {})

    all_days = list(business_days_between(fy_start, today))
    missing  = [d for d in all_days if d.isoformat() not in fetched]

    print(f'  {len(all_days)} business days in FY, {len(missing)} not yet fetched')

    for i, d in enumerate(missing):
        key = d.isoformat()
        print(f'  [{i+1}/{len(missing)}] {key}…', end=' ', flush=True)

        navs = fetch_navs_for_date(d)
        if not navs:
            print('no data — skipping')
            fetched[key] = False
            time.sleep(1)
            continue

        updated = 0
        for fund in ALL_TRACKED_FUNDS:
            nav = match_fund(navs, fund)
            if nav and nav > 0:
                if fund not in history:
                    history[fund] = {'high': nav, 'low': nav}
                else:
                    if nav > history[fund]['high']: history[fund]['high'] = nav
                    if nav < history[fund]['low']:  history[fund]['low']  = nav
                updated += 1

        fetched[key] = True
        print(f'ok ({updated} funds)')
        time.sleep(0.5)

    history['_fetched_dates'] = fetched
    return history

def update_with_today(history, today_navs):
    """Compare today's NAVs with stored high/low and update if exceeded."""
    for fund in ALL_TRACKED_FUNDS:
        nav = match_fund(today_navs, fund)
        if not nav or nav <= 0:
            continue
        if fund not in history:
            history[fund] = {'high': nav, 'low': nav}
        else:
            if nav > history[fund]['high']: history[fund]['high'] = nav
            if nav < history[fund]['low']:  history[fund]['low']  = nav
    return history

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print('=== Wafrah NAV Fetcher ===')
    db  = init_firebase()
    fy  = get_current_fy()
    today = date.today()
    print(f'Date: {today} | FY: {fy}')

    # ── Step 1: Fetch today's NAVs from MUFAP ────────────────────────────────
    print('\n[1] Fetching today\'s NAVs from MUFAP…')
    today_navs = fetch_navs_for_date(today)
    if today_navs:
        print(f'    Got {len(today_navs)} NAVs from MUFAP')
    else:
        print('    WARNING: No data returned from MUFAP for today')

    # Build a clean dict of only tracked funds using fuzzy matching
    tracked_navs = {}
    for fund in ALL_TRACKED_FUNDS:
        nav = match_fund(today_navs, fund)
        if nav:
            tracked_navs[fund] = nav

    # ── Step 2: Write today's NAVs to meezan/navs ────────────────────────────
    if tracked_navs:
        db.collection('meezan').document('navs').set({
            'navs': tracked_navs,
            'success': True,
            'updatedAt': datetime.utcnow().isoformat() + 'Z',
        })
        print(f'    Stored {len(tracked_navs)} NAVs → meezan/navs')
        for f, v in tracked_navs.items():
            print(f'      {f}: {v}')
    else:
        print('    Skipping meezan/navs update — no tracked NAVs found')

    # ── Step 2b: Update meezan/portfolio navHistory (daily price log) ────────
    if tracked_navs:
        today_iso = date.today().isoformat()  # "2026-06-02"
        try:
            port_ref = db.collection('meezan').document('portfolio')
            port_doc = port_ref.get()
            nav_history = {}
            if port_doc.exists:
                nav_history = port_doc.to_dict().get('navHistory', {})
            # Merge today's NAVs — keep highest value per fund per day
            if today_iso not in nav_history:
                nav_history[today_iso] = {}
            for fund, nav in tracked_navs.items():
                existing = nav_history[today_iso].get(fund, 0)
                if nav > existing:
                    nav_history[today_iso][fund] = nav
            # Update only the navHistory field
            port_ref.update({'navHistory': nav_history})
            print(f'    Updated navHistory for {today_iso} → meezan/portfolio')
        except Exception as e:
            print(f'    WARNING: Could not update navHistory: {e}')

    # ── Step 3: Load existing FY history from Firestore ──────────────────────
    print(f'\n[2] Loading FY history for {fy}…')
    hist_ref = db.collection('meezan').document('fyHistory')
    hist_doc = hist_ref.get()
    all_history = hist_doc.to_dict() if hist_doc.exists else {}
    fy_history  = all_history.get(fy, {})

    # Check if we have meaningful history already
    has_history = any(f in fy_history and not f.startswith('_') for f in PLAN_FUNDS)
    if has_history:
        print(f'    Existing history found')
        for f in PLAN_FUNDS:
            if f in fy_history:
                s = fy_history[f]
                print(f'      {f}: Low={s["low"]:.4f}  High={s["high"]:.4f}')
    else:
        # ── Bootstrap: fetch full FY history from MUFAP ──────────────────────
        print('    No history found — bootstrapping from MUFAP historical data…')
        fy_history = build_fy_history(fy, fy_history)

    # ── Step 4: Update with today's data ─────────────────────────────────────
    fy_history = update_with_today(fy_history, today_navs)

    # ── Step 5: Save to Firestore meezan/fyHistory ───────────────────────────
    # Strip internal tracking metadata before saving
    clean_fy = {k: v for k, v in fy_history.items() if not k.startswith('_')}
    all_history[fy] = clean_fy
    hist_ref.set(all_history)
    print(f'\n[3] Saved FY history → meezan/fyHistory')

    # ── Final summary ─────────────────────────────────────────────────────────
    print('\n=== FY High/Low Summary ===')
    for fund in PLAN_FUNDS:
        if fund in clean_fy:
            s = clean_fy[fund]
            print(f'  {fund}')
            print(f'    FY Low : {s["low"]:.4f}')
            print(f'    FY High: {s["high"]:.4f}')
        else:
            print(f'  {fund}: no data yet')

    print('\nDone.')

if __name__ == '__main__':
    main()
