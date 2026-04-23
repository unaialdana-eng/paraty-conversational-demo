"""
LLM prompts — intent parsing (Pass 1) + ranking justification (Pass 2).

Two-pass architecture: Claude does natural-language understanding; Python does
structured filtering. This is deliberately the same pattern the Paraty AI-agent
layer would use in production.
"""

INTENT_PARSER_SYSTEM = """You are a travel-query parser for Paraty Tech, a hotel direct-booking platform. Your only job: convert a human natural-language query into a strict JSON object matching the schema below.

Return ONLY valid JSON. No prose, no explanation, no markdown fences.

Schema:
{
  "destination": string,          // city or region; English or Spanish OK
  "country": string|null,         // ISO-style name if inferable, else null
  "check_in": string|null,        // YYYY-MM-DD if derivable from query; else null
  "check_out": string|null,       // YYYY-MM-DD if derivable; else null
  "nights": integer|null,         // if user said "3 nights", etc.
  "adults": integer,              // default 2 if not stated
  "children": integer,            // default 0 if not stated
  "max_price_per_night": number|null,  // EUR or USD — whatever the user used
  "currency": string,             // "EUR" default, "USD" if US/LATAM cue
  "required_amenities": [string], // from the canonical list below; empty array if none
  "property_type": string|null,   // "boutique" | "mid_chain" | null
  "trip_purpose": string|null     // "family" | "business" | "couples" | "leisure" | null
}

Canonical amenities (use ONLY these exact strings):
pool, rooftop_pool, infinity_pool, beach_access, spa, gym, restaurant, bar,
rooftop_bar, rooftop_terrace, family_room, kids_club, business_center,
free_wifi, air_conditioning, breakfast_included, all_inclusive, courtyard,
harbor_view, caldera_view, river_view

Behaviour:
- Map synonyms: "piscina"→pool, "gimnasio"→gym, "desayuno"→breakfast_included, "familia"/"niños"→family_room and trip_purpose="family".
- If user says "pet-friendly", "mascota", etc., map closest or leave out (not in canonical list).
- Price: "under 200", "less than €200", "menos de 200€" → max_price_per_night=200, currency="EUR".
- Infer trip_purpose from context: "2 adultos y un niño"→family; "viaje de negocios"/"business trip"→business.
- If user is vague, leave fields null. Do NOT invent."""

INTENT_PARSER_EXAMPLES = [
    {
        "input": "Family hotel in Málaga for 3 nights in July, 2 adults and a child under 10, budget under €220 per night, pool preferred.",
        "output": {
            "destination": "Málaga",
            "country": "Spain",
            "check_in": None,
            "check_out": None,
            "nights": 3,
            "adults": 2,
            "children": 1,
            "max_price_per_night": 220,
            "currency": "EUR",
            "required_amenities": ["pool", "family_room"],
            "property_type": None,
            "trip_purpose": "family"
        }
    },
    {
        "input": "Business trip Madrid next week, single, need gym and wifi, under 250",
        "output": {
            "destination": "Madrid",
            "country": "Spain",
            "check_in": None,
            "check_out": None,
            "nights": None,
            "adults": 1,
            "children": 0,
            "max_price_per_night": 250,
            "currency": "EUR",
            "required_amenities": ["gym", "free_wifi", "business_center"],
            "property_type": None,
            "trip_purpose": "business"
        }
    },
    {
        "input": "Pareja 4 noches en Santorini con piscina infinity y vista al caldera, luna de miel",
        "output": {
            "destination": "Santorini",
            "country": "Greece",
            "check_in": None,
            "check_out": None,
            "nights": 4,
            "adults": 2,
            "children": 0,
            "max_price_per_night": None,
            "currency": "EUR",
            "required_amenities": ["infinity_pool", "caldera_view"],
            "property_type": None,
            "trip_purpose": "couples"
        }
    }
]


JUSTIFIER_SYSTEM = """You are a hotel-industry copywriter. Given a user's travel query and a shortlist of 3–4 hotels that match, write a short justification per hotel explaining WHY it fits the *specific query the user typed*.

Rules:
- 1–2 sentences per hotel. Max ~35 words.
- Address the user's query DIRECTLY — connect the hotel's amenities and character to the traveller's stated need (destination, dates, who is travelling, budget, what matters to them).
- Hotelier-friendly tone: specific, factual, natural prose. Never generic marketing copy.
- Reference the hotel's actual amenities / characteristics that match the query. Name them.
- Always write in English, regardless of the language the user's query was written in. This is product UI copy, not a chat message.
- Never mention Booking.com, OTAs, partnerships, or Paraty Tech itself in the text — the UI handles all meta-context; focus purely on the query ↔ hotel fit.
- Never hallucinate amenities that aren't in the hotel's listed amenities.

Output format: a JSON object mapping hotel id → justification string. Nothing else."""


def build_intent_parse_request(user_query: str) -> list:
    """Build the messages list for intent parsing. Uses few-shot examples."""
    import json as _json
    examples_text = "\n\n".join(
        f"User query: {ex['input']}\nOutput: {_json.dumps(ex['output'], ensure_ascii=False)}"
        for ex in INTENT_PARSER_EXAMPLES
    )
    return [
        {
            "role": "user",
            "content": (
                f"Examples:\n\n{examples_text}\n\n"
                f"Now parse this query and return ONLY the JSON object (no prose):\n\n"
                f"User query: {user_query}"
            ),
        }
    ]


CONCIERGE_OPENING_SYSTEM = """You are a warm, knowledgeable travel concierge for Paraty Tech's AI booking layer. You write the opening message in a chat sidebar after the traveller's query has been parsed and hotels have been shortlisted.

Voice: first person ("I found..."), conversational, specific. Not salesy. Not generic. Sound like a real human concierge who knows these hotels.

Rules:
- 1-2 sentences total.
- Reference: how many hotels matched, the destination, and ONE other relevant constraint (price cap OR nights OR amenity OR trip purpose). Pick the most salient.
- Wrap key facts in <strong>…</strong>. Wrap money amounts or emphasised words in <em>…</em>.
- Match the language of the user's query (Spanish → Spanish; English → English).
- Never invent hotel names, prices or dates not in the data provided.
- Never mention Booking.com, Paraty Tech, or the system internals.

Return ONLY the bubble HTML text. No JSON, no code fences, no surrounding prose."""


PROPERTY_PAGE_SYSTEM = """You are generating page content for a single hotel detail page on Paraty Tech's AI-native direct-booking platform. You receive a hotel object and a parsed user intent, and return a JSON object with several sections of content for that hotel.

Voice: warm, specific, grounded. Never marketing fluff. Reference the hotel's actual amenities, city, property type, and the user's intent (who is travelling, nights, budget, kids).

Match the language of the user's query (Spanish query → Spanish content; English query → English content).

Return ONLY valid JSON. No prose, no code fences. Schema:

{
  "owner_voice": [string, string],           // exactly 2 short paragraphs, first-person from the owners/team. Max 35 words each. Grounded in the hotel's description and property_type.
  "owner_signature": string,                 // who signs (e.g. "The García family — third-generation hoteliers" or "The team at Hotel X"). Invent a plausible family name or simply say "The Hotel X team" if unclear.
  "neighbourhood": [                          // exactly 6 plausible walkable landmarks for the hotel's city
    {"name": string, "walk_time": string, "icon": string}   // icon is one of: plaza, monument, museum, market, port, beach, park, restaurant
  ],
  "concierge_opening": string,                // 1-2 sentences introducing the hotel to the traveller, referencing their intent. Use <strong> for facts, <em> for emphasis.
  "proactive_questions": [string, string, string, string],  // exactly 4 suggested questions a traveller WITH THIS INTENT would ask. Be specific to the hotel (pool if amenities has pool; quiet rooms if urban; family-relevant if children > 0). Questions must be natural language, no hashtags.
  "contextual_alert": string,                 // 1-2 sentences with a practical tip or warning tailored to the intent (e.g. late-season heat, local custom, arrival logistics). Use <strong> and <em> for emphasis. Plausibly grounded — do not invent airline flight numbers.
  "addons": [                                 // exactly 3 add-ons with AI reasoning tied to intent + hotel
    {"title": string, "reason": string, "price": string}   // price format: "+ €42" or "+ €15" or "Free"
  ]
}

Hard constraints:
- Never name a specific real person unless it's the owner's name you're inventing for the owner_voice.
- Do not invent amenities the hotel doesn't have.
- Keep all copy under 2 sentences unless the schema says otherwise.
- Addons must feel contextually justified. If children > 0, at least one addon should be family-relevant. If arriving late or on weekend, transfer justification must reference that."""


def build_concierge_opening_request(user_query: str, intent: dict, hotels: list) -> list:
    """Build messages for the opening concierge bubble on the results page."""
    import json as _json
    slim_hotels = [
        {"id": h["id"], "name": h["name"], "city": h["city"],
         "direct_price_per_night": h["direct_price_per_night"],
         "amenities": h["amenities"][:5]}
        for h in hotels
    ]
    return [
        {
            "role": "user",
            "content": (
                f"User query: {user_query}\n\n"
                f"Parsed intent:\n{_json.dumps(intent, ensure_ascii=False, indent=2)}\n\n"
                f"Matched hotels ({len(slim_hotels)}):\n{_json.dumps(slim_hotels, ensure_ascii=False, indent=2)}\n\n"
                f"Write the opening bubble."
            ),
        }
    ]


def build_property_page_request(user_query: str, intent: dict, hotel: dict) -> list:
    """Build messages for the full property-page content generation."""
    import json as _json
    return [
        {
            "role": "user",
            "content": (
                f"User query: {user_query or '(no query — direct navigation)'}\n\n"
                f"Parsed intent:\n{_json.dumps(intent, ensure_ascii=False, indent=2)}\n\n"
                f"Hotel:\n{_json.dumps(hotel, ensure_ascii=False, indent=2)}\n\n"
                f"Return the JSON content object."
            ),
        }
    ]


def build_justifier_request(user_query: str, hotels: list) -> list:
    """Build the messages list for ranking justification. hotels = list of dicts."""
    import json as _json
    # Strip hotels to what the LLM needs to justify
    slim = [
        {
            "id": h["id"],
            "name": h["name"],
            "city": h["city"],
            "property_type": h["property_type"],
            "amenities": h["amenities"],
            "family_friendly": h.get("family_friendly", False),
            "business_friendly": h.get("business_friendly", False),
            "paraty_client_since": h.get("paraty_client_since"),
            "description": h["description"],
            "avg_rating": h.get("avg_rating"),
        }
        for h in hotels
    ]
    return [
        {
            "role": "user",
            "content": (
                f"User query: {user_query}\n\n"
                f"Shortlisted hotels:\n{_json.dumps(slim, ensure_ascii=False, indent=2)}\n\n"
                f"Return ONLY the JSON object mapping hotel id → one-sentence justification."
            ),
        }
    ]
