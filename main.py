from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import stripe
import os

app = Flask(__name__)
CORS(app)

genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

STRIPE_PRICES = {
    "normal": os.environ.get("STRIPE_PRICE_NORMAL", ""),
    "pro": os.environ.get("STRIPE_PRICE_PRO", ""),
}

PLANS = {
    "normal": {"max_photos": 3, "model": "gemini-1.5-flash"},
    "pro": {"max_photos": 5, "model": "gemini-1.5-pro"},
}

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json
    images = data.get("images", [])
    plan = data.get("plan", "normal")
    custom_prompt = data.get("customPrompt", "")
    plan_config = PLANS.get(plan, PLANS["normal"])
    images = images[:plan_config["max_photos"]]
    model_name = plan_config["model"]
    if not custom_prompt:
        custom_prompt = "Analyze this item and create a listing. Reply ONLY with JSON: {title, description, price, priceReasoning, category, condition, hashtags}"
    parts = []
    for img_b64 in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_b64}})
    parts.append(custom_prompt)
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(parts)
    text = response.text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return jsonify({"result": text.strip()})

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    data = request.json
    plan = data.get("plan", "normal")
    lang = data.get("lang", "EN")
    price_id = STRIPE_PRICES.get(plan)
    if not price_id:
        return jsonify({"error": "Stripe not configured"}), 400
    origin = request.headers.get("Origin", "https://easy2resell.netlify.app")
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=origin + "?success=1",
        cancel_url=origin + "?canceled=1",
        locale="de" if lang == "DE" else "en",
    )
    return jsonify({"url": session.url})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": "gemini"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)