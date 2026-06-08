from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
import google.generativeai as genai
import base64
import os
import requests
from datetime import datetime

app = Flask(__name__)
# Hinter Railways Proxy: echte Client-IP aus X-Forwarded-For nehmen,
# damit das Rate-Limiting PRO NUTZER greift (sonst teilen sich alle eine IP).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# ═══════════════════════════════════════════
# CONFIG — alle Keys kommen aus Railway Env Vars
# ═══════════════════════════════════════════
GOOGLE_API_KEY       = os.environ.get('GOOGLE_API_KEY', '')
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
    response.headers['Access-Control-Allow-Origin'] = FRONTEND_URL
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Vary'] = 'Origin'
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
        "version": "4.2"
    })

# ═══════════════════════════════════════════
# ANALYZE ENDPOINT
# ═══════════════════════════════════════════
ADMIN_EMAILS = ['ben-koepke@web.de', 'metazocker@gmail.com']

def verify_token(req):
    """Verifiziert den Supabase-Login-Token aus dem Authorization-Header und
    gibt (user_id, email) der ECHTEN Identitaet zurueck — oder (None, None).
    Dadurch koennen user_id / user_email / is_guest aus dem Body NICHT mehr
    gefaelscht werden (sonst koennte jeder gratis die KI nutzen)."""
    auth = req.headers.get('Authorization', '')
    if not auth or not auth.startswith('Bearer ') or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None, None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": auth},
            timeout=5
        )
        if r.status_code == 200:
            u = r.json()
            return u.get('id'), (u.get('email') or '')
    except Exception as e:
        print(f"[Auth] verify error: {e}")
    return None, None

def is_maintenance_active():
    """Liest den Wartungsmodus aus site_settings (id=1)."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return False
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/site_settings?id=eq.1&select=maintenance_mode",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
            timeout=5
        )
        d = r.json()
        return bool(d and d[0].get('maintenance_mode'))
    except Exception as e:
        print(f"[Maintenance] check error: {e}")
        return False

def get_credit_balance(user_id):
    """Gibt den Credit-Stand eines Nutzers zurück."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not user_id:
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/credits?user_id=eq.{user_id}&select=balance",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"
            },
            timeout=5
        )
        data = resp.json()
        if data and len(data) > 0:
            return data[0].get('balance', 0)
        return 0
    except Exception as e:
        print(f"[Credits] get_balance error: {e}")
        return None

def deduct_credits(user_id, amount, description):
    """Zieht Credits ab. Gibt True zurück wenn erfolgreich."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not user_id:
        return False
    try:
        # Aktuellen Stand holen
        balance = get_credit_balance(user_id)
        if balance is None or balance < amount:
            return False

        new_balance = balance - amount

        # Balance updaten
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/credits?user_id=eq.{user_id}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json={"balance": new_balance, "updated_at": datetime.now().isoformat()},
            timeout=5
        )

        # Transaktion loggen
        requests.post(
            f"{SUPABASE_URL}/rest/v1/credit_transactions",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json"
            },
            json={"user_id": user_id, "amount": -amount, "type": "deduct", "description": description},
            timeout=5
        )
        return True
    except Exception as e:
        print(f"[Credits] deduct error: {e}")
        return False

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
    custom_prompt = data.get('customPrompt', '')
    is_guest = data.get('is_guest', False)
    is_refine = data.get('is_refine', False)
    
    # ECHTE Identität aus dem Login-Token holen (Body-Angaben sind fälschbar!)
    # user_id / user_email / plan / is_guest aus dem Body werden NICHT vertraut.
    user_id, user_email = verify_token(request)
    is_admin = bool(user_email and user_email in ADMIN_EMAILS)

    # Wartungsmodus: blockiert ALLE außer Admins (server-seitig, nicht umgehbar)
    if not is_admin and is_maintenance_active():
        return jsonify({"error": "Wartungsmodus aktiv — gleich wieder verfügbar.", "maintenance": True}), 503

    if user_id:
        # Eingeloggt: Admins zahlen nichts, alle anderen 1 Credit
        if not is_admin and not is_refine:
            ok = deduct_credits(user_id, 1, '1 Analyse')
            if not ok:
                return jsonify({"error": "Nicht genug Credits", "credits_required": True}), 402
    elif is_guest:
        # Gast-Demo ohne Login: kostenlos, nur durch Rate-Limit gebremst
        pass
    else:
        # Kein gültiger Token und kein Gast → abgelehnt
        return jsonify({"error": "Anmeldung erforderlich", "auth_required": True}), 401

    # customPrompt-Missbrauch eindaemmen:
    #  • Laenge hart begrenzen (verhindert riesige Prompt-Injections / Kosten).
    #  • Fuer nicht eingeloggte Gaeste KEINEN frei waehlbaren Prompt zulassen —
    #    sonst liesse sich unser Gemini-Key gratis als allgemeines KI-Tool
    #    missbrauchen. Gaeste bekommen ausschliesslich den Standard-Inserat-Prompt.
    #  • Eingeloggte zahlen pro Lauf 1 Credit -> wirtschaftlich selbst-begrenzt
    #    und brauchen den Custom-Prompt fuer das "Inserat anpassen"-Feature.
    if custom_prompt and len(custom_prompt) > 8000:
        return jsonify({"error": "customPrompt zu lang (max 8000 Zeichen)"}), 400
    if not user_id:
        custom_prompt = ''

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

    # Modellwahl server-seitig: das faelschbare 'plan'-Feld aus dem Body wird
    # NICHT genutzt (sonst koennte jeder gratis das teure Pro-Modell anfordern).
    # Pro-Modell deaktiviert (gemini-2.5-pro hat zu strenge Quota -> 429).
    # Einheitlich flash; bei echtem Pro-Abo + Quota spaeter wieder differenzieren.
    model_name = 'gemini-2.5-flash'

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
REMOVEBG_MONTHLY_LIMIT = 25  # max Bilder pro Studio-Nutzer pro Monat

def check_removebg_usage(user_id):
    """Prueft wie viele remove.bg Calls der Nutzer diesen Monat gemacht hat."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return 0
    try:
        from datetime import timezone
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/removebg_usage?user_id=eq.{user_id}&created_at=gte.{month_start}&select=count",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Prefer": "count=exact"
            },
            timeout=5
        )
        count = int(resp.headers.get('content-range', '0/0').split('/')[1])
        return count
    except Exception as e:
        print(f"[removebg] Usage check error: {e}")
        return 0

def track_removebg_usage(user_id):
    """Speichert einen remove.bg Call in Supabase."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/removebg_usage",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json"
            },
            json={"user_id": user_id},
            timeout=5
        )
    except Exception as e:
        print(f"[removebg] Usage track error: {e}")

@app.route('/remove-background', methods=['POST'])
@limiter.limit("30 per hour")
def remove_background():
    if not REMOVEBG_API_KEY:
        return jsonify({"error": "Background removal not configured"}), 500

    data = request.get_json(silent=True)
    if not data or not data.get('image'):
        return jsonify({"error": "No image provided"}), 400

    # ECHTE Identität aus dem Login-Token (nicht aus dem fälschbaren Body!)
    user_id, user_email = verify_token(request)
    is_admin = bool(user_email and user_email in ADMIN_EMAILS)

    # Wartungsmodus: blockiert alle außer Admins
    if not is_admin and is_maintenance_active():
        return jsonify({"error": "Wartungsmodus aktiv — gleich wieder verfügbar.", "maintenance": True}), 503

    if not is_admin:
        if not user_id:
            return jsonify({"error": "Anmeldung erforderlich", "upgrade": True}), 403
        usage = check_removebg_usage(user_id)
        if usage >= REMOVEBG_MONTHLY_LIMIT:
            return jsonify({
                "error": "Kein Credit verfuegbar. Bitte Credits kaufen.",
                "limit_reached": True,
                "usage": usage,
                "limit": REMOVEBG_MONTHLY_LIMIT
            }), 429

    try:
        img_bytes = base64.b64decode(data['image'])
        if len(img_bytes) > 12 * 1024 * 1024:
            return jsonify({"error": "Bild zu gross (max 12MB)"}), 400

        resp = requests.post(
            'https://api.remove.bg/v1.0/removebg',
            files={'image_file': ('image.jpg', img_bytes)},
            data={'size': 'auto', 'format': 'png'},
            headers={'X-Api-Key': REMOVEBG_API_KEY},
            timeout=30
        )

        if resp.status_code == 200:
            if user_id and not is_admin:
                track_removebg_usage(user_id)
            processed_b64 = base64.b64encode(resp.content).decode('utf-8')
            return jsonify({
                "image": processed_b64,
                "usage": check_removebg_usage(user_id) if user_id else None,
                "limit": REMOVEBG_MONTHLY_LIMIT
            })
        else:
            print(f"[remove.bg] Error: {resp.status_code} {resp.text[:200]}")
            return jsonify({"error": f"Hintergrundentfernung fehlgeschlagen: {resp.status_code}"}), 500
    except Exception as e:
        print(f"[remove.bg] Exception: {e}")
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════
# WILLKOMMENS-E-MAIL
# ═══════════════════════════════════════════
# Hinweis: Account-Loeschung laeuft ausschliesslich ueber die abgesicherte
# Supabase Edge Function (verifiziert das JWT, loescht NUR den eigenen Account).
# Der frueher hier vorhandene Flask-Endpoint /delete-account nahm die user_id
# ungeprueft aus dem Body und konnte so JEDEN Account loeschen — er wurde vom
# Frontend nie genutzt und ist deshalb komplett entfernt.

@app.route('/send-welcome', methods=['POST'])
@limiter.limit("5 per hour")
def send_welcome():
    data = request.get_json(silent=True) or {}
    email = (data.get('email', '') or '').strip()
    if not email or '@' not in email:
        return jsonify({"error": "Invalid email"}), 400

    # Nur fuer den eingeloggten Nutzer selbst: echtes JWT noetig und die
    # angefragte Adresse MUSS der Token-Identitaet entsprechen. Sonst koennte
    # jeder ueber unsere Domain beliebig Magic-Link-Mails ausloesen (Spam).
    token_uid, token_email = verify_token(request)
    if not token_uid or (token_email or '').lower() != email.lower():
        return jsonify({"error": "Nicht autorisiert"}), 403

    print(f"[Welcome] Email sent to {email}")

    # Willkommens-E-Mail via Supabase Auth (Custom SMTP). Der Mail-Inhalt
    # kommt aus der Supabase-E-Mail-Vorlage (Magic Link), nicht aus dem Code.
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        try:
            requests.post(
                f"{SUPABASE_URL}/auth/v1/admin/generate_link",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json"
                },
                json={"type": "magiclink", "email": email},
                timeout=5
            )
        except Exception as e:
            print(f"[Welcome] Email error: {e}")

    return jsonify({"status": "sent"}), 200


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
