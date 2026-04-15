from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
import os

app = Flask(__name__)
CORS(app)  # Erlaubt Anfragen von deiner Netlify-Seite

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

PLANS = {
    "normal": {"max_photos": 3, "hashtags": 8,  "detailed": False, "model": "claude-haiku-4-5-20251001"},
    "pro":    {"max_photos": 5, "hashtags": 15, "detailed": True,  "model": "claude-sonnet-4-20250514"},
}

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json
    images   = data.get("images", [])
    language = data.get("language", "DE")
    plan     = data.get("plan", "normal")

    plan_config = PLANS.get(plan, PLANS["normal"])
    images = images[:plan_config["max_photos"]]  # Limit serverseitig durchsetzen
    hashtag_count = plan_config["hashtags"]
    detailed = plan_config["detailed"]
    model = plan_config["model"]  # Haiku fuer Normal, Sonnet fuer Pro

    desc_instruction = (
        "5-6 Saetze, sehr detailliert, verkaufsoptimiert" if detailed
        else "3-4 Saetze, klar und praezise"
    ) if language == "DE" else (
        "5-6 sentences, very detailed, sales-optimized" if detailed
        else "3-4 sentences, clear and precise"
    )

    if language == "DE":
        prompt = f"""Analysiere dieses Kleidungsstueck und generiere ein perfektes Vinted-Inserat auf Deutsch.

Antworte NUR mit diesem JSON (kein Text davor oder danach):
{{
  "title": "Praegnanter Titel max 50 Zeichen",
  "description": "{desc_instruction}",
  "price": "25EUR",
  "priceReasoning": "Kurze Begruendung fuer den Preis",
  "category": "z.B. Jacken",
  "condition": "Neu / Sehr gut / Gut / Akzeptabel",
  "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8"{',\"tag9\",\"tag10\",\"tag11\",\"tag12\",\"tag13\",\"tag14\",\"tag15\"' if hashtag_count > 8 else ''}]
}}"""
    else:
        prompt = f"""Analyze this clothing item and generate a perfect Vinted listing in English.

Respond ONLY with this JSON (no text before or after):
{{
  "title": "Catchy title max 50 chars",
  "description": "{desc_instruction}",
  "price": "EUR25",
  "priceReasoning": "Brief reasoning for the price",
  "category": "e.g. Jackets",
  "condition": "New / Very Good / Good / Acceptable",
  "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8"{',\"tag9\",\"tag10\",\"tag11\",\"tag12\",\"tag13\",\"tag14\",\"tag15\"' if hashtag_count > 8 else ''}]
}}"""

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}
        for img in images
    ]
    content.append({"type": "text", "text": prompt})

    message = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": content}]
    )

    result_text = message.content[0].text
    return jsonify({"result": result_text})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
