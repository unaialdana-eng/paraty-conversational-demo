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
        .hotel-card {
            background: #FFFFFF;
            border: 1px solid #E5E9EF;
            padding: 0;
            margin: 1.2rem 0;
            border-radius: 6px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(15, 52, 87, 0.08);
            transition: box-shadow 0.2s, transform 0.2s;
        }
        .hotel-card:hover {
            box-shadow: 0 6px 20px rgba(15, 52, 87, 0.12);
            transform: translateY(-1px);
        }
        .hotel-card-image {
            width: 100%;
            height: 180px;
            object-fit: cover;
            display: block;
            background: linear-gradient(135deg, #0088CC 0%, #0F3457 100%);
        }
        .hotel-card-top { height: 4px; background: linear-gradient(90deg, #0088CC 0%, #00D0B7 100%); }
        .hotel-card-body { padding: 1.4rem 1.6rem 1.5rem; }
        .hotel-header-row {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
            margin-bottom: 0.2rem;
        }
        .hotel-name {
            font-family: 'Montserrat', sans-serif;
            font-size: 1.3rem;
            font-weight: 700;
            color: #0F3457;
            margin: 0;
            line-height: 1.25;
            letter-spacing: -0.01em;
        }
        .hotel-rating {
            flex-shrink: 0;
            background: #F7F9FB;
            color: #0F3457;
            padding: 0.3rem 0.6rem;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 700;
            border: 1px solid #E5E9EF;
            white-space: nowrap;
        }
        .hotel-rating .star { color: #FFB400; margin-right: 0.2rem; }
        .hotel-meta {
            color: #6B7280;
            font-size: 0.82rem;
            margin: 0.25rem 0 1rem;
            font-weight: 500;
            letter-spacing: 0.01em;
        }
        .hotel-justification {
            font-family: 'Montserrat', sans-serif;
            font-weight: 400;
            color: #374151;
            font-size: 0.97rem;
            line-height: 1.6;
            margin: 0.4rem 0 1.1rem;
            background: #F7F9FB;
            border-left: 3px solid #0088CC;
            padding: 0.85rem 1rem;
            border-radius: 0 4px 4px 0;
        }
        .price-block {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            padding: 1rem 0 0.8rem;
            border-top: 1px solid #F0F2F5;
            margin-top: 1rem;
            flex-wrap: wrap;
        }
        .price-left { display: flex; flex-direction: column; gap: 0.2rem; }
        .price-direct {
            font-family: 'Montserrat', sans-serif;
            font-size: 1.7rem;
            font-weight: 700;
            color: #0F3457;
            line-height: 1;
        }
        .price-direct small { font-weight: 500; color: #6B7280; font-size: 0.75rem; margin-left: 0.3rem; }
        .price-booking {
            color: #9CA3AF;
            text-decoration: line-through;
            font-size: 0.82rem;
            font-weight: 500;
        }
        .price-right { display: flex; flex-direction: column; align-items: flex-end; gap: 0.5rem; }
        .savings-banner {
            display: inline-block;
            background: #E6FBF7;
            color: #007A6B;
            padding: 0.35rem 0.75rem;
            font-family: 'Montserrat', sans-serif;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            border-radius: 4px;
            border: 1px solid #A8EFE3;
            white-space: nowrap;
        }
        .cta, a.cta, a.cta:link, a.cta:visited, a.cta:hover, a.cta:active {
            display: inline-block;
            background: #0088CC;
            color: #FFFFFF !important;
            padding: 0.6rem 1.4rem;
            font-family: 'Montserrat', sans-serif;
            font-size: 0.82rem;
            font-weight: 600;
            letter-spacing: 0.03em;
            border-radius: 4px;
            text-decoration: none !important;
            transition: background 0.15s;
            border: none;
        }
        a.cta:hover { background: #0099E6; }
        .amenity-chips { margin: 0.5rem 0 0.2rem; }
        .chip {
            display: inline-block;
            background: #F0F7FC;
            color: #006699;
            padding: 0.25rem 0.7rem;
            font-size: 0.72rem;
            border-radius: 12px;
            margin: 0.15rem 0.3rem 0.15rem 0;
            font-family: 'Montserrat', sans-serif;
            font-weight: 600;
            border: 1px solid #D6EAF5;
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
    amenities_html = "".join(
        f'<span class="chip">{a.replace("_", " ")}</span>' for a in h.get("amenities", [])[:6]
    )
    rating = h.get("avg_rating", "—")
    rating_html = (
        f'<div class="hotel-rating"><span class="star">★</span>{rating}</div>'
        if rating != "—" else ""
    )
    night_label = f"{nights} night{'s' if nights > 1 else ''}"
    image_html = (
        f'<img class="hotel-card-image" src="{h["image_url"]}" alt="{h["name"]}" loading="lazy">'
        if h.get("image_url") else ""
    )
    st.markdown(
        f"""
        <div class="hotel-card">
          {image_html}
          <div class="hotel-card-top"></div>
          <div class="hotel-card-body">
            <div class="hotel-header-row">
              <p class="hotel-name">{h['name']}</p>
              {rating_html}
            </div>
            <p class="hotel-meta">{h['city']}, {h['country']} · {h.get('rooms', '—')} rooms</p>
            <div class="hotel-justification">{justification}</div>
            <div class="amenity-chips">{amenities_html}</div>
            <div class="price-block">
              <div class="price-left">
                <span class="price-direct">€{savings['direct_total']:.0f}<small>direct · {night_label}</small></span>
                <span class="price-booking">€{savings['booking_total']:.0f} on Booking.com</span>
              </div>
              <div class="price-right">
                <span class="savings-banner">Save €{savings['saving_total']:.0f} · {savings['saving_pct']}% vs OTA</span>
                <a class="cta" href="#">Book direct →</a>
              </div>
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
