"""
Paraty Conversational Booking Agent — MVP

Streamlit app. User types natural-language travel query; Claude parses intent;
Python filters Paraty inventory; Claude re-enters to write justifications; UI
renders hotel cards with direct-vs-Booking.com savings.

Run: streamlit run app.py
"""

import json
import os
import re
from pathlib import Path

import streamlit as st

# Auto-load .env if present (so ANTHROPIC_API_KEY works without manual export).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# Anthropic SDK is optional — falls back to "mock mode" if not installed / no API key.
try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from booking_engine import load_hotels, filter_and_rank, compute_savings
from prompts import (
    INTENT_PARSER_SYSTEM,
    JUSTIFIER_SYSTEM,
    build_intent_parse_request,
    build_justifier_request,
)

# ============================================================
# Config
# ============================================================
st.set_page_config(
    page_title="Paraty AI Booking — Demo",
    page_icon="🏨",
    layout="centered",
)

CLAUDE_MODEL = "claude-sonnet-4-6"


# ============================================================
# Claude call helpers (with graceful mock fallback)
# ============================================================
def _get_client():
    if not _ANTHROPIC_AVAILABLE:
        return None
    # Try env var first, then Streamlit secrets (which throws if secrets.toml
    # doesn't exist — so wrap it).
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            api_key = st.secrets.get("ANTHROPIC_API_KEY")
        except Exception:
            api_key = None
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def _extract_json(text: str) -> dict:
    """Strip any accidental code fences / prose and parse JSON."""
    text = text.strip()
    # strip ```json ... ``` fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # find outermost JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def parse_intent(user_query: str) -> dict:
    """Pass 1 — intent parsing. Returns dict matching schema; falls back to regex heuristic."""
    client = _get_client()
    if client is None:
        return _mock_intent(user_query)

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=INTENT_PARSER_SYSTEM,
            messages=build_intent_parse_request(user_query),
        )
        text = msg.content[0].text
        return _extract_json(text)
    except Exception as e:
        st.warning(f"Claude parse failed — using heuristic fallback. ({e})")
        return _mock_intent(user_query)


def generate_justifications(user_query: str, hotels: list) -> dict:
    """Pass 2 — ranking justifier. Returns {hotel_id: sentence}; falls back to template."""
    client = _get_client()
    if client is None or not hotels:
        return {h["id"]: _mock_justification(h) for h in hotels}

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=JUSTIFIER_SYSTEM,
            messages=build_justifier_request(user_query, hotels),
        )
        text = msg.content[0].text
        return _extract_json(text)
    except Exception as e:
        st.warning(f"Claude justifier failed — using template fallback. ({e})")
        return {h["id"]: _mock_justification(h) for h in hotels}


# ============================================================
# Mock-mode fallback (works without API key)
# ============================================================
def _mock_intent(q: str) -> dict:
    q_low = q.lower()
    intent = {
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
    # Destination — simple match against known hotel cities
    for city in [
        "málaga", "malaga", "marbella", "palma", "sevilla", "seville",
        "barcelona", "madrid", "porto", "lisboa", "lisbon", "albufeira",
        "cancún", "cancun", "ciudad de méxico", "cdmx", "mexico city",
        "cartagena", "santiago", "buenos aires", "punta cana",
        "marrakech", "santorini", "tokyo", "tokio", "roma", "rome",
    ]:
        if city in q_low:
            intent["destination"] = city.title()
            break
    # Price
    m = re.search(r"(?:under|less than|menos de|bajo)\s*[€$]?\s*(\d+)", q_low)
    if m:
        intent["max_price_per_night"] = int(m.group(1))
    # Nights
    m = re.search(r"(\d+)\s*(?:nights|noches)", q_low)
    if m:
        intent["nights"] = int(m.group(1))
    # Amenities (heuristic keywords → canonical)
    amenity_map = {
        "pool": "pool", "piscina": "pool",
        "beach": "beach_access", "playa": "beach_access",
        "gym": "gym", "gimnasio": "gym",
        "spa": "spa",
        "breakfast": "breakfast_included", "desayuno": "breakfast_included",
        "wifi": "free_wifi",
        "family": "family_room", "familia": "family_room", "niñ": "family_room",
        "business": "business_center", "negocio": "business_center",
        "rooftop": "rooftop_bar",
        "caldera": "caldera_view",
    }
    for k, v in amenity_map.items():
        if k in q_low and v not in intent["required_amenities"]:
            intent["required_amenities"].append(v)
    # Trip purpose
    if "family" in q_low or "familia" in q_low or "niñ" in q_low or "kids" in q_low:
        intent["trip_purpose"] = "family"
    elif "business" in q_low or "negocio" in q_low:
        intent["trip_purpose"] = "business"
    elif "honeymoon" in q_low or "luna de miel" in q_low or "couple" in q_low or "pareja" in q_low:
        intent["trip_purpose"] = "couples"
    # Adults / children
    m = re.search(r"(\d+)\s*(?:adults|adultos)", q_low)
    if m:
        intent["adults"] = int(m.group(1))
    m = re.search(r"(\d+)\s*(?:children|child|niñ)", q_low)
    if m:
        intent["children"] = int(m.group(1))
    return intent


def _mock_justification(h: dict) -> str:
    """Template-based fallback — uses the hotel description if available."""
    parts = []
    if h.get("property_type"):
        parts.append(h["property_type"].replace("_", " "))
    parts.append(f"in {h['city']}")
    if h.get("amenities"):
        key_am = [a for a in h["amenities"] if a in (
            "pool", "rooftop_pool", "infinity_pool", "spa", "beach_access",
            "family_room", "kids_club", "business_center", "caldera_view"
        )]
        if key_am:
            parts.append("with " + ", ".join(a.replace("_", " ") for a in key_am[:2]))
    since = h.get("paraty_client_since")
    if since and since <= 2022:
        parts.append(f"— {2026 - since}-year Paraty direct-booking partner")
    return " ".join(parts).capitalize() + "."


# ============================================================
# Editorial style — injected via raw HTML
# ============================================================
def _inject_style():
    st.markdown(
        """
        <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
        <style>
        html, body, [class*="css"], .stMarkdown, .stTextInput input, .stTextArea textarea, .stButton button {
            font-family: 'Montserrat', -apple-system, BlinkMacSystemFont, sans-serif !important;
        }
        .main .block-container { padding-top: 2rem; max-width: 900px; }
        h1 { font-family: 'Montserrat', sans-serif; color: #0F3457; font-weight: 700; letter-spacing: -0.02em; }
        h2, h3 { font-family: 'Montserrat', sans-serif; color: #0F3457; font-weight: 600; }
        .stTextInput input, .stTextArea textarea { font-size: 15px; color: #57595A; }
        /* ======================================================
           Hotel card — OTA-standard 3-zone layout
           [ IMAGE 260 ][ IDENTITY + FEATURES (flex) ][ PRICE 210 ]
           ====================================================== */
        .hotel-card {
            display: grid;
            grid-template-columns: 260px 1fr 210px;
            background: #FFFFFF;
            border: 1px solid #DFE3E8;
            margin: 0.8rem 0;
            border-radius: 8px;
            overflow: hidden;
            transition: border-color 0.15s, box-shadow 0.15s;
            min-height: 240px;
        }
        .hotel-card:hover {
            border-color: #0088CC;
            box-shadow: 0 4px 16px rgba(15, 52, 87, 0.08);
        }
        .hotel-card-image-wrap {
            position: relative;
            background: linear-gradient(135deg, #0088CC 0%, #0F3457 100%);
            overflow: hidden;
        }
        .hotel-card-image {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
            transition: transform 0.4s ease;
        }
        .hotel-card:hover .hotel-card-image { transform: scale(1.03); }
        .discount-chip {
            position: absolute;
            top: 12px;
            left: 12px;
            background: rgba(15, 52, 87, 0.92);
            color: #FFFFFF;
            padding: 0.32rem 0.6rem;
            font-family: 'Montserrat', sans-serif;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            border-radius: 3px;
            backdrop-filter: blur(4px);
        }
        .partner-badge {
            position: absolute;
            bottom: 12px;
            left: 12px;
            background: rgba(255, 255, 255, 0.95);
            color: #0088CC;
            padding: 0.25rem 0.55rem;
            font-family: 'Montserrat', sans-serif;
            font-size: 0.66rem;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            border-radius: 3px;
        }
        /* ==== Identity + features (middle column) ==== */
        .hotel-identity {
            display: flex;
            flex-direction: column;
            padding: 1.2rem 1.4rem 1.2rem 1.4rem;
            min-width: 0;
            gap: 0.55rem;
        }
        .hotel-header { display: flex; align-items: baseline; gap: 0.6rem; flex-wrap: wrap; }
        .hotel-name {
            font-family: 'Montserrat', sans-serif;
            font-size: 1.15rem;
            font-weight: 700;
            color: #0F3457;
            margin: 0;
            line-height: 1.3;
            letter-spacing: -0.01em;
        }
        .hotel-type-chip {
            display: inline-block;
            background: #F0F7FC;
            color: #006699;
            padding: 0.15rem 0.5rem;
            font-size: 0.66rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            border-radius: 3px;
            white-space: nowrap;
        }
        .hotel-location {
            color: #57595A;
            font-size: 0.83rem;
            font-weight: 500;
            margin: 0;
            display: flex;
            align-items: center;
            gap: 0.35rem;
        }
        .hotel-location .loc-icon { color: #0088CC; font-size: 0.78rem; }
        .hotel-location .loc-sep { color: #CDD5DF; margin: 0 0.1rem; }
        .hotel-location a.map-link {
            color: #0088CC;
            text-decoration: none;
            font-weight: 600;
            font-size: 0.78rem;
        }
        .hotel-location a.map-link:hover { text-decoration: underline; }
        .hotel-justification {
            font-family: 'Montserrat', sans-serif;
            color: #1F2937;
            font-size: 0.9rem;
            line-height: 1.5;
            margin: 0.2rem 0 0.3rem;
            font-weight: 500;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            font-style: italic;
        }
        .hotel-features {
            display: flex;
            flex-wrap: wrap;
            gap: 0.3rem 1rem;
            margin-top: auto;
            padding-top: 0.3rem;
        }
        .feature {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            color: #374151;
            font-size: 0.8rem;
            font-weight: 500;
            white-space: nowrap;
        }
        .feature .check {
            color: #00A896;
            font-weight: 700;
            font-size: 0.85rem;
        }
        /* ==== Price column (right) ==== */
        .hotel-price-col {
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            padding: 1.2rem 1.2rem 1.2rem 1.1rem;
            border-left: 1px solid #EEF0F3;
            gap: 0.5rem;
        }
        .review-block {
            display: flex;
            align-items: center;
            gap: 0.55rem;
        }
        .review-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: #0088CC;
            color: #FFFFFF;
            padding: 0.35rem 0.55rem;
            border-radius: 4px 4px 4px 0;
            font-size: 0.9rem;
            font-weight: 700;
            line-height: 1;
            min-width: 2.3rem;
        }
        .review-pill.excellent { background: #0F3457; }
        .review-pill.very-good { background: #0088CC; }
        .review-pill.good { background: #4A90B8; }
        .review-label {
            display: flex;
            flex-direction: column;
            font-size: 0.78rem;
            line-height: 1.25;
        }
        .review-label .label-text {
            color: #0F3457;
            font-weight: 700;
            font-size: 0.82rem;
        }
        .review-label .count-text {
            color: #6B7280;
            font-size: 0.72rem;
            font-weight: 500;
        }
        .price-block {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            text-align: right;
            margin-top: 0.3rem;
        }
        .price-context {
            color: #6B7280;
            font-size: 0.72rem;
            font-weight: 500;
            margin-bottom: 0.2rem;
            letter-spacing: 0.01em;
        }
        .price-was {
            color: #9CA3AF;
            text-decoration: line-through;
            font-size: 0.82rem;
            font-weight: 500;
            line-height: 1.1;
        }
        .price-now {
            font-family: 'Montserrat', sans-serif;
            font-size: 1.65rem;
            font-weight: 700;
            color: #0F3457;
            line-height: 1.05;
            letter-spacing: -0.01em;
        }
        .price-per-night {
            color: #57595A;
            font-size: 0.73rem;
            font-weight: 500;
            margin-top: 0.15rem;
        }
        .savings-tag {
            display: inline-block;
            background: #E6FBF7;
            color: #007A6B;
            padding: 0.22rem 0.55rem;
            font-size: 0.7rem;
            font-weight: 700;
            border-radius: 3px;
            margin-top: 0.4rem;
            letter-spacing: 0.01em;
        }
        .cta, a.cta, a.cta:link, a.cta:visited, a.cta:hover, a.cta:active {
            display: block;
            width: 100%;
            text-align: center;
            background: #0088CC;
            color: #FFFFFF !important;
            padding: 0.65rem 0.9rem;
            font-family: 'Montserrat', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            letter-spacing: 0.01em;
            border-radius: 4px;
            text-decoration: none !important;
            transition: background 0.15s;
            border: none;
            margin-top: 0.7rem;
        }
        a.cta:hover { background: #0099E6; }
        /* ==== Responsive breakpoints ==== */
        @media (max-width: 860px) {
            .hotel-card { grid-template-columns: 220px 1fr 190px; min-height: 220px; }
            .hotel-identity { padding: 1rem 1.1rem; }
        }
        @media (max-width: 720px) {
            .hotel-card { grid-template-columns: 1fr; grid-template-rows: 180px auto auto; min-height: 0; }
            .hotel-price-col { border-left: none; border-top: 1px solid #EEF0F3; align-items: stretch; flex-direction: row; justify-content: space-between; align-items: center; }
            .review-block { flex-shrink: 0; }
            .price-block { align-items: flex-end; }
            .cta { width: auto; margin-top: 0; padding: 0.55rem 1.4rem; }
        }
        .disclaimer {
            font-size: 0.72rem;
            color: #B3B3B3;
            margin-top: 2rem;
            padding-top: 1rem;
            border-top: 1px solid #E5E7EB;
            line-height: 1.5;
        }
        .brand {
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            font-size: 0.72rem;
            letter-spacing: 0.12em;
            color: #0088CC;
            text-transform: uppercase;
        }
        .accent-bar { width: 40px; height: 3px; background: #0088CC; margin: 0.3rem 0 1rem; }
        /* Primary button — strong cyan fill */
        .stButton button[kind="primary"],
        button[data-testid="baseButton-primary"] {
            background: #0088CC !important;
            border: 1px solid #0088CC !important;
            color: #FFFFFF !important;
            font-weight: 700 !important;
            letter-spacing: 0.03em !important;
            padding: 0.65rem 1.4rem !important;
            font-size: 0.9rem !important;
            border-radius: 4px !important;
            box-shadow: 0 2px 4px rgba(0, 136, 204, 0.2) !important;
        }
        .stButton button[kind="primary"]:hover,
        button[data-testid="baseButton-primary"]:hover {
            background: #0099E6 !important;
            border-color: #0099E6 !important;
            box-shadow: 0 4px 8px rgba(0, 136, 204, 0.3) !important;
        }
        /* Secondary buttons (Example 1/2/3) — outlined cyan, visible */
        .stButton button[kind="secondary"],
        button[data-testid="baseButton-secondary"] {
            background: #FFFFFF !important;
            border: 1.5px solid #0088CC !important;
            color: #0088CC !important;
            font-weight: 600 !important;
            padding: 0.55rem 1rem !important;
            font-size: 0.83rem !important;
            border-radius: 4px !important;
            transition: all 0.15s !important;
        }
        .stButton button[kind="secondary"]:hover,
        button[data-testid="baseButton-secondary"]:hover {
            background: #F0F7FC !important;
            border-color: #0099E6 !important;
            color: #006699 !important;
        }
        /* Text input — stronger contrast */
        .stTextArea textarea, .stTextInput input {
            color: #0F3457 !important;
            border: 1.5px solid #D6DBE3 !important;
            border-radius: 4px !important;
        }
        .stTextArea textarea:focus, .stTextInput input:focus {
            border-color: #0088CC !important;
            box-shadow: 0 0 0 2px rgba(0, 136, 204, 0.15) !important;
        }
        /* Expander styling */
        .streamlit-expanderHeader {
            background: #F7F9FB !important;
            border: 1px solid #E5E9EF !important;
            border-radius: 4px !important;
            font-weight: 600 !important;
            color: #0F3457 !important;
        }
        .about-box {
            background: #F7F7F7;
            border-left: 3px solid #0088CC;
            padding: 1.1rem 1.3rem;
            margin: 1rem 0;
            border-radius: 3px;
            font-size: 0.88rem;
            line-height: 1.6;
            color: #57595A;
        }
        .about-box strong { color: #0F3457; font-weight: 600; }
        .about-box h4 {
            font-family: 'Montserrat', sans-serif;
            color: #0F3457;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin: 0.9rem 0 0.3rem;
        }
        .about-box h4:first-child { margin-top: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# Rendering
# ============================================================
def _render_header():
    st.markdown('<div class="brand">Paraty Tech · AI Booking Demo</div>', unsafe_allow_html=True)
    st.markdown('<div class="accent-bar"></div>', unsafe_allow_html=True)
    st.markdown(
        "<h1 style='margin-top:0;'>Ask. Compare. Book direct.</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='color:#57595A; font-size:1rem; font-weight:400; margin-top:-0.3rem;'>"
        "A working prototype of the AI-agent layer proposed in Move 1 of the "
        "12-month product agenda."
        "</p>",
        unsafe_allow_html=True,
    )


def _render_about():
    with st.expander("About this demo — purpose, technology, caveats", expanded=False):
        st.markdown(
            """
            <div class="about-box">
              <h4>Purpose</h4>
              Live artifact accompanying the CPO deck. Demonstrates Move 1 of the
              proposed 12-month product agenda — a conversational AI layer over
              Paraty's direct-booking engine — running end-to-end, not as slideware.

              <h4>Objective</h4>
              Make Paraty's core value proposition tangible: natural-language access
              to the inventory, with the direct-vs-OTA savings surfaced per hotel.
              Prove the two-pass LLM pattern (parse intent → deterministic Python
              filter → LLM justification) is robust and shippable.

              <h4>Technology</h4>
              Claude Sonnet 4.6 (intent parsing + ranking justifications) · Python
              (deterministic filter/rank with fuzzy city matching) · Streamlit (UI) ·
              mock JSON inventory (60 hotels across Paraty's real geo footprint:
              Spain, Portugal, Italy, Morocco, Greece, LatAm, Japan).

              <h4>Caveats</h4>
              <strong>Mock data</strong> — not connected to Paraty's production
              backend; 60 hand-curated hotels, Kent + Colony (Málaga) are real
              Paraty case-study properties, the rest are plausible but fictional.<br>
              <strong>Booking.com comparison is illustrative</strong> (+12-20%
              deltas per industry benchmarks, not a live feed).<br>
              <strong>Out of scope</strong> for this prototype: auth, payment,
              multi-agent clarifying questions (Move 2), WhatsApp channel (Move 3),
              MCP server exposure, real PMS/CRS integration.

              <h4>Next step — competitive gap analysis</h4>
              Before committing engineering capacity, a structured benchmark vs
              <strong>Mirai, Bookassist, D-EDGE, SiteMinder and TravelClick</strong>
              is the logical next move — (a) identify where a conversational AI
              layer is genuine differentiation vs feature parity, (b) quantify
              conversion-uplift potential against Paraty's current direct-booking
              baseline, (c) surface the verticals (family, honeymoon, business)
              where AI-assisted search shows the highest delta. This demo is the
              hypothesis; the competitive study is what turns it into a
              defensible roadmap.
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_hotel_card(h: dict, justification: str, nights: int):
    savings = compute_savings(h, nights)

    # Features — the top 3 most "booking-relevant" amenities, formatted as checkmark bullets.
    _feature_labels = {
        "pool": "Pool", "rooftop_pool": "Rooftop pool", "infinity_pool": "Infinity pool",
        "beach_access": "Beach access", "spa": "Spa", "gym": "Gym",
        "free_wifi": "Free WiFi", "breakfast_included": "Breakfast included",
        "family_room": "Family rooms", "kids_club": "Kids club",
        "restaurant": "Restaurant", "bar": "Bar",
        "business_center": "Business center", "rooftop_bar": "Rooftop bar",
        "rooftop_terrace": "Rooftop terrace", "all_inclusive": "All-inclusive",
        "air_conditioning": "Air conditioning", "caldera_view": "Caldera view",
        "harbor_view": "Harbour view", "mountain_view": "Mountain view",
        "ocean_view": "Ocean view", "river_view": "River view",
        "garden": "Garden", "courtyard": "Courtyard", "onsen": "Private onsen",
        "coworking": "Coworking", "shuttle": "Airport shuttle",
    }
    _priority = [
        "pool", "infinity_pool", "rooftop_pool", "beach_access", "spa",
        "breakfast_included", "family_room", "kids_club", "gym",
        "rooftop_terrace", "rooftop_bar", "caldera_view", "harbor_view",
        "ocean_view", "mountain_view", "onsen", "all_inclusive",
        "business_center", "restaurant", "free_wifi",
    ]
    hotel_amenities = h.get("amenities", [])
    ordered = [a for a in _priority if a in hotel_amenities]
    ordered += [a for a in hotel_amenities if a not in ordered]
    top_features = ordered[:4]
    features_html = "".join(
        f'<span class="feature"><span class="check">✓</span>{_feature_labels.get(a, a.replace("_", " ").title())}</span>'
        for a in top_features
    )

    rating = h.get("avg_rating") or 0
    # Review-score classification
    if rating >= 4.7:
        rclass, rlabel = "excellent", "Exceptional"
    elif rating >= 4.4:
        rclass, rlabel = "excellent", "Excellent"
    elif rating >= 4.1:
        rclass, rlabel = "very-good", "Very good"
    else:
        rclass, rlabel = "good", "Good"
    review_count = int(h.get("rooms", 30) * 8.5)  # synthetic review count — stable per hotel
    review_html = (
        f'''<div class="review-block">
              <div class="review-label">
                <span class="label-text">{rlabel}</span>
                <span class="count-text">{review_count:,} reviews</span>
              </div>
              <div class="review-pill {rclass}">{rating:.1f}</div>
            </div>'''
        if rating else ""
    )

    night_label = f"{nights} night{'s' if nights > 1 else ''}"
    guests_label = "2 guests"  # default — could be pulled from intent

    image_html = (
        f'<img class="hotel-card-image" src="{h["image_url"]}" alt="{h["name"]}" loading="lazy">'
        if h.get("image_url") else ""
    )
    discount_chip = (
        f'<div class="discount-chip">−{savings["saving_pct"]}%</div>'
        if savings["saving_pct"] >= 10 else ""
    )
    paraty_since = h.get("paraty_client_since")
    partner_badge = (
        f'<div class="partner-badge">Paraty Direct</div>'
        if paraty_since and paraty_since <= 2023 else ""
    )

    property_type = h.get("property_type", "").replace("_", " ").title()
    type_chip = (
        f'<span class="hotel-type-chip">{property_type}</span>'
        if property_type else ""
    )

    st.markdown(
        f"""
        <div class="hotel-card">
          <div class="hotel-card-image-wrap">
            {image_html}
            {discount_chip}
            {partner_badge}
          </div>
          <div class="hotel-identity">
            <div class="hotel-header">
              <p class="hotel-name">{h['name']}</p>
              {type_chip}
            </div>
            <p class="hotel-location">
              <span class="loc-icon">●</span>
              {h['city']}, {h['country']}
              <span class="loc-sep">·</span>
              {h.get('rooms', '—')} rooms
            </p>
            <p class="hotel-justification">{justification}</p>
            <div class="hotel-features">{features_html}</div>
          </div>
          <div class="hotel-price-col">
            {review_html}
            <div class="price-block">
              <span class="price-context">{night_label} · {guests_label}</span>
              <span class="price-was">€{savings['booking_total']:.0f}</span>
              <span class="price-now">€{savings['direct_total']:.0f}</span>
              <span class="price-per-night">€{h['direct_price_per_night']:.0f} / night</span>
              <span class="savings-tag">Save €{savings['saving_total']:.0f} direct</span>
              <a class="cta" href="#">Book direct</a>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_intent_debug(intent: dict):
    with st.expander("Parsed intent (debug)"):
        st.json(intent)


# ============================================================
# Main flow
# ============================================================
def main():
    _inject_style()
    _render_header()
    _render_about()

    # Mock-mode banner if no API key
    if _get_client() is None:
        st.info(
            "ℹ Running in **heuristic mock mode** — no Anthropic API key detected. "
            "Set `ANTHROPIC_API_KEY` in your environment (or `.streamlit/secrets.toml`) "
            "to enable Claude for intent parsing + natural-language justifications. "
            "The flow still works with a regex fallback; responses are just less nuanced."
        )

    # Example queries — click to populate. Four flagship ES cities.
    st.markdown("<br>**Try an example:**", unsafe_allow_html=True)
    cols = st.columns(4)
    examples = [
        ("Málaga · family", "Family hotel in Málaga for 3 nights in July, 2 adults and a child, under €220 per night, pool preferred."),
        ("Madrid · business", "Business trip Madrid next week, single, need gym and wifi, under €250."),
        ("Barcelona · design", "Weekend Barcelona, pareja, boutique de diseño en El Born o Gothic Quarter, rooftop con vista, bajo 260€."),
        ("Mallorca · luna de miel", "Luna de miel 4 noches en Mallorca, vista mar o montaña, piscina y spa, ambiente adulto, hasta 300€ por noche."),
    ]
    for i, col in enumerate(cols):
        with col:
            label, query = examples[i]
            if st.button(label, key=f"ex{i}", use_container_width=True):
                st.session_state.user_query = query

    user_query = st.text_area(
        "Your travel query",
        value=st.session_state.get("user_query", ""),
        height=100,
        placeholder="Describe your trip in plain language — city, dates, who, budget, what matters.",
        label_visibility="collapsed",
    )

    submitted = st.button("Find direct", type="primary", use_container_width=True)

    if submitted and user_query.strip():
        with st.spinner("Parsing intent and querying Paraty inventory..."):
            intent = parse_intent(user_query)
            all_hotels = load_hotels()
            shortlisted = filter_and_rank(intent, all_hotels, top_n=4)
            justifications = generate_justifications(user_query, shortlisted)

        _render_intent_debug(intent)

        if not shortlisted:
            st.warning(
                "No hotels in the Paraty inventory match those criteria. "
                "Try relaxing price, amenities, or destination."
            )
        else:
            nights = intent.get("nights") or 1
            st.markdown(
                f"<br><p class='brand'>{len(shortlisted)} Paraty-network matches</p>"
                f"<div class='accent-bar'></div>",
                unsafe_allow_html=True,
            )
            for h in shortlisted:
                just = justifications.get(h["id"]) or _mock_justification(h)
                _render_hotel_card(h, just, nights)

    st.markdown(
        """
        <div class="disclaimer">
          Prototype built by Unai Aldana · April 2026 · demonstrating the architecture
          proposed in Move 1 of the Paraty Tech CPO 12-month product agenda.
          Mock data — 60 hand-curated hotels. Not affiliated with Paraty Tech or any
          listed hotel. Booking.com prices shown are illustrative deltas, not live.
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
