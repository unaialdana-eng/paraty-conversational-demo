"""
Mock Paraty Booking Engine — structured filter + score.

In production, this layer would query the real Paraty BE API. Here it operates
on hotels.json as static inventory. The logic is intentionally simple so that
the focus is on the agent-orchestration pattern, not the ranking algorithm.
"""

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
