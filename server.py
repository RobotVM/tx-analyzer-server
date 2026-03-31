"""
Transaction Analyzer — Backend Server
======================================
Handles: Plaid bank connection + Google Sheets sync (read/write)

Install:
    pip install flask flask-cors plaid-python google-auth google-auth-oauthlib
                google-auth-httplib2 google-api-python-client python-dotenv

Run locally:    python server.py
Deploy free:    Push to GitHub → connect to Railway or Render

.env file needs:
    PLAID_CLIENT_ID=...
    PLAID_SECRET=...
    PLAID_ENV=sandbox
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...
    GOOGLE_SHEET_ID=...          # The long ID from your sheet's URL
    SECRET_KEY=any-random-string  # Used to sign session cookies
    FRONTEND_URL=https://your-app.up.railway.app  # or http://localhost:5000 locally
"""

import os, json, sqlite3
from datetime import date, timedelta, datetime
from flask import Flask, request, jsonify, redirect, session, url_for
from flask_cors import CORS
from dotenv import load_dotenv

# Google OAuth + Sheets
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Plaid
import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.products import Products
from plaid.model.country_code import CountryCode

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-change-this')
CORS(app, supports_credentials=True, origins=[
    os.getenv('FRONTEND_URL', 'http://localhost:5000'),
    'null',           # local file:// access during development
    'http://localhost:8080',
])

# ── Google OAuth config ───────────────────────────────────────────────────────
# Scopes: read/write spreadsheets, read user email (to confirm identity)
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid',
]
SHEET_ID      = os.getenv('GOOGLE_SHEET_ID', '')
SHEET_TAB     = 'Transactions'   # Tab name in your Google Sheet
FRONTEND_URL  = os.getenv('FRONTEND_URL', 'http://localhost:5000')

# Column layout in the Transactions sheet (1-indexed for Sheets API, 0-indexed here)
# date | description | amount | category | source | fingerprint
SHEET_HEADERS = ['Date', 'Description', 'Amount', 'Category', 'Source', 'Fingerprint']


def get_google_flow():
    """Build the OAuth flow from env credentials."""
    client_config = {
        "web": {
            "client_id":     os.getenv('GOOGLE_CLIENT_ID'),
            "client_secret": os.getenv('GOOGLE_CLIENT_SECRET'),
            "redirect_uris": [FRONTEND_URL + '/api/google/callback'],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = FRONTEND_URL + '/api/google/callback'
    return flow


def get_sheets_service():
    """Build an authenticated Sheets API client from the stored session token."""
    creds_data = session.get('google_credentials')
    if not creds_data:
        return None
    creds = Credentials(**creds_data)
    return build('sheets', 'v4', credentials=creds)


def ensure_sheet_headers(service):
    """Make sure the Transactions tab exists and has the right header row."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f'{SHEET_TAB}!A1:F1'
        ).execute()
        if not result.get('values'):
            # Write headers
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f'{SHEET_TAB}!A1',
                valueInputOption='RAW',
                body={'values': [SHEET_HEADERS]}
            ).execute()
    except Exception:
        # Tab probably doesn't exist — create it then add headers
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={'requests': [{'addSheet': {'properties': {'title': SHEET_TAB}}}]}
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f'{SHEET_TAB}!A1',
            valueInputOption='RAW',
            body={'values': [SHEET_HEADERS]}
        ).execute()


# ── Google OAuth endpoints ────────────────────────────────────────────────────

@app.route('/api/google/login')
def google_login():
    """Step 1: Redirect the user to Google's sign-in page."""
    flow = get_google_flow()
    auth_url, state = flow.authorization_url(
        access_type='offline',       # Get a refresh token so we don't re-auth constantly
        include_granted_scopes='true',
        prompt='consent'
    )
    session['oauth_state'] = state
    return redirect(auth_url)


@app.route('/api/google/callback')
def google_callback():
    """Step 2: Google redirects back here with an auth code. Exchange it for tokens."""
    flow = get_google_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    # Store credentials in the session (server-side only)
    session['google_credentials'] = {
        'token':         creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri':     creds.token_uri,
        'client_id':     creds.client_id,
        'client_secret': creds.client_secret,
        'scopes':        list(creds.scopes),
    }
    # Redirect back to the HTML app with a success flag
    return redirect(FRONTEND_URL + '?google=connected')


@app.route('/api/google/status')
def google_status():
    """Let the frontend check if Google is connected and which sheet is linked."""
    connected = 'google_credentials' in session
    return jsonify({
        'connected': connected,
        'sheet_id':  SHEET_ID if connected else None,
        'sheet_url': f'https://docs.google.com/spreadsheets/d/{SHEET_ID}' if SHEET_ID else None,
    })


@app.route('/api/google/logout')
def google_logout():
    """Disconnect Google — removes tokens from session."""
    session.pop('google_credentials', None)
    return jsonify({'status': 'disconnected'})


# ── Google Sheets transaction endpoints ───────────────────────────────────────

@app.route('/api/sheets/transactions', methods=['GET'])
def sheets_read():
    """
    Pull all transactions from the Google Sheet.
    Returns them in the same normalized shape the HTML app already understands,
    so no changes needed on the frontend.
    """
    service = get_sheets_service()
    if not service:
        return jsonify({'error': 'Google not connected'}), 401

    ensure_sheet_headers(service)

    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f'{SHEET_TAB}!A2:F'   # Skip header row
    ).execute()

    rows = result.get('values', [])
    transactions = []
    for i, row in enumerate(rows):
        # Pad short rows (in case some columns are empty)
        while len(row) < 6:
            row.append('')
        transactions.append({
            'id':           i,
            'date':         row[0],
            'desc':         row[1],
            'amount':       float(row[2]) if row[2] else 0,
            'category':     row[3],
            'src':          row[4],
            'fp':           row[5],
            'origCategory': row[3],  # Already categorized when stored
            'wasLearned':   False,
        })

    return jsonify({'transactions': transactions, 'total': len(transactions)})


@app.route('/api/sheets/transactions', methods=['POST'])
def sheets_write():
    """
    Merge new transactions into the Google Sheet.
    The frontend sends { transactions: [...] } — we deduplicate by fingerprint
    and append only the new ones. Returns { added, skipped }.
    """
    service = get_sheets_service()
    if not service:
        return jsonify({'error': 'Google not connected'}), 401

    ensure_sheet_headers(service)

    new_txns = request.json.get('transactions', [])
    if not new_txns:
        return jsonify({'added': 0, 'skipped': 0})

    # Read existing fingerprints to deduplicate
    existing = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f'{SHEET_TAB}!F2:F'   # Fingerprint column only (fast)
    ).execute()
    existing_fps = set()
    for row in existing.get('values', []):
        if row:
            existing_fps.add(row[0])

    # Build rows to append — skip any fingerprint already in the sheet
    rows_to_add = []
    skipped = 0
    for t in new_txns:
        fp = t.get('fp', '')
        if fp in existing_fps:
            skipped += 1
            continue
        existing_fps.add(fp)
        # Format date consistently
        d = t.get('date', '')
        if isinstance(d, str) and 'T' in d:
            d = d[:10]  # ISO format → YYYY-MM-DD
        rows_to_add.append([
            d,
            t.get('desc', ''),
            str(t.get('amount', 0)),
            t.get('category', 'Other'),
            t.get('src', 'csv'),
            fp,
        ])

    if rows_to_add:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f'{SHEET_TAB}!A1',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': rows_to_add}
        ).execute()

    return jsonify({'added': len(rows_to_add), 'skipped': skipped})


@app.route('/api/sheets/update_category', methods=['POST'])
def sheets_update_category():
    """
    Update a single transaction's category in the sheet.
    Called when the user re-categorizes a transaction in the app.
    Matches by fingerprint, updates column D (Category).
    """
    service = get_sheets_service()
    if not service:
        return jsonify({'error': 'Google not connected'}), 401

    fp       = request.json.get('fp')
    new_cat  = request.json.get('category')
    if not fp or not new_cat:
        return jsonify({'error': 'Missing fp or category'}), 400

    # Find the row with this fingerprint
    fps = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f'{SHEET_TAB}!F2:F'
    ).execute().get('values', [])

    row_index = None
    for i, row in enumerate(fps):
        if row and row[0] == fp:
            row_index = i + 2  # +2 because we skip header and 0-index
            break

    if row_index is None:
        return jsonify({'error': 'Transaction not found'}), 404

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f'{SHEET_TAB}!D{row_index}',
        valueInputOption='RAW',
        body={'values': [[new_cat]]}
    ).execute()

    return jsonify({'status': 'updated', 'row': row_index})


# ── Plaid endpoints (unchanged from before) ───────────────────────────────────

env_map = {
    'sandbox':     'https://sandbox.plaid.com',
    'development': 'https://development.plaid.com',
    'production':  'https://production.plaid.com',
}
PLAID_ENV = os.getenv('PLAID_ENV', 'sandbox')
configuration = plaid.Configuration(
    host=env_map.get(PLAID_ENV, 'https://sandbox.plaid.com'),
    api_key={
        'clientId': os.getenv('PLAID_CLIENT_ID', ''),
        'secret':   os.getenv('PLAID_SECRET', ''),
    }
)
plaid_client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
plaid_access_tokens = {}


@app.route('/api/status')
def status():
    return jsonify({
        'connected':      'local-user' in plaid_access_tokens,
        'google':         'google_credentials' in session,
        'plaid_env':      PLAID_ENV,
    })


@app.route('/api/create_link_token', methods=['POST'])
def create_link_token():
    try:
        req = LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id='local-user'),
            client_name='Transaction Analyzer',
            products=[Products('transactions')],
            country_codes=[CountryCode('US')],
            language='en',
        )
        resp = plaid_client.link_token_create(req)
        return jsonify({'link_token': resp['link_token']})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400


@app.route('/api/exchange_public_token', methods=['POST'])
def exchange_public_token():
    try:
        req = ItemPublicTokenExchangeRequest(public_token=request.json.get('public_token'))
        resp = plaid_client.item_public_token_exchange(req)
        plaid_access_tokens['local-user'] = resp['access_token']
        return jsonify({'status': 'connected'})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400


@app.route('/api/transactions')
def get_plaid_transactions():
    access_token = plaid_access_tokens.get('local-user')
    if not access_token:
        return jsonify({'error': 'No bank connected'}), 401
    days_back  = int(request.args.get('days', 90))
    end_date   = date.today()
    start_date = end_date - timedelta(days=days_back)
    try:
        req  = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(count=500)
        )
        resp = plaid_client.transactions_get(req)
        normalized = []
        for t in resp['transactions']:
            normalized.append({
                'date':     t['date'].strftime('%m/%d/%Y'),
                'desc':     t['name'],
                'amount':   -t['amount'],
                'category': t['personal_finance_category']['primary'] if t.get('personal_finance_category') else '',
                'merchant': t.get('merchant_name', ''),
                'pending':  t['pending'],
                'account':  t['account_id'],
            })
        return jsonify({'transactions': normalized, 'total': len(normalized)})
    except plaid.ApiException as e:
        return jsonify({'error': json.loads(e.body)}), 400


if __name__ == '__main__':
    # Allow OAuth redirect over HTTP in local dev (not needed in production)
    os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
    print(f'Starting server — Plaid: {PLAID_ENV}, Sheets: {SHEET_ID or "not set"}')
    app.run(debug=True, port=5000)
