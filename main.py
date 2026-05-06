from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import google.generativeai as genai
from google.generativeai import types
import base64
import os
import json
import hashlib
from datetime import datetime

app = Flask(__name__)

# ═══════════════════════════════════════════
# eBay MARKETPLACE ACCOUNT DELETION CONFIG
# ═══════════════════════════════════════════
# Verification Token: zufaelliger 32-80 Zeichen String, identisch mit dem Wert in der eBay Developer Console
EBAY_VERIFICATION_TOKEN = os.environ.get(
    'EBAY_VERIFICATION_TOKEN',
    'easy2resell-ebay-verification-token-2026-secure-string-do-not-share'
)
# Endpoint URL — muss exakt mit der in eBay Developer Console hinterlegten URL uebereinstimmen
EBAY_ENDPOINT_URL = os.environ.get(
    'EBAY_ENDPOINT_URL',
    'https://web-production-c1b1b.up.railway.app/ebay-deletion'
)

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
# GEMINI SETUP
# ═══════════════════════════════════════════
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# ═══════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "3.0"
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
        # ── SCHRITT 1: Artikel identifizieren ──
        identify_model = genai.GenerativeModel(model_name)
        identify_parts = []
        for img_b64 in images:
            identify_parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": img_b64}
            })
        identify_parts.append(
            "Identifiziere diesen Artikel in einem kurzen Satz: Marke, Typ, Farbe, Zustand, Groesse falls erkennbar. "
            "Beispiel: 'Nike Air Force 1 Sneaker weiss Groesse 42 neuwertig'"
        )
        identify_response = identify_model.generate_content(identify_parts)
        item_description = identify_response.text.strip()
        print(f"[INFO] Item: {item_description}")

        # ── SCHRITT 2: Live-Preisrecherche mit Google Search ──
        price_context = ""
        try:
            search_model = genai.GenerativeModel(
                model_name,
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
            search_prompt = (
                f"Suche jetzt auf vinted.de und ebay.de/kleinanzeigen nach aktuellen Verkaufspreisen fuer: {item_description}\n"
                f"Finde echte aktuelle Angebote. Gib mir:\n"
                f"1. Preisspanne der gefundenen Angebote (z.B. 35-65 Euro)\n"
                f"2. Empfohlener Verkaufspreis\n"
                f"3. Kurze Begruendung\n"
                f"Antworte auf Deutsch in 2-3 Saetzen."
            )
            search_response = search_model.generate_content(search_prompt)
            price_context = f"\n\nLIVE MARKTPREISE (gerade recherchiert auf Vinted/eBay):\n{search_response.text.strip()}\n\nNutze diese echten Marktpreise fuer deine Preisempfehlung."
            print(f"[INFO] Price research: {search_response.text[:100]}")
        except Exception as se:
            print(f"[WARN] Search failed: {se}")
            price_context = "\n\nPREISREGEL: Neu mit Etikett = 40-60% UVP. Neuwertig Markenartikel = 30-50% UVP. Niemals unter Marktwert einpreisen."

        # ── SCHRITT 3: Inserat generieren ──
        listing_model = genai.GenerativeModel(model_name)
        listing_parts = []
        for img_b64 in images:
            listing_parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": img_b64}
            })

        prompt = custom_prompt if custom_prompt else f"""
Analysiere diesen Artikel und erstelle ein optimiertes Inserat fuer deutsche Marketplace-Plattformen.
{price_context}

Antworte NUR mit einem JSON-Objekt (kein Markdown, keine Erklaerungen):
{{
  "title": "Praegnanter Titel (max 60 Zeichen)",
  "price": REALISTISCHER_PREIS_ALS_ZAHL,
  "priceReason": "Begruendung mit konkreten Vergleichspreisen aus der Live-Recherche",
  "description": "Verkaufsorientierte Beschreibung (3-5 Saetze)",
  "category": "Kategorie",
  "condition": "Zustand (Neu mit Etikett/Neu ohne Etikett/Sehr gut/Gut/Akzeptabel)",
  "brand": "Marke oder Unbekannt",
  "material": "Material falls erkennbar",
  "hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}
"""
        listing_parts.append(prompt)
        listing_response = listing_model.generate_content(listing_parts)
        result_text = listing_response.text

        return jsonify({"result": result_text})

    except Exception as e:
        error_msg = str(e)
        print(f"[ERROR] {datetime.utcnow().isoformat()} — {error_msg}")
        if 'quota' in error_msg.lower() or '429' in error_msg:
            return jsonify({"error": "Rate limit reached, please try again later"}), 429
        return jsonify({"error": "Analysis failed: " + error_msg}), 500


# ═══════════════════════════════════════════
# eBay MARKETPLACE ACCOUNT DELETION
# ═══════════════════════════════════════════
# Doku: https://developer.ebay.com/marketplace-account-deletion
#
# eBay sendet 2 Arten von Requests:
# 1) GET mit ?challenge_code=XYZ  -> wir antworten mit SHA256(challenge + token + endpoint)
# 2) POST mit Lösch-Notification  -> wir loeschen die Nutzerdaten und antworten 200/204

@app.route('/ebay-deletion', methods=['GET'])
def ebay_deletion_challenge():
    """
    Validation-Endpoint: eBay sendet einen Challenge-Code, wir antworten mit Hash.
    Hash = SHA256( challenge_code + verification_token + endpoint_url )
    """
    challenge_code = request.args.get('challenge_code', '')
    if not challenge_code:
        return jsonify({"error": "Missing challenge_code"}), 400

    raw = challenge_code + EBAY_VERIFICATION_TOKEN + EBAY_ENDPOINT_URL
    challenge_response = hashlib.sha256(raw.encode('utf-8')).hexdigest()

    print(f"[eBay] Challenge received: {challenge_code[:20]}... → response generated")

    return jsonify({"challengeResponse": challenge_response}), 200


@app.route('/ebay-deletion', methods=['POST'])
def ebay_deletion_notification():
    """
    Notification-Endpoint: eBay sendet Lösch-Events.
    Payload-Beispiel:
      {
        "metadata": {...},
        "notification": {
          "data": {
            "username": "ebay_user",
            "userId": "123",
            "eiasToken": "..."
          }
        }
      }
    Wir bestätigen mit 200/204 — eBay erwartet eine Antwort innerhalb von 3 Sekunden.
    """
    try:
        data = request.get_json(silent=True) or {}
        notification = data.get('notification', {})
        user_data = notification.get('data', {})
        username = user_data.get('username', 'unknown')
        user_id = user_data.get('userId', 'unknown')

        print(f"[eBay] Account deletion notification: user={username}, id={user_id}")

        # ── Hier die Nutzerdaten aus der eigenen DB löschen ──
        # Wir speichern aktuell keine eBay-Userdaten persistent.
        # Sobald wir eBay-OAuth-Tokens speichern, hier per user_id löschen:
        #
        # supabase.from_('ebay_tokens').delete().eq('ebay_user_id', user_id).execute()
        #
        # Fuer jetzt: einfach loggen und 200 antworten.

        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"[eBay] Deletion notification error: {e}")
        # Wir antworten trotzdem 200, damit eBay nicht retry-storms macht
        return jsonify({"status": "error_logged"}), 200


# ═══════════════════════════════════════════
# RATE LIMIT ERROR HANDLER
# ═══════════════════════════════════════════
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Too many requests",
        "message": "Rate limit exceeded. Please try again later.",
        "retry_after": str(e.description)
    }), 429


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)