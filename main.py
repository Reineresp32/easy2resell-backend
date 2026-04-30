from flask import Flask, request, jsonify, Response
import google.generativeai as genai
import os
import json

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Max-Age'] = '3600'
        return response

@app.before_request
def handle_preflight():
        if request.method == 'OPTIONS':
                    return '', 200

    try:
            genai.configure(api_key=os.environ.get("GOOGLE_API_KEY"))
        except:
    pass

            PLANS = {
                "normal": {"max_photos": 3, "model": "gemini-2.5-flash"},
                    "pro": {"max_photos": 5, "model": "gemini-2.5-pro"},
            }

@app.route("/health", methods=["GET"])
def health():
        return jsonify({"status": "ok"}), 200

@app.route("/analyze", methods=["POST"])
def analyze():
        try:
                    data = request.json or {}
                    images = data.get("images", [])
                    plan = data.get("plan", "normal")
                    custom_prompt = data.get("customPrompt", "")

        if not images:
                        return jsonify({"error": "No images"}), 400

            plan_config = PLANS.get(plan, PLANS["normal"])
        images = images[:plan_config["max_photos"]]
        model_name = plan_config["model"]

        if not custom_prompt:
                        custom_prompt = "Reply ONLY with JSON: {title, description, price, category, condition, hashtags}"

        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": img}} for img in images]
        parts.append(custom_prompt)

        model = genai.GenerativeModel(model_name)
        response = model.generate_content(parts)
        text = response.text.strip()

        if "```" in text:
                        text = text.split("```")[1]
                        if text.startswith("json"):
                                            text = text[4:]

                    return jsonify({"result": text}), 200
except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
        port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
