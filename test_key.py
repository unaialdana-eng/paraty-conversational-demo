"""Quick diagnostic for the Anthropic API key. Run: python3 test_key.py"""
import os
from dotenv import load_dotenv

load_dotenv(".env")
key = os.getenv("ANTHROPIC_API_KEY")

print(f"Key loaded: {bool(key)}")
print(f"Length: {len(key) if key else 0}")
print(f"Prefix: {repr(key[:20]) if key else None}")
print(f"Suffix: {repr(key[-10:]) if key else None}")

if not key:
    print("❌ No key found in .env — check file exists and has ANTHROPIC_API_KEY=sk-ant-...")
    raise SystemExit(1)

from anthropic import Anthropic

client = Anthropic(api_key=key)
try:
    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=20,
        messages=[{"role": "user", "content": "say hello in 5 words"}],
    )
    print("✅ Sonnet 4.6 works:", r.content[0].text)
except Exception as e:
    print("❌ Sonnet 4.6 error:", e)

try:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "say hello"}],
    )
    print("✅ Haiku 4.5 works:", r.content[0].text)
except Exception as e:
    print("❌ Haiku 4.5 error:", e)
