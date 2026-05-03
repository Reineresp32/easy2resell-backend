from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import google.generativeai as genai
from google.generativeai import types
import base64
import os
import json
from datetime import datetime

app = Flask(__name__)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "30 per hour"],
    storage_uri="memory://"
)

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

GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat(), "version": "3.0"})

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
        identify_model = genai.GenerativeModel(model_name)
        identify_parts = []
        for img_b64 in images:
            identify_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_b64}})
        identify_parts.append(
            "Identifiziere diesen Artikel in einem kurzen Satz: Marke, Typ, Farbe, Zustand, Groesse falls erkennbar. "
            "Beispiel: 'Nike Air Force 1 Sneaker weiss Groesse 42 neuwertig'"
        )
        identify_response = identify_model.generate_content(identify_parts)
        item_description = identify_response.text.strip()
        print(f"[INFO] Item: {item_description}")

        price_context = ""
        try:
            search_model = genai.GenerativeModel(
                model_name,
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
            search_prompt = (
                f"Suche jetzt auf vinted.de und ebay.de nach aktuellen Verkaufspreisen fuer: {item_description}\n"
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
            price_context = "\n\nPREISREGEL: Neu mit Etikett = 40-60% UVP. Neuwertig = 30-50% UVP. Niemals unter Marktwert."

        listing_model = genai.GenerativeModel(model_name)
        listing_parts = []
        for img_b64 in images:
            listing_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_b64}})

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
        return jsonify({"result": listing_response.text})

    except Exception as e:
        error_msg = str(e)
        print(f"[ERROR] {datetime.utcnow().isoformat()} — {error_msg}")
        if 'quota' in error_msg.lower() or '429' in error_msg:
            return jsonify({"error": "Rate limit reached, please try again later"}), 429
        return jsonify({"error": "Analysis failed: " + error_msg}), 500

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"error": "Too many requests", "retry_after": str(e.description)}), 429

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)