from flask import Flask, request, jsonify, Response
import google.generativeai as genai
import os
import json

app = Flask(__name__)

# ═══════════════════════════════════════════════════
# CORS HEADERS - before any routes
# ═══════════════════════════════════════════════════
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '3600'
    return response

# Handle preflight requests
@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS':
        response = Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200

# ═══════════════════════════════════════════════════
# GOOGLE GEMINI CONFIG
# ═══════════════════════════════════════════════════
try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not set in environment!")
    else:
        genai.configure(api_key=api_key)
        print(f"✅ Google Gemini configured")
except Exception as e:
    print(f"❌ Google Gemini config error: {e}")

# ═══════════════════════════════════════════════════
# STRIPE CONFIG
# ═══════════════════════════════════════════════════
import stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

STRIPE_PRICES = {
    "normal": os.environ.get("STRIPE_PRICE_NORMAL", ""),
    "pro": os.environ.get("STRIPE_PRICE_PRO", ""),
}

# ═══════════════════════════════════════════════════
# PLANS
# ═══════════════════════════════════════════════════
PLANS = {
    "normal": {"max_photos": 3, "model": "gemini-1.5-flash"},
    "pro": {"max_photos": 5, "model": "gemini-1.5-pro"},
}

# ═══════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "model": "gemini-1.5-flash",
        "timestamp": __import__('datetime').datetime.utcnow().isoformat()
    }), 200

@app.route("/analyze", methods=["POST"])
def analyze():
    """Analyze images and generate marketplace listing"""
    try:
        # Get request data
        data = request.json or {}
        images = data.get("images", [])
        plan = data.get("plan", "normal")
        custom_prompt = data.get("customPrompt", "")
        
        print(f"📸 Analyzing {len(images)} image(s) with plan: {plan}")
        
        # Validate plan
        if plan not in PLANS:
            plan = "normal"
        
        plan_config = PLANS[plan]
        
        # Limit images to plan max
        if len(images) > plan_config["max_photos"]:
            images = images[:plan_config["max_photos"]]
        
        model_name = plan_config["model"]
        print(f"🤖 Using model: {model_name}")
        
        # Build default prompt if none provided
        if not custom_prompt:
            custom_prompt = "Analyze this item and create a marketplace listing. Reply ONLY with valid JSON containing: title (string), description (string, 2-3 sentences), price (string or number), priceReasoning (string), category (string), condition (string), hashtags (array of strings)"
        
        # Build request parts with images
        parts = []
        
        if not images:
            return jsonify({
                "error": "No images provided",
                "result": json.dumps({"error": "Please provide at least one image"})
            }), 400
        
        for img_b64 in images:
            if not img_b64:
                continue
            try:
                parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": img_b64
                    }
                })
            except Exception as e:
                print(f"⚠️  Image parsing error: {e}")
        
        # Add prompt
        parts.append(custom_prompt)
        
        print(f"📤 Sending to Google Gemini API...")
        
        # Call Google Gemini API
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(parts, stream=False)
        
        text = response.text.strip() if response and response.text else ""
        print(f"📥 Got response: {text[:200]}")
        
        # Parse JSON from response
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        
        # Validate JSON
        try:
            json.loads(text)
            print("✅ Valid JSON response")
        except json.JSONDecodeError as e:
            print(f"⚠️  Invalid JSON: {e}")
            # Try to return raw text in safe format
            text = json.dumps({"raw_response": text, "error": "Could not parse as JSON"})
        
        return jsonify({"result": text}), 200
        
    except Exception as e:
        print(f"❌ Analyze error: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": str(e),
            "type": type(e).__name__,
            "result": json.dumps({"error": str(e)})
        }), 500

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    """Create Stripe checkout session"""
    try:
        data = request.json or {}
        plan = data.get("plan", "normal")
        lang = data.get("lang", "EN")
        
        price_id = STRIPE_PRICES.get(plan)
        if not price_id:
            return jsonify({"error": "Stripe not configured for this plan"}), 400
        
        origin = request.headers.get("Origin", "https://easy2resell.netlify.app")
        
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=origin + "?success=1",
            cancel_url=origin + "?canceled=1",
            locale="de" if lang == "DE" else "en",
        )
        
        return jsonify({"url": session.url}), 200
        
    except Exception as e:
        print(f"❌ Checkout error: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def server_error(error):
    print(f"🔥 500 error: {error}")
    return jsonify({"error": "Internal server error"}), 500

# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    print(f"🚀 Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)