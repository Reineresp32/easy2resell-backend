"""
Apple In-App-Purchase — server-seitige, SIGNATUR-VERIFIZIERTE Gutschrift.
================================================================================

Ablauf (client-initiiert, server-verifiziert):

  iOS-App  ──POST /apple/redeem──►  Backend
           Body:   { "jws": "<StoreKit transaction.jwsRepresentation>" }
           Header: Authorization: Bearer <Supabase-Login-Token>

  Backend  ─ verifiziert die von APPLE signierte Transaktion (JWS, Zertifikatskette)
           ─ schreibt Credits gut / setzt Pro  (idempotent pro transactionId)
           ─ liefert das neue Guthaben zurück

WARUM SICHER: Die JWS ist von Apple signiert und kann nicht gefälscht werden.
Der Client schreibt NIE selbst Guthaben (RLS blockt das ohnehin). Selbst wenn
jemand /apple/redeem mit Müll aufruft, scheitert die Signaturprüfung.

Einbau in main.py:
    from apple_iap import register_iap, user_is_pro
    register_iap(app, verify_token)          # nach der verify_token-Definition
    # ... und in /analyze:  is_pro = user_is_pro(user_id)

Voraussetzungen:  siehe RAILWAY_pro_iap.md
"""
import os
import glob
from datetime import datetime, timezone

import requests
from flask import request, jsonify

try:
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier, VerificationException
    from appstoreserverlibrary.models.Environment import Environment
    _LIB_OK = True
except Exception as _e:  # Library noch nicht installiert -> Endpoint meldet sauberen Fehler
    _LIB_OK = False
    _IMPORT_ERR = str(_e)

# ── Konfiguration (alles aus Railway Env Vars) ──────────────────────────────
SUPABASE_URL         = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
BUNDLE_ID            = os.environ.get('IOS_BUNDLE_ID', 'de.easy2resell.app')
APP_APPLE_ID         = os.environ.get('APP_APPLE_ID', '')   # numerisch, aus App Store Connect (für Production)
ONLINE_CHECKS        = os.environ.get('APPLE_ONLINE_CHECKS', '0') == '1'

# Produkt-IDs -> Credits (müssen exakt zu StoreKit / App Store Connect passen)
CREDIT_PRODUCTS = {
    'de.easy2resell.credits.10': 10,
    'de.easy2resell.credits.30': 30,
    'de.easy2resell.credits.100': 100,
}
PRO_PRODUCT          = 'de.easy2resell.pro.monthly'
PRO_MONTHLY_CREDITS  = 10   # "10 Credits/Monat inklusive" — pro Abo-Periode gutgeschrieben

# Apple Root-CA-Zertifikate (.cer, DER) liegen neben dieser Datei in apple_certs/
_CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apple_certs')


def _load_root_certs():
    certs = []
    for p in sorted(glob.glob(os.path.join(_CERT_DIR, '*.cer'))):
        with open(p, 'rb') as f:
            certs.append(f.read())
    return certs


_verifiers_cache = None
def _verifiers():
    """Verifier für Sandbox (+ Production, falls APP_APPLE_ID gesetzt). Gecacht."""
    global _verifiers_cache
    if _verifiers_cache is not None:
        return _verifiers_cache
    vs = []
    certs = _load_root_certs()
    if certs and _LIB_OK:
        vs.append(SignedDataVerifier(certs, ONLINE_CHECKS, Environment.SANDBOX, BUNDLE_ID, None))
        if APP_APPLE_ID.isdigit():
            vs.append(SignedDataVerifier(certs, ONLINE_CHECKS, Environment.PRODUCTION, BUNDLE_ID, int(APP_APPLE_ID)))
    _verifiers_cache = vs
    return vs


def _verify_transaction(jws):
    """Verifiziert die JWS gegen Sandbox & Production. Wirft bei Misserfolg."""
    if not _LIB_OK:
        raise RuntimeError(f"app-store-server-library fehlt: {_IMPORT_ERR}")
    last = None
    vs = _verifiers()
    if not vs:
        raise RuntimeError("Keine Apple-Root-Zertifikate in apple_certs/ gefunden.")
    for v in vs:
        try:
            return v.verify_and_decode_signed_transaction(jws)
        except VerificationException as e:
            last = e
    raise last


def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


def user_is_pro(user_id):
    """True, wenn der Nutzer ein aktives Pro-Abo hat (server-autoritativ via pro_until)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY and user_id):
        return False
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/credits?user_id=eq.{user_id}&select=pro_until",
            headers=_supabase_headers(), timeout=5)
        rows = r.json()
        if not rows:
            return False
        until = _parse_ts(rows[0].get('pro_until'))
        return bool(until and until > datetime.now(timezone.utc))
    except Exception:
        return False


def _balance(user_id):
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/credits?user_id=eq.{user_id}&select=balance",
            headers=_supabase_headers(), timeout=5)
        rows = r.json()
        return int(rows[0]['balance']) if rows else 0
    except Exception:
        return 0


def _norm_uuid(x):
    return str(x).replace('-', '').lower() if x else ''


def register_iap(app, verify_token):
    """Hängt /apple/redeem an die Flask-App. `verify_token(request) -> (user_id, email)`."""

    @app.route('/apple/redeem', methods=['POST'])
    def apple_redeem():
        # 1) Echte Identität aus dem Login-Token (Body ist fälschbar)
        user_id, user_email = verify_token(request)
        if not user_id:
            return jsonify({"error": "Anmeldung erforderlich"}), 401

        data = request.get_json(silent=True) or {}
        jws = data.get('jws') or data.get('signedTransaction')
        if not jws:
            return jsonify({"error": "Keine Transaktion übergeben"}), 400

        # 2) Von Apple signierte Transaktion verifizieren
        try:
            txn = _verify_transaction(jws)
        except Exception as e:
            print(f"[IAP] Verify failed: {e}")
            return jsonify({"error": "Transaktion nicht verifizierbar"}), 400

        product_id     = getattr(txn, 'product_id', None)
        transaction_id = getattr(txn, 'transaction_id', None)
        aat            = getattr(txn, 'app_account_token', None)
        if not product_id or not transaction_id:
            return jsonify({"error": "Unvollständige Transaktion"}), 400

        # 3) appAccountToken (falls gesetzt) muss zum eingeloggten Konto passen
        if aat and _norm_uuid(aat) != _norm_uuid(user_id):
            print(f"[IAP] appAccountToken mismatch: txn={aat} user={user_id}")
            return jsonify({"error": "Transaktion gehört zu einem anderen Konto"}), 403

        # 4) Produkt -> Credits / Pro
        credits = CREDIT_PRODUCTS.get(product_id, 0)
        pro_until = None
        if product_id == PRO_PRODUCT:
            credits = PRO_MONTHLY_CREDITS
            exp_ms = getattr(txn, 'expires_date', None)   # ms seit Epoch
            if exp_ms:
                pro_until = datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc).isoformat()
        if credits == 0 and pro_until is None:
            return jsonify({"status": "ignored", "balance": _balance(user_id)}), 200

        # 5) Idempotente Gutschrift in Supabase (RPC bricht bei bereits verarbeiteter
        #    transactionId ab -> kein Doppel-Gutschreiben)
        try:
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/apply_apple_purchase",
                headers=_supabase_headers(),
                json={
                    "p_user_id": user_id,
                    "p_transaction_id": str(transaction_id),
                    "p_product_id": product_id,
                    "p_credits": int(credits),
                    "p_pro_until": pro_until,
                },
                timeout=8)
            if r.status_code not in (200, 204):
                print(f"[IAP] RPC failed: {r.status_code} {r.text[:200]}")
                return jsonify({"error": "Gutschrift fehlgeschlagen"}), 500
        except Exception as e:
            print(f"[IAP] RPC error: {e}")
            return jsonify({"error": "Gutschrift fehlgeschlagen"}), 500

        print(f"[IAP] redeemed product={product_id} txn={transaction_id} user={user_id[:8]}")
        return jsonify({
            "status": "ok",
            "product": product_id,
            "balance": _balance(user_id),
            "pro": user_is_pro(user_id),
        }), 200
