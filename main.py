from flask import Flask, request, jsonify, redirect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import google.generativeai as genai
from google.generativeai import types
import base64
import os
import json
import hashlib
import requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ═══════════════════════════════════════════
# CONFIG — alle Keys kommen aus Railway Env Vars
# ═══════════════════════════════════════════
GOOGLE_API_KEY       = os.environ.get('GOOGLE_API_KEY', '')
EBAY_CLIENT_ID       = os.environ.get('EBAY_CLIENT_ID', '')
EBAY_CLIENT_SECRET   = os.environ.get('EBAY_CLIENT_SECRET', '')
EBAY_RUNAME          = os.environ.get('EBAY_RUNAME', '')
EBAY_VERIFICATION_TOKEN = os.environ.get('EBAY_VERIFICATION_TOKEN', '')
EBAY_ENDPOINT_URL    = os.environ.get('EBAY_ENDPOINT_URL', 'https://web-production-c1b1b.up.railway.app/ebay-deletion')
SUPABASE_URL         = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
FRONTEND_URL         = os.environ.get('FRONTEND_URL', 'https://easy2resell.de')
REMOVEBG_API_KEY     = os.environ.get('REMOVEBG_API_KEY', '')

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# ═══════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "30 per hour"],
    storage_uri="memory://"
)

# ═══════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options(path):
    return jsonify({}), 200

# ═══════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "4.0",
        "ebay_configured": bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET and EBAY_RUNAME)
    })

# ═══════════════════════════════════════════
# ANALYZE ENDPOINT
# ═══════════════════════════════════════════
@app.route('/analyze', methods=['POST'])
@limiter.limit("20 per minute")
@limiter.limit("100 per hour")
@limiter.limit("300 per day")
def analyze():
    if not GOOGLE_API_KEY:
        return jsonify({"error": "API key not configured"}), 500

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body"}), 400

    images = data.get('images', [])
    plan = data.get('plan', 'normal')
    custom_prompt = data.get('customPrompt', '')

    if not images:
        return jsonify({"error": "No images provided"}), 400
    if len(images) > 10:
        return jsonify({"error": "Max 10 images allowed"}), 400

    for img in images:
        try:
            img_bytes = base64.b64decode(img)
            if len(img_bytes) > 4 * 1024 * 1024:
                return jsonify({"error": "Image too large (max 4MB)"}), 400
        except Exception:
            return jsonify({"error": "Invalid image data"}), 400

    model_name = 'gemini-2.5-pro' if plan == 'pro' else 'gemini-2.5-flash'

    try:
        # Schritt 1: Artikel identifizieren
        identify_model = genai.GenerativeModel(model_name)
        identify_parts = [{"inline_data": {"mime_type": "image/jpeg", "data": img}} for img in images]
        identify_parts.append(
            "Identifiziere diesen Artikel in einem kurzen Satz: Marke, Typ, Farbe, Zustand, Groesse falls erkennbar."
        )
        item_description = identify_model.generate_content(identify_parts).text.strip()
        print(f"[INFO] Item: {item_description}")

        # Schritt 2: Live-Preisrecherche
        price_context = ""
        try:
            search_model = genai.GenerativeModel(
                model_name,
                tools="google_search_retrieval"
            )
            search_prompt = (
                f"Suche jetzt auf vinted.de und ebay.de nach aktuellen Verkaufspreisen fuer: {item_description}\n"
                f"Gib mir Preisspanne, empfohlenen Preis und kurze Begruendung auf Deutsch in 2-3 Saetzen."
            )
            search_response = search_model.generate_content(search_prompt)
            price_context = f"\n\nLIVE MARKTPREISE:\n{search_response.text.strip()}\n\nNutze diese Preise als Basis."
        except Exception as se:
            print(f"[WARN] Search failed: {se}")
            price_context = "\n\nPREISREGEL: Neu mit Etikett = 40-60% UVP. Neuwertig = 30-50% UVP."

        # Schritt 3: Inserat generieren
        listing_model = genai.GenerativeModel(model_name)
        listing_parts = [{"inline_data": {"mime_type": "image/jpeg", "data": img}} for img in images]
        prompt = custom_prompt if custom_prompt else f"""
Analysiere diesen Artikel und erstelle ein optimiertes Inserat fuer deutsche Marketplace-Plattformen.
{price_context}

Antworte NUR mit einem JSON-Objekt (kein Markdown):
{{
  "title": "Praegnanter Titel (max 60 Zeichen)",
  "price": REALISTISCHER_PREIS_ALS_ZAHL,
  "priceReason": "Begruendung mit Vergleichspreisen",
  "description": "Verkaufsorientierte Beschreibung (3-5 Saetze)",
  "category": "Kategorie",
  "condition": "Neu mit Etikett/Neu ohne Etikett/Sehr gut/Gut/Akzeptabel",
  "brand": "Marke oder Unbekannt",
  "material": "Material falls erkennbar",
  "hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}
"""
        listing_parts.append(prompt)
        result_text = listing_model.generate_content(listing_parts).text
        return jsonify({"result": result_text})

    except Exception as e:
        error_msg = str(e)
        print(f"[ERROR] {datetime.utcnow().isoformat()} - {error_msg}")
        if 'quota' in error_msg.lower() or '429' in error_msg:
            return jsonify({"error": "Rate limit reached, please try again later"}), 429
        return jsonify({"error": "Analysis failed: " + error_msg}), 500


# ═══════════════════════════════════════════
# REMOVE.BG — Hintergrund entfernen + Bild verbessern
# ═══════════════════════════════════════════
@app.route('/remove-background', methods=['POST'])
@limiter.limit("30 per hour")
def remove_background():
    if not REMOVEBG_API_KEY:
        return jsonify({"error": "remove.bg not configured"}), 500

    data = request.get_json(silent=True)
    if not data or not data.get('image'):
        return jsonify({"error": "No image provided"}), 400

    try:
        img_bytes = base64.b64decode(data['image'])
        if len(img_bytes) > 12 * 1024 * 1024:
            return jsonify({"error": "Image too large (max 12MB)"}), 400

        resp = requests.post(
            'https://api.remove.bg/v1.0/removebg',
            files={'image_file': ('image.jpg', img_bytes)},
            data={'size': 'auto', 'format': 'png'},
            headers={'X-Api-Key': REMOVEBG_API_KEY},
            timeout=30
        )

        if resp.status_code == 200:
            processed_b64 = base64.b64encode(resp.content).decode('utf-8')
            return jsonify({"image": processed_b64})
        else:
            print(f"[remove.bg] Error: {resp.status_code} {resp.text[:200]}")
            return jsonify({"error": f"remove.bg API error: {resp.status_code}"}), 500
    except Exception as e:
        print(f"[remove.bg] Exception: {e}")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════
# eBay OAUTH FLOW
# ═══════════════════════════════════════════

def supabase_save_token(user_id, access_token, refresh_token, expires_at, ebay_user_id=''):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print(f"[Supabase] Missing config: URL={bool(SUPABASE_URL)}, KEY={bool(SUPABASE_SERVICE_KEY)}")
        return False
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/ebay_tokens",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal"
            },
            json={
                "user_id": user_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "ebay_user_id": ebay_user_id,
                "updated_at": datetime.utcnow().isoformat()
            },
            timeout=5
        )
        print(f"[Supabase] Save token response: {resp.status_code} {resp.text[:200]}")
        if resp.status_code in [200, 201, 204]:
            return True
        # Fallback: PATCH wenn schon existiert
        patch_resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/ebay_tokens?user_id=eq.{user_id}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "ebay_user_id": ebay_user_id,
                "updated_at": datetime.utcnow().isoformat()
            },
            timeout=5
        )
        print(f"[Supabase] Patch token response: {patch_resp.status_code}")
        return patch_resp.status_code in [200, 201, 204]
    except Exception as e:
        print(f"[Supabase] Save token error: {e}")
        return False


def supabase_get_token(user_id):
    """Holt eBay Token aus Supabase."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/ebay_tokens?user_id=eq.{user_id}&limit=1",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"
            },
            timeout=5
        )
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        print(f"[ERROR] Supabase get token: {e}")
        return None


def refresh_ebay_token(refresh_token):
    """Erneuert einen abgelaufenen eBay Access Token."""
    credentials = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "https://api.ebay.com/oauth/api_scope/sell.item"
        },
        timeout=10
    )
    if resp.status_code == 200:
        return resp.json()
    return None


@app.route('/ebay-auth', methods=['GET'])
def ebay_auth():
    """
    Leitet den Nutzer zur eBay Login-Seite weiter.
    Aufruf: GET /ebay-auth?user_id=SUPABASE_USER_ID
    """
    user_id = request.args.get('user_id', '')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    if not EBAY_CLIENT_ID or not EBAY_RUNAME:
        return jsonify({"error": "eBay not configured"}), 500

    # State = user_id damit wir im Callback wissen wer es ist
    state = base64.b64encode(user_id.encode()).decode()

    auth_url = (
        f"https://auth.ebay.com/oauth2/authorize"
        f"?client_id={EBAY_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={EBAY_RUNAME}"
        f"&scope=https://api.ebay.com/oauth/api_scope"
        f"%20https://api.ebay.com/oauth/api_scope/sell.inventory"
        f"%20https://api.ebay.com/oauth/api_scope/sell.account"
        f"&state={state}"
    )
    return redirect(auth_url)


@app.route('/ebay-callback', methods=['GET'])
def ebay_callback():
    """
    eBay leitet nach Login hierher weiter.
    Tauscht den Code gegen Access + Refresh Token.
    """
    code  = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')

    if error:
        return redirect(f"{FRONTEND_URL}?ebay=declined")

    if not code:
        return redirect(f"{FRONTEND_URL}?ebay=error&reason=no_code")

    # User ID aus State dekodieren
    try:
        user_id = base64.b64decode(state.encode()).decode()
    except Exception:
        return redirect(f"{FRONTEND_URL}?ebay=error&reason=invalid_state")

    # Code gegen Token tauschen
    credentials = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    try:
        token_resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": EBAY_RUNAME
            },
            timeout=10
        )

        if token_resp.status_code != 200:
            print(f"[ERROR] eBay token exchange failed: {token_resp.text}")
            return redirect(f"{FRONTEND_URL}?ebay=error&reason=token_exchange")

        token_data = token_resp.json()
        access_token  = token_data.get('access_token', '')
        refresh_token = token_data.get('refresh_token', '')
        expires_in    = token_data.get('expires_in', 7200)
        expires_at    = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        # Token in Supabase speichern
        saved = supabase_save_token(user_id, access_token, refresh_token, expires_at)
        if not saved:
            print(f"[WARN] Token not saved to Supabase for user {user_id}")

        print(f"[eBay] OAuth successful for user {user_id[:8]}...")
        return redirect(f"{FRONTEND_URL}?ebay=connected")

    except Exception as e:
        print(f"[ERROR] eBay callback: {e}")
        return redirect(f"{FRONTEND_URL}?ebay=error&reason=exception")


# ═══════════════════════════════════════════
# eBay LISTING ERSTELLEN
# ═══════════════════════════════════════════
@app.route('/ebay-list', methods=['POST'])
@limiter.limit("30 per hour")
def ebay_list():
    """
    Erstellt ein eBay Listing mit dem gespeicherten Token des Nutzers.
    Body: {
      user_id: "supabase-user-id",
      title: "Titel",
      description: "Beschreibung",
      price: 29.99,
      condition: "LIKE_NEW",
      category_id: "11450",
      images: ["base64..."]  (optional, max 12)
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body"}), 400

    user_id = data.get('user_id', '')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    # Token holen
    token_row = supabase_get_token(user_id)
    if not token_row:
        return jsonify({"error": "eBay not connected", "action": "connect"}), 401

    access_token = token_row.get('access_token', '')
    refresh_token = token_row.get('refresh_token', '')
    expires_at = token_row.get('expires_at', '')

    # Token erneuern falls abgelaufen
    if expires_at:
        try:
            # Timezone-safe Vergleich
            expires_str = expires_at.replace('Z', '+00:00') if expires_at.endswith('Z') else expires_at
            try:
                from datetime import timezone
                expires_dt = datetime.fromisoformat(expires_str)
                now = datetime.now(timezone.utc)
            except Exception:
                expires_dt = datetime.fromisoformat(expires_at.replace('Z', ''))
                now = datetime.utcnow()
                expires_dt = expires_dt.replace(tzinfo=None)
                now = now.replace(tzinfo=None)

            if now >= expires_dt - timedelta(minutes=5):
                new_tokens = refresh_ebay_token(refresh_token)
                if new_tokens:
                    access_token = new_tokens.get('access_token', access_token)
                    new_expires_at = (datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 7200))).isoformat()
                    supabase_save_token(user_id, access_token, new_tokens.get('refresh_token', refresh_token), new_expires_at)
                else:
                    return jsonify({"error": "eBay token expired, please reconnect", "action": "reconnect"}), 401
        except Exception as ex:
            print(f"[WARN] Token expiry check failed: {ex}")

    # Condition Mapping
    condition_map = {
        "Neu mit Etikett": "NEW",
        "Neu ohne Etikett": "NEW",
        "Sehr gut": "LIKE_NEW",
        "Gut": "GOOD",
        "Akzeptabel": "ACCEPTABLE"
    }
    condition = condition_map.get(data.get('condition', ''), "LIKE_NEW")

    # eBay Listing Payload (Inventory Item API)
    title = data.get('title', '')[:80]  # eBay max 80 Zeichen
    description = data.get('description', '')
    price = float(data.get('price', 0))
    category_id = str(data.get('category_id', '11450'))  # 11450 = Kleidung Damen

    # Zuerst Inventory Item anlegen
    sku = f"e2r-{user_id[:8]}-{int(datetime.utcnow().timestamp())}"

    inventory_payload = {
        "condition": condition,
        "product": {
            "title": title,
            "description": description,
            "aspects": {}
        },
        "availability": {
            "shipToLocationAvailability": {
                "quantity": 1
            }
        }
    }

    try:
        # 0. Location sicherstellen
        loc_check = requests.get(
            "https://api.ebay.com/sell/inventory/v1/location/easy2resell_default",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        if loc_check.status_code != 200:
            loc_create = requests.put(
                "https://api.ebay.com/sell/inventory/v1/location/easy2resell_default",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Content-Language": "de-DE"
                },
                json={
                    "location": {"address": {"country": "DE"}},
                    "locationTypes": ["WAREHOUSE"],
                    "merchantLocationStatus": "ENABLED",
                    "name": "easy2resell"
                },
                timeout=10
            )
            print(f"[eBay] Location create: {loc_create.status_code} {loc_create.text[:100]}")

        # 1. Inventory Item erstellen
        inv_resp = requests.put(
            f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Content-Language": "de-DE",
                "Accept-Language": "de-DE"
            },
            json=inventory_payload,
            timeout=15
        )

        if inv_resp.status_code not in [200, 201, 204]:
            print(f"[ERROR] eBay inventory: {inv_resp.status_code} {inv_resp.text[:200]}")
            return jsonify({"error": "eBay inventory creation failed", "detail": inv_resp.text[:200]}), 500

        # 2. Offer erstellen (Listing)
        offer_payload = {
            "sku": sku,
            "marketplaceId": "EBAY_DE",
            "format": "FIXED_PRICE",
            "listingDescription": description,
            "pricingSummary": {
                "price": {
                    "value": str(price),
                    "currency": "EUR"
                }
            },
            "categoryId": category_id,
            "merchantLocationKey": "easy2resell_default",
            "listingPolicies": {}
        }

        offer_resp = requests.post(
            "https://api.ebay.com/sell/inventory/v1/offer",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Content-Language": "de-DE"
            },
            json=offer_payload,
            timeout=15
        )

        if offer_resp.status_code not in [200, 201]:
            print(f"[ERROR] eBay offer: {offer_resp.status_code} {offer_resp.text[:200]}")
            return jsonify({"error": "eBay offer creation failed", "detail": offer_resp.text[:200]}), 500

        offer_id = offer_resp.json().get('offerId', '')

        # 3. Offer publizieren
        pub_resp = requests.post(
            f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            timeout=15
        )

        if pub_resp.status_code not in [200, 201]:
            print(f"[ERROR] eBay publish: {pub_resp.status_code} {pub_resp.text[:200]}")
            return jsonify({"error": "eBay publish failed", "detail": pub_resp.text[:200]}), 500

        listing_id = pub_resp.json().get('listingId', '')
        listing_url = f"https://www.ebay.de/itm/{listing_id}"

        print(f"[eBay] Listing created: {listing_url}")
        return jsonify({
            "success": True,
            "listing_id": listing_id,
            "listing_url": listing_url,
            "sku": sku
        })

    except Exception as e:
        print(f"[ERROR] eBay listing: {e}")
        return jsonify({"error": "eBay listing failed: " + str(e)}), 500


# ═══════════════════════════════════════════
# eBay CONNECTION STATUS
# ═══════════════════════════════════════════
@app.route('/ebay-status', methods=['GET'])
def ebay_status():
    """Prueft ob ein Nutzer eBay verbunden hat."""
    user_id = request.args.get('user_id', '')
    if not user_id:
        return jsonify({"connected": False}), 400

    token_row = supabase_get_token(user_id)
    if not token_row:
        return jsonify({"connected": False})

    expires_at = token_row.get('expires_at', '')
    is_expired = False
    if expires_at:
        try:
            expires_dt = datetime.fromisoformat(expires_at.replace('Z', ''))
            is_expired = datetime.utcnow() >= expires_dt
        except Exception:
            pass

    return jsonify({
        "connected": True,
        "expired": is_expired,
        "ebay_user_id": token_row.get('ebay_user_id', '')
    })


# ═══════════════════════════════════════════
# eBay MERCHANT LOCATION SETUP
# ═══════════════════════════════════════════
@app.route('/ebay-setup-location', methods=['POST'])
def ebay_setup_location():
    data = request.get_json(silent=True) or {}
    user_id = data.get('user_id', '')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    token_row = supabase_get_token(user_id)
    if not token_row:
        print(f"[eBay Setup] No token found for user {user_id[:8]}")
        return jsonify({"error": "eBay not connected"}), 401

    access_token = token_row.get('access_token', '')
    print(f"[eBay Setup] Creating location for user {user_id[:8]}...")

    location_payload = {
        "location": {
            "address": {
                "country": "DE"
            }
        },
        "locationAdditionalInformation": "easy2resell seller location",
        "locationTypes": ["WAREHOUSE"],
        "merchantLocationStatus": "ENABLED",
        "name": "easy2resell"
    }

    try:
        resp = requests.put(
            "https://api.ebay.com/sell/inventory/v1/location/easy2resell_default",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Content-Language": "de-DE"
            },
            json=location_payload,
            timeout=10
        )
        print(f"[eBay Setup] Location response: {resp.status_code} {resp.text[:200]}")
        if resp.status_code in [200, 201, 204]:
            return jsonify({"success": True})
        return jsonify({"error": resp.text[:200], "status": resp.status_code}), 500
    except Exception as e:
        print(f"[eBay Setup] Exception: {e}")
        return jsonify({"error": str(e)}), 500



@app.route('/ebay-deletion', methods=['GET'])
def ebay_deletion_challenge():
    challenge_code = request.args.get('challenge_code', '')
    if not challenge_code:
        return jsonify({"error": "Missing challenge_code"}), 400

    raw = challenge_code + EBAY_VERIFICATION_TOKEN + EBAY_ENDPOINT_URL
    challenge_response = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    print(f"[eBay] Challenge: {challenge_code[:20]}...")
    return jsonify({"challengeResponse": challenge_response}), 200


@app.route('/ebay-deletion', methods=['POST'])
def ebay_deletion_notification():
    try:
        data = request.get_json(silent=True) or {}
        notification = data.get('notification', {})
        user_data = notification.get('data', {})
        username = user_data.get('username', 'unknown')
        user_id = user_data.get('userId', 'unknown')
        print(f"[eBay] Deletion: user={username}, id={user_id}")

        # eBay Token aus Supabase loeschen
        if SUPABASE_URL and SUPABASE_SERVICE_KEY and user_id != 'unknown':
            try:
                requests.delete(
                    f"{SUPABASE_URL}/rest/v1/ebay_tokens?ebay_user_id=eq.{user_id}",
                    headers={
                        "apikey": SUPABASE_SERVICE_KEY,
                        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"
                    },
                    timeout=5
                )
            except Exception:
                pass

        return jsonify({"status": "received"}), 200
    except Exception as e:
        print(f"[eBay] Deletion error: {e}")
        return jsonify({"status": "error_logged"}), 200


# ═══════════════════════════════════════════
# RATE LIMIT ERROR HANDLER
# ═══════════════════════════════════════════
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Too many requests",
        "retry_after": str(e.description)
    }), 429


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)