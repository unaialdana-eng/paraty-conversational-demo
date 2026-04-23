# Paraty Conversational Booking Agent — MVP

A 90-second working demo of the AI-agent layer proposed in **Move 1** of the Paraty Tech CPO 12-month product agenda.

Built by Unai Aldana, April 2026.

---

## What it does

User types a natural-language travel query ("family hotel in Málaga for 3 nights in July, 2 adults + 1 child, under €220 with pool"). The system:

1. **Claude parses intent** (pass 1) into structured JSON — destination, dates, adults, children, price cap, required amenities, trip purpose.
2. **Python filters + ranks** the 20-hotel Paraty inventory (mock JSON) by city match, price gate, amenity overlap, trip alignment, and partnership longevity.
3. **Claude writes hotelier-friendly justifications** (pass 2) — one sentence per shortlisted hotel explaining why it fits the query.
4. **UI renders** 3–4 hotel cards with direct price, Booking.com comparison, savings banner, and direct-book CTA — in the editorial theme matching the CPO deck.

---

## Run locally (5 min setup)

Two runtimes ship in this repo:

- **FastAPI + Jinja** (recommended) — 4 pixel-fidelity pages matching the Claude Design HTML exports, plus an explainer landing. This is the version to demo.
- **Streamlit** (legacy) — the original one-command demo. Kept around as a fallback but visually less faithful.

```bash
cd prototype

# 1) Install deps in a venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) Add your Anthropic API key (optional — mock mode works without it)
cp .env.example .env
# Edit .env with your key from https://console.anthropic.com/
export ANTHROPIC_API_KEY="sk-ant-..."

# 3a) Launch FastAPI (recommended for the pitch)
uvicorn server:app --reload --port 8000
# open http://localhost:8000

# 3b) OR launch the legacy Streamlit single-page demo
streamlit run streamlit_app.py
# open http://localhost:8501
```

### FastAPI routes

| Route                     | Purpose |
|---------------------------|---------|
| `GET /`                   | Explainer landing — purpose, restrictions, tech, what's mock |
| `GET /app`                | Homepage — unified search shell (classic + AI concierge) |
| `POST /search`            | Parses the query and redirects to `/results?q=…` |
| `GET /results?q=…`        | AI concierge sidebar + editable search + filter chips + hotel cards |
| `GET /property/{id}?q=…`  | Hero + gallery + owner voice + neighbourhood + AI add-ons |
| `GET /checkout/{id}?q=…`  | Express checkout — guest info, payment, addons, savings |

---

## Demo flow (the 90-second pitch)

1. Screen-share `localhost:8501`.
2. "Let me show you what Move 1 of the year-one plan looks like running, not on slides."
3. Click **Example 1** (or type a query).
4. Click **Find direct**.
5. As the results render: *"Claude parses the natural-language query into structured intent, Python filters Paraty's inventory, Claude re-enters to write hotelier-friendly justifications. Three cards, direct price vs Booking.com comparison, save banner. This is exactly the pattern a production AI-agent layer would use — the difference is the mock JSON becomes the real Paraty BE API."*
6. Close with: *"Ten hours of a Saturday to demonstrate Move 1 running. The full production version is a 4-engineer, 90-120 day squad."*

---

## File layout

```
prototype/
├── README.md                 # this file
├── requirements.txt          # fastapi, uvicorn, jinja2, streamlit, anthropic, dotenv
├── server.py                 # FastAPI app — routes + LLM orchestration + caching
├── streamlit_app.py          # legacy single-page Streamlit demo
├── prompts.py                # Claude prompts (intent, justifier, concierge opening, property page)
├── booking_engine.py         # mock Paraty BE — filter + rank + savings + fallback generators
├── hotels.json               # 60 hand-curated mock hotels (across Paraty's real geo footprint)
├── templates/
│   ├── explainer.html        # Landing / about-the-demo
│   ├── homepage.html         # Unified search shell (classic + AI concierge strip)
│   ├── results.html          # Concierge sidebar + search + filter + cards
│   ├── property.html         # Hero + concierge Q&A + gallery + owner voice + addons
│   └── checkout.html         # Express checkout with AI-reasoned addons
├── Dockerfile                # Container image for Fly.io deploy
├── .dockerignore             # Keeps the image small (excludes designs/, .venv, etc.)
├── fly.toml                  # Fly.io app config (Madrid region, always-on)
└── designs/                  # Untouched Claude Design HTML exports (reference)
    ├── Paraty Homepage.html
    ├── Paraty Results.html
    ├── Paraty Property.html
    ├── Paraty Checkout.html
    ├── DESIGN_BUNDLE_README.md
    └── chats/                # Chat transcripts from the design iteration
```

---

## Deploy to Fly.io (public URL for the pitch)

```bash
# 1) One-time install + login (skip if already done)
brew install flyctl
fly auth login

# 2) From prototype/ — first-time launch
#    Fly will read fly.toml + Dockerfile. Answer:
#      - "Would you like to copy its configuration?" → Yes
#      - App name → paraty-demo  (or whatever is free — remember it)
#      - Region → mad (Madrid) is pre-set, accept
#      - Postgres / Redis → No
#      - Deploy now? → No  (we still need to set the secret)
fly launch --no-deploy

# 3) Set the Anthropic API key as a secret
fly secrets set ANTHROPIC_API_KEY="sk-ant-..."

# 4) Deploy — builds the Docker image, pushes, and spins up the VM
fly deploy

# 5) Open the public URL
fly open
```

The URL will look like `https://paraty-demo.fly.dev`. Share it in the founder email.

### Day-to-day

| Need | Command |
|------|---------|
| See live logs | `fly logs` |
| Redeploy after code change | `fly deploy` |
| Update the Claude key | `fly secrets set ANTHROPIC_API_KEY="sk-ant-..."` |
| Check machine is running | `fly status` |
| Scale memory if it OOMs | `fly scale memory 1024` |

### Cost

Fly's free allowance covers 3 shared-cpu-1x VMs at 256MB. This demo runs on one 512MB VM (Python + Anthropic SDK is a little chubby for 256MB), which costs roughly **€1.70–€3/month** depending on traffic. Acceptable for a demo you can take down after the pitch (`fly apps destroy paraty-demo`).

---

## Deploy to Streamlit Cloud (free, 3 min)

Sharing a public URL in the founder email is the asymmetric move.

1. Push `prototype/` to a GitHub repo (private is fine).
2. Go to https://share.streamlit.io → New app → connect the repo, point at `prototype/app.py`.
3. In the app's Settings → Secrets, paste:
   ```
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```
4. Deploy. You'll get a URL like `paraty-demo-unai.streamlit.app`.
5. Include in the founder email: *"Working demo of Move 1: [URL]. Screen-share ready for our conversation."*

---

## Mock mode (no API key)

If no Anthropic API key is set, the app falls back to a **regex-based heuristic parser** and **template-based justifications**. It still runs end-to-end and still looks clean — you just lose the natural-language understanding nuance (e.g. Spanish queries, ambiguous phrasing, subtle amenity inference).

For an interview demo, you want the API key mode. For initial local testing or if you're concerned about demo-day API outages, mock mode is a safety net.

---

## Design choices — worth noting

- **Two-pass LLM architecture** (intent parse → Python filter → LLM justify). Same pattern a production agent layer would use. Keeps structured filtering in Python where it's robust, LLM where language matters.
- **Streamlit over Next.js**: 2-hour build vs 6-hour build. For interview demo, velocity beats polish. Founders judge the product thinking, not the framework.
- **Editorial theme matching the deck**: ivory + charcoal + terracotta. Makes the demo and deck feel like a single artefact.
- **Disclaimer always visible** at page bottom: independent prototype, mock data, not affiliated. No Paraty branding used.
- **20 hotels covering real Paraty geo footprint**: Spain, Portugal, Italy, Morocco, Greece, Mexico, Colombia, Chile, Argentina, DR, Japan. Uses Kent + Colony (the public Paraty case-study hotels) as anchors. Rest are plausible boutique / mid-chain examples.
- **Savings computation**: direct price × nights vs Booking.com price × nights. Booking.com prices are illustrative deltas (+12–20%) — not live data. Realistic per industry benchmarks; explicitly flagged as illustrative in the footer.

---

## Potential extensions (for a second conversation)

If the founder conversation goes well and they want more:

- **Real Paraty API integration** — swap `hotels.json` for Paraty BE endpoint. 1 day.
- **Multi-agent handoff** — Claude orchestrates not just filter but also helps compare, ask clarifying questions ("did you want adults-only or with kids?"). 2 days.
- **WhatsApp channel** — same backend, WhatsApp Business API frontend. 3-4 days.
- **Embedded in Paraty customer website** — iframe/widget. 1 week.
- **MCP server exposure** — so Perplexity / Claude / ChatGPT can query Paraty inventory directly via Model Context Protocol. 1-2 weeks.

The production version of Move 1 in the deck = all of the above, staged over 90–120 days by a 4-engineer + 1 PM squad.

---

## Legal / IP notes

- No Paraty proprietary information used — all inventory is hand-curated mock data.
- Kent Hotel + Colony Hotel are named because they appear in a public Paraty case study (paratytech.com/en/news). Other hotels are fictional.
- Non-compete (HomeToGo): 2024-present engagement — this prototype does not use any HomeToGo IP or product architecture.
- Non-compete (HBX 2023): more than 16 months have elapsed, and Paraty Tech is not an HBX-covered entity.

If at any point Paraty asks to take this down after the conversation, I'd do so same-day.

---

**Unai Aldana**
Palma de Mallorca · +34 606 213 544 · unaialdana@gmail.com · linkedin.com/in/unaialdana
