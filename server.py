"""
Paraty — FastAPI server. Renders the 4-page demo (+ explainer landing) using
Jinja templates that match the Claude Design HTML exports. Reuses the existing
prompts.py + booking_engine.py for intent parsing, filtering and justification.

Run:   uvicorn server:app --reload --port 8000
Open:  http://localhost:8000
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from booking_engine import (
    compute_savings,
    fallback_addons,
    fallback_contextual_alert,
    fallback_neighbourhood,
    fallback_owner_voice,
    fallback_proactive_questions,
    filter_and_rank,
    get_hotel_by_id,
    load_hotels,
)
from prompts import (
    CONCIERGE_OPENING_SYSTEM,
    INTENT_PARSER_SYSTEM,
    JUSTIFIER_SYSTEM,
    PROPERTY_PAGE_SYSTEM,
    build_concierge_opening_request,
    build_intent_parse_request,
    build_justifier_request,
    build_property_page_request,
)

BASE_DIR = Path(__file__).parent
CLAUDE_MODEL = "claude-sonnet-4-6"

app = FastAPI(title="Paraty AI Booking — Demo")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# In-memory cache for Claude outputs — keyed by hash of relevant inputs.
_claude_cache: dict[str, Any] = {}


# ============================================================
# Claude helpers (graceful mock fallback)
# ============================================================
def _get_client():
    if not _ANTHROPIC_AVAILABLE:
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def _cache_key(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(blob.encode()).hexdigest()


# ============================================================
# Intent parse (re-uses same pattern as streamlit_app.py)
# ============================================================
def parse_intent(user_query: str) -> dict:
    if not user_query or not user_query.strip():
        return _empty_intent()

    key = _cache_key("intent", user_query)
    if key in _claude_cache:
        return _claude_cache[key]

    client = _get_client()
    if client is None:
        intent = _mock_intent(user_query)
        _claude_cache[key] = intent
        return intent

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=INTENT_PARSER_SYSTEM,
            messages=build_intent_parse_request(user_query),
        )
        intent = _extract_json(msg.content[0].text)
        _claude_cache[key] = intent
        return intent
    except Exception:
        intent = _mock_intent(user_query)
        _claude_cache[key] = intent
        return intent


def _empty_intent() -> dict:
    return {
        "destination": "",
        "country": None,
        "check_in": None,
        "check_out": None,
        "nights": None,
        "adults": 2,
        "children": 0,
        "max_price_per_night": None,
        "currency": "EUR",
        "required_amenities": [],
        "property_type": None,
        "trip_purpose": None,
    }


def _mock_intent(q: str) -> dict:
    q_low = q.lower()
    intent = _empty_intent()
    for city in [
        "málaga", "malaga", "marbella", "palma", "mallorca", "sevilla", "seville",
        "barcelona", "madrid", "porto", "lisboa", "lisbon", "albufeira",
        "cancún", "cancun", "ciudad de méxico", "cdmx", "mexico city",
        "cartagena", "santiago", "buenos aires", "punta cana",
        "marrakech", "santorini", "tokyo", "tokio", "roma", "rome",
    ]:
        if city in q_low:
            intent["destination"] = city.title()
            break
    m = re.search(r"(?:under|less than|menos de|bajo)\s*[€$]?\s*(\d+)", q_low)
    if m:
        intent["max_price_per_night"] = int(m.group(1))
    m = re.search(r"(\d+)\s*(?:nights|noches)", q_low)
    if m:
        intent["nights"] = int(m.group(1))
    amenity_map = {
        "pool": "pool", "piscina": "pool",
        "beach": "beach_access", "playa": "beach_access",
        "gym": "gym", "gimnasio": "gym",
        "spa": "spa",
        "breakfast": "breakfast_included", "desayuno": "breakfast_included",
        "wifi": "free_wifi",
        "family": "family_room", "familia": "family_room", "niñ": "family_room",
        "rooftop": "rooftop_bar",
    }
    for k, v in amenity_map.items():
        if k in q_low and v not in intent["required_amenities"]:
            intent["required_amenities"].append(v)
    if "family" in q_low or "familia" in q_low or "niñ" in q_low or "kids" in q_low:
        intent["trip_purpose"] = "family"
    elif "business" in q_low or "negocio" in q_low:
        intent["trip_purpose"] = "business"
    elif "honeymoon" in q_low or "luna de miel" in q_low or "couple" in q_low or "pareja" in q_low:
        intent["trip_purpose"] = "couples"
    m = re.search(r"(\d+)\s*(?:adults|adultos)", q_low)
    if m:
        intent["adults"] = int(m.group(1))
    m = re.search(r"(\d+)\s*(?:children|child|niñ)", q_low)
    if m:
        intent["children"] = int(m.group(1))
    return intent


# ============================================================
# Justifications (Pass 2 — per-card ai-quote)
# ============================================================
def generate_justifications(user_query: str, hotels: list) -> dict:
    if not hotels:
        return {}
    key = _cache_key("justify", user_query, [h["id"] for h in hotels])
    if key in _claude_cache:
        return _claude_cache[key]

    client = _get_client()
    if client is None:
        out = {h["id"]: _fallback_justification(h) for h in hotels}
        _claude_cache[key] = out
        return out

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=900,
            system=JUSTIFIER_SYSTEM,
            messages=build_justifier_request(user_query, hotels),
        )
        out = _extract_json(msg.content[0].text)
        _claude_cache[key] = out
        return out
    except Exception:
        out = {h["id"]: _fallback_justification(h) for h in hotels}
        _claude_cache[key] = out
        return out


def _fallback_justification(h: dict) -> str:
    parts = [h.get("property_type", "").replace("_", " ").capitalize()]
    parts.append(f"in {h['city']}")
    key_am = [a for a in h.get("amenities", [])
              if a in ("pool", "rooftop_pool", "spa", "beach_access",
                       "family_room", "caldera_view")]
    if key_am:
        parts.append("with " + ", ".join(a.replace("_", " ") for a in key_am[:2]))
    return " ".join(parts).strip() + "."


# ============================================================
# Concierge opening bubble (Results page)
# ============================================================
def generate_concierge_opening(user_query: str, intent: dict, hotels: list) -> str:
    if not hotels:
        return "I couldn't find hotels matching those criteria. Try relaxing the price cap or amenities."

    key = _cache_key("concierge_open", user_query, [h["id"] for h in hotels])
    if key in _claude_cache:
        return _claude_cache[key]

    client = _get_client()
    if client is None:
        out = _fallback_concierge_opening(intent, hotels)
        _claude_cache[key] = out
        return out

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=220,
            system=CONCIERGE_OPENING_SYSTEM,
            messages=build_concierge_opening_request(user_query, intent, hotels),
        )
        out = msg.content[0].text.strip()
        _claude_cache[key] = out
        return out
    except Exception:
        out = _fallback_concierge_opening(intent, hotels)
        _claude_cache[key] = out
        return out


def _fallback_concierge_opening(intent: dict, hotels: list) -> str:
    n = len(hotels)
    dest = intent.get("destination") or "the area"
    purpose = intent.get("trip_purpose")
    max_price = intent.get("max_price_per_night")

    purpose_adj = {
        "family": "family-friendly",
        "business": "business-friendly",
        "couples": "adult-oriented",
    }.get(purpose, "")

    pieces = [f"I found <strong>{n} {purpose_adj + ' ' if purpose_adj else ''}hotels</strong>"]
    pieces.append(f"in {dest}")
    if max_price:
        pieces.append(f"under <em>€{max_price}/night</em>")
    nights = intent.get("nights")
    if nights:
        pieces.append(f"for a <strong>{nights}-night</strong> stay")
    return " ".join(pieces) + ". All offer direct-booking discounts."


# ============================================================
# Property-page content (owner voice, nbhd, concierge, addons)
# ============================================================
def generate_property_content(user_query: str, intent: dict, hotel: dict) -> dict:
    key = _cache_key("prop", user_query, hotel["id"])
    if key in _claude_cache:
        return _claude_cache[key]

    client = _get_client()
    if client is None:
        out = _fallback_property_content(intent, hotel)
        _claude_cache[key] = out
        return out

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1400,
            system=PROPERTY_PAGE_SYSTEM,
            messages=build_property_page_request(user_query, intent, hotel),
        )
        parsed = _extract_json(msg.content[0].text)
        out = _validate_property_content(parsed, intent, hotel)
        _claude_cache[key] = out
        return out
    except Exception:
        out = _fallback_property_content(intent, hotel)
        _claude_cache[key] = out
        return out


def _validate_property_content(data: dict, intent: dict, hotel: dict) -> dict:
    """Ensure required keys and sensible defaults; fall back per-section if missing."""
    owner = data.get("owner_voice") or []
    if not isinstance(owner, list) or len(owner) < 2:
        owner_fb = fallback_owner_voice(hotel)
        owner = owner_fb["owner_voice"]
        signature = owner_fb["owner_signature"]
    else:
        signature = data.get("owner_signature") or fallback_owner_voice(hotel)["owner_signature"]

    nbhd = data.get("neighbourhood") or []
    if not isinstance(nbhd, list) or len(nbhd) < 6:
        nbhd = fallback_neighbourhood(hotel.get("city", ""))

    questions = data.get("proactive_questions") or []
    if not isinstance(questions, list) or len(questions) < 3:
        questions = fallback_proactive_questions(intent, hotel)

    concierge_opening = data.get("concierge_opening") or (
        f"You're looking at <strong>{hotel['name']}</strong> in {hotel['city']}. "
        f"Small enough to feel personal, set up for the stay you described."
    )

    alert = data.get("contextual_alert") or fallback_contextual_alert(intent, hotel)

    addons = data.get("addons") or []
    if not isinstance(addons, list) or len(addons) < 3:
        addons = fallback_addons(intent, hotel)

    return {
        "owner_voice": owner[:2],
        "owner_signature": signature,
        "neighbourhood": nbhd[:6],
        "concierge_opening": concierge_opening,
        "proactive_questions": questions[:4],
        "contextual_alert": alert,
        "addons": addons[:3],
    }


def _fallback_property_content(intent: dict, hotel: dict) -> dict:
    owner_fb = fallback_owner_voice(hotel)
    return {
        "owner_voice": owner_fb["owner_voice"],
        "owner_signature": owner_fb["owner_signature"],
        "neighbourhood": fallback_neighbourhood(hotel.get("city", "")),
        "concierge_opening": (
            f"You're looking at <strong>{hotel['name']}</strong> — {hotel.get('rooms', 'a handful of')} rooms in "
            f"<em>{hotel['city']}</em>, the kind of place that fits the stay you described."
        ),
        "proactive_questions": fallback_proactive_questions(intent, hotel),
        "contextual_alert": fallback_contextual_alert(intent, hotel),
        "addons": fallback_addons(intent, hotel),
    }


# ============================================================
# Concierge conversation (results page) — hybrid
# The opening bubble is Claude-generated; the upsells/cross-sells are
# parameterised templates keyed off intent + the top hotel.
# ============================================================
def build_concierge_conversation(user_query: str, intent: dict, hotels: list) -> dict:
    opening = generate_concierge_opening(user_query, intent, hotels)
    top = hotels[0] if hotels else None
    children = intent.get("children") or 0
    nights = intent.get("nights") or 3

    # Early-return minimal convo if no hotels
    if top is None:
        return {"opening": opening, "bubbles": [], "top_hotel": None}

    savings = compute_savings(top, nights)
    top_name = top["name"]
    top_nightly = top["direct_price_per_night"]

    bubbles = []

    # Proactive framing bubble
    if children > 0:
        bubbles.append({
            "kind": "text",
            "html": "Since you're traveling with a child, would you like me to:",
        })
        bubbles.append({
            "kind": "suggestions",
            "items": [
                "Find hotels with kids clubs",
                "Check beach proximity",
                "Add airport transfer",
            ],
        })
        bubbles.append({
            "kind": "text",
            "html": (
                "By the way — flying in with a young child after a long day is rough. "
                f"Most families I help book a <em>private transfer with a car seat</em> so you can skip the queue. "
                f"It's <strong>€45</strong> direct to {top_name}, usually around 25 minutes. Want me to pencil it in?"
            ),
        })
        bubbles.append({
            "kind": "suggestions",
            "items": ["Yes, add the transfer", "Show me other options"],
        })
        bubbles.append({
            "kind": "text",
            "html": (
                f"Also — {top_name} does a little something for families: a <em>welcome basket</em> "
                f"with local snacks and a colouring book waiting in the room. It's <strong>€28</strong> and the hosts swear by it. "
                f"Small touch, but kids love it."
            ),
        })
    else:
        bubbles.append({
            "kind": "text",
            "html": (
                f"Let me know what matters most — I can dig into any of these:"
            ),
        })
        bubbles.append({
            "kind": "suggestions",
            "items": [
                "Quietest rooms",
                "Closest to the centre",
                "Best for a long weekend",
            ],
        })
        bubbles.append({
            "kind": "text",
            "html": (
                f"One thing I'd flag on {top_name}: guests tell us the <em>best rooms are on the top two floors</em> — "
                f"quieter, better light, and usually the same price if you ask at check-in. Want me to add that note to your booking?"
            ),
        })
        bubbles.append({
            "kind": "suggestions",
            "items": ["Add the room-request note", "Show other quiet options"],
        })

    # Nights optimisation — only if stay is short
    if nights and nights <= 3:
        extended_nightly = int(top_nightly * 0.92)
        extra_nights = max(1, 4 - nights)
        extended_total = extended_nightly * (nights + extra_nights)
        current_total = top_nightly * nights
        delta = current_total + extra_nights * extended_nightly - extended_total
        bubbles.append({
            "kind": "text",
            "html": (
                f"One more thought: if you extend to <em>{nights + extra_nights} nights instead of {nights}</em>, "
                f"{top_name} drops the rate to <strong>€{extended_nightly}/night</strong> — "
                f"you'd save around <strong>€{max(0, int(delta))}</strong> overall and get a slower pace. "
                f"Happy to swap the dates if that sounds better."
            ),
        })
        bubbles.append({
            "kind": "suggestions",
            "items": [f"Try {nights + extra_nights} nights instead", f"Keep {nights} nights"],
        })

    return {
        "opening": opening,
        "bubbles": bubbles,
        "top_hotel": top,
        "ready_price": int(savings["direct_total"]),
        "ready_nights": nights,
    }


# ============================================================
# Routes
# ============================================================
@app.get("/", response_class=HTMLResponse)
def explainer(request: Request):
    return templates.TemplateResponse("explainer.html", {
        "request": request,
        "claude_mode": _get_client() is not None,
    })


@app.get("/app", response_class=HTMLResponse)
def homepage(request: Request):
    return templates.TemplateResponse("homepage.html", {"request": request})


@app.post("/search")
def search(q: str = Form(default=""), where: str = Form(default=""),
           check_in: str = Form(default=""), check_out: str = Form(default=""),
           guests: str = Form(default="")):
    """Accept either a natural-language query (AI concierge input) or classic fields."""
    if q.strip():
        return RedirectResponse(f"/results?q={q.strip()}", status_code=303)
    # Build a synthetic query from classic fields
    parts = []
    if where.strip():
        parts.append(where.strip())
    if check_in and check_out:
        parts.append(f"from {check_in} to {check_out}")
    if guests.strip():
        parts.append(guests.strip())
    synthetic = ", ".join(parts) or "hotels in Málaga"
    return RedirectResponse(f"/results?q={synthetic}", status_code=303)


@app.get("/results", response_class=HTMLResponse)
def results(request: Request, q: str = ""):
    query = q.strip() or "Family hotel in Málaga for 3 nights in July, 2 adults and a child, under €220 per night, pool preferred."
    intent = parse_intent(query)
    all_hotels = load_hotels()
    shortlisted = filter_and_rank(intent, all_hotels, top_n=3)
    justifications = generate_justifications(query, shortlisted)
    conversation = build_concierge_conversation(query, intent, shortlisted)
    nights = intent.get("nights") or 3

    enriched = []
    for h in shortlisted:
        sv = compute_savings(h, nights)
        enriched.append({
            "h": h,
            "just": justifications.get(h["id"]) or _fallback_justification(h),
            "savings": sv,
            "review_count": int((h.get("rooms") or 30) * 8.5),
            "review_label": _review_label(h.get("avg_rating", 0)),
        })

    return templates.TemplateResponse("results.html", {
        "request": request,
        "q": query,
        "intent": intent,
        "nights": nights,
        "cards": enriched,
        "conversation": conversation,
    })


@app.get("/property/{hotel_id}", response_class=HTMLResponse)
def property_page(request: Request, hotel_id: str, q: str = ""):
    hotel = get_hotel_by_id(hotel_id)
    if hotel is None:
        return RedirectResponse("/results", status_code=303)
    query = q.strip() or "Family hotel in Málaga for 3 nights in July, 2 adults and a child, under €220 per night, pool preferred."
    intent = parse_intent(query)
    nights = intent.get("nights") or 3
    savings = compute_savings(hotel, nights)
    content = generate_property_content(query, intent, hotel)

    return templates.TemplateResponse("property.html", {
        "request": request,
        "q": query,
        "hotel": hotel,
        "intent": intent,
        "nights": nights,
        "savings": savings,
        "content": content,
    })


@app.get("/checkout/{hotel_id}", response_class=HTMLResponse)
def checkout(request: Request, hotel_id: str, q: str = ""):
    hotel = get_hotel_by_id(hotel_id)
    if hotel is None:
        return RedirectResponse("/results", status_code=303)
    query = q.strip() or ""
    intent = parse_intent(query) if query else _empty_intent()
    nights = intent.get("nights") or 3
    savings = compute_savings(hotel, nights)
    addons = generate_property_content(query, intent, hotel)["addons"]
    return templates.TemplateResponse("checkout.html", {
        "request": request,
        "q": query,
        "hotel": hotel,
        "intent": intent,
        "nights": nights,
        "savings": savings,
        "addons": addons,
    })


# ============================================================
# Small helpers for templates
# ============================================================
def _review_label(rating: float) -> str:
    if rating >= 4.7:
        return "Exceptional"
    if rating >= 4.4:
        return "Excellent"
    if rating >= 4.1:
        return "Very good"
    return "Good"
