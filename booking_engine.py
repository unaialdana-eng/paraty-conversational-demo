"""
Mock Paraty Booking Engine — structured filter + score.

In production, this layer would query the real Paraty BE API. Here it operates
on hotels.json as static inventory. The logic is intentionally simple so that
the focus is on the agent-orchestration pattern, not the ranking algorithm.
"""

from __future__ import annotations

import json
from pathlib import Path
from difflib import SequenceMatcher

_HOTELS_PATH = Path(__file__).with_name("hotels.json")


def load_hotels() -> list:
    with open(_HOTELS_PATH) as f:
        return json.load(f)


def _city_matches(query_city: str, hotel_city: str) -> float:
    """Fuzzy string similarity; handles 'Malaga' / 'Málaga' / 'CDMX' / 'Ciudad de México'."""
    if not query_city or not hotel_city:
        return 0.0
    q = query_city.lower().strip()
    h = hotel_city.lower().strip()
    if q == h:
        return 1.0
    # Normalise common accents manually (avoid unicodedata import for simplicity)
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        q = q.replace(a, b)
        h = h.replace(a, b)
    if q == h:
        return 1.0
    # Substring match (e.g. "malaga" in "malaga centro")
    if q in h or h in q:
        return 0.9
    # Common city aliases
    aliases = {
        "cdmx": "ciudad de mexico",
        "mexico city": "ciudad de mexico",
        "df": "ciudad de mexico",
        "ba": "buenos aires",
        "bsas": "buenos aires",
        "tokio": "tokyo",
        "lisbon": "lisboa",
        "rome": "roma",
        "seville": "sevilla",
    }
    if aliases.get(q) == h or aliases.get(h) == q:
        return 1.0
    return SequenceMatcher(None, q, h).ratio()


def filter_and_rank(intent: dict, all_hotels: list, top_n: int = 4) -> list:
    """Apply intent filters + score each hotel. Returns top_n with a 'match_score' field."""
    destination = (intent.get("destination") or "").strip()
    max_price = intent.get("max_price_per_night")
    required_amenities = set(intent.get("required_amenities") or [])
    property_type = intent.get("property_type")
    trip_purpose = intent.get("trip_purpose")

    scored = []
    for h in all_hotels:
        score = 0.0
        reasons = []

        # City match — hard gate at 0.7; below that, exclude
        city_score = _city_matches(destination, h["city"])
        if destination and city_score < 0.7:
            continue
        score += city_score * 40  # up to 40 pts
        if city_score >= 0.9:
            reasons.append("exact-city match")

        # Price gate
        if max_price and h["direct_price_per_night"] > max_price:
            continue  # over budget — exclude
        if max_price:
            price_gap = max_price - h["direct_price_per_night"]
            score += min(price_gap / max_price * 20, 20)  # up to 20 pts for under-budget
            reasons.append(f"direct €{h['direct_price_per_night']}/night under €{max_price} cap")

        # Amenity match
        hotel_amenities = set(h.get("amenities", []))
        matched = required_amenities & hotel_amenities
        missing = required_amenities - hotel_amenities
        if required_amenities:
            if missing:
                # if over half the required amenities missing, exclude
                if len(missing) > len(required_amenities) / 2:
                    continue
            score += (len(matched) / max(len(required_amenities), 1)) * 20
            if matched:
                reasons.append(f"{len(matched)}/{len(required_amenities)} requested amenities")

        # Property-type preference
        if property_type and h.get("property_type") == property_type:
            score += 5

        # Trip purpose alignment
        if trip_purpose == "family" and h.get("family_friendly"):
            score += 10
            reasons.append("family-friendly")
        if trip_purpose == "business" and h.get("business_friendly"):
            score += 10
            reasons.append("business-friendly")
        if trip_purpose == "couples" and not h.get("family_friendly"):
            score += 4  # soft signal: adult-oriented

        # Paraty partnership longevity as small quality signal
        since = h.get("paraty_client_since")
        if since and since <= 2022:
            score += 3  # long-standing partner

        # Rating
        rating = h.get("avg_rating", 0)
        score += (rating - 4.0) * 5  # each 0.1 above 4.0 = 0.5 pts

        scored.append({**h, "match_score": round(score, 2), "match_reasons": reasons})

    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return scored[:top_n]


def get_hotel_by_id(hotel_id: str) -> dict | None:
    for h in load_hotels():
        if h["id"] == hotel_id:
            return h
    return None


def compute_savings(hotel: dict, nights: int = 1) -> dict:
    """Compute the direct-vs-Booking.com savings for display."""
    direct = hotel["direct_price_per_night"] * nights
    booking = hotel["booking_com_price_per_night"] * nights
    saving = booking - direct
    pct = (saving / booking) * 100 if booking > 0 else 0
    return {
        "nights": nights,
        "direct_total": direct,
        "booking_total": booking,
        "saving_total": saving,
        "saving_pct": round(pct, 1),
    }


def fallback_addons(intent: dict, hotel: dict) -> list:
    """Heuristic add-on suggestions used when Claude is unavailable."""
    children = intent.get("children") or 0
    amenities = set(hotel.get("amenities", []))
    city = hotel.get("city", "the area")

    addons: list = []

    # Airport transfer — always relevant; reason shifts with family/solo
    if children > 0:
        addons.append({
            "title": f"Airport transfer to {hotel['name']}",
            "reason": "Private car with child seat — skip the taxi queue after a long travel day",
            "price": "+ €42",
        })
    else:
        addons.append({
            "title": f"Airport transfer to {hotel['name']}",
            "reason": f"Door-to-door to {city} — typically 25 min vs 40+ min in public transport",
            "price": "+ €38",
        })

    # Welcome item tied to property type
    if hotel.get("property_type") == "boutique":
        addons.append({
            "title": "Welcome local wine",
            "reason": f"Small-batch bottle curated by the hosts — pairs with the {city} cheese plate",
            "price": "+ €15",
        })
    else:
        addons.append({
            "title": "In-room welcome basket",
            "reason": "Local snacks and water for your arrival day — the hosts arrange it in advance",
            "price": "+ €18",
        })

    # Third addon — family pack if kids, experience if couples, work kit if business
    if children > 0:
        addons.append({
            "title": "Kids amenities pack",
            "reason": "Pool inflatables, colouring set, sun hat — included for under-12s",
            "price": "Free",
        })
    elif intent.get("trip_purpose") == "business":
        addons.append({
            "title": "Late checkout + desk kit",
            "reason": "Keep the room until 4pm with a full work setup — worth it on travel days",
            "price": "+ €25",
        })
    else:
        addons.append({
            "title": "Morning coffee for two on the terrace",
            "reason": "Small ritual the hosts set up before breakfast opens — quiet start",
            "price": "+ €12",
        })

    return addons


def fallback_neighbourhood(city: str) -> list:
    """Generic walkable landmarks used when Claude is unavailable. Keyed by city when possible."""
    city_low = (city or "").lower()
    generic = [
        {"name": "Old town plaza", "walk_time": "5 min walk", "icon": "plaza"},
        {"name": "Central market", "walk_time": "8 min walk", "icon": "market"},
        {"name": "Main cathedral", "walk_time": "7 min walk", "icon": "monument"},
        {"name": "Waterfront promenade", "walk_time": "10 min walk", "icon": "port"},
        {"name": "Historic museum", "walk_time": "6 min walk", "icon": "museum"},
        {"name": "City park", "walk_time": "9 min walk", "icon": "park"},
    ]
    curated = {
        "málaga": [
            {"name": "Plaza de la Merced", "walk_time": "4 min walk", "icon": "plaza"},
            {"name": "Alcazaba", "walk_time": "8 min walk", "icon": "monument"},
            {"name": "Centre Pompidou Málaga", "walk_time": "6 min walk", "icon": "museum"},
            {"name": "Mercado Atarazanas", "walk_time": "9 min walk", "icon": "market"},
            {"name": "Port of Málaga", "walk_time": "10 min walk", "icon": "port"},
            {"name": "Playa de la Malagueta", "walk_time": "14 min walk", "icon": "beach"},
        ],
        "palma": [
            {"name": "Plaça Major", "walk_time": "5 min walk", "icon": "plaza"},
            {"name": "La Seu Cathedral", "walk_time": "8 min walk", "icon": "monument"},
            {"name": "Mercat de l'Olivar", "walk_time": "6 min walk", "icon": "market"},
            {"name": "Es Baluard museum", "walk_time": "9 min walk", "icon": "museum"},
            {"name": "Paseo Marítimo", "walk_time": "12 min walk", "icon": "port"},
            {"name": "Passeig des Born", "walk_time": "4 min walk", "icon": "park"},
        ],
    }
    return curated.get(city_low, generic)


def fallback_owner_voice(hotel: dict) -> dict:
    """Template owner voice used when Claude is unavailable."""
    city = hotel.get("city", "the city")
    rooms = hotel.get("rooms", 20)
    desc = hotel.get("description", "")
    ptype = hotel.get("property_type", "hotel").replace("_", " ")
    return {
        "owner_voice": [
            f"We run a {rooms}-room {ptype} in {city}. {desc.split('.')[0]}." if desc else
            f"We run a {rooms}-room {ptype} in {city}, focused on the details that matter to our guests.",
            "Breakfast is always on, the rooms are quiet, and our staff know which bars to avoid. Small enough to remember your name, serious enough to run properly.",
        ],
        "owner_signature": f"The team at {hotel.get('name', 'the hotel')}",
    }


def fallback_proactive_questions(intent: dict, hotel: dict) -> list:
    amenities = set(hotel.get("amenities", []))
    children = intent.get("children") or 0
    purpose = intent.get("trip_purpose")

    qs = []
    if "pool" in amenities or "rooftop_pool" in amenities:
        if children > 0:
            qs.append("Is the pool suitable for young kids?")
        else:
            qs.append("Is the pool open year-round?")
    qs.append("How noisy are the rooms at night?")
    if children > 0:
        qs.append("Can we get connecting rooms?")
    else:
        qs.append("Which room should we request?")
    if "breakfast_included" in amenities:
        if children > 0:
            qs.append("What's breakfast like for kids?")
        else:
            qs.append("What's breakfast like?")
    elif purpose == "business":
        qs.append("Is there a proper workspace in the room?")
    else:
        qs.append("Is the neighbourhood safe at night?")
    return qs[:4]


def fallback_contextual_alert(intent: dict, hotel: dict) -> str:
    children = intent.get("children") or 0
    nights = intent.get("nights") or 0
    city = hotel.get("city", "the city")
    if children > 0:
        return (
            f"Travelling with young children? <strong>{city}</strong> runs hot in July afternoons — "
            f"most families plan pool time between <em>2pm–5pm</em> and save sightseeing for the cooler evenings."
        )
    if nights and nights <= 2:
        return (
            f"Only <strong>{nights} night{'s' if nights > 1 else ''}</strong> — worth pre-booking any restaurant "
            f"you want. Good {city} places fill up fast for short stays."
        )
    return (
        f"<strong>Tip:</strong> ask at check-in for the staff's own dinner recommendation — "
        f"every {city} hotel has one, and it's usually better than any guidebook pick."
    )
