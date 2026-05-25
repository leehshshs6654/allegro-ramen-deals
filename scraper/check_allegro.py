import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import requests
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"
OFFERS_PATH = BASE_DIR / "public" / "offers.json"
SEEN_PATH = BASE_DIR / "seen_hotdeals.json"


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def should_run_now(config):
    timezone = config.get("timezone", "Europe/Budapest")
    run_hours = set(config.get("run_hours", [3, 9, 15, 21]))

    now = datetime.now(ZoneInfo(timezone))

    if len(sys.argv) > 1 and sys.argv[1] == "--force":
        return True, now

    return now.hour in run_hours, now


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def extract_price_huf(text):
    cleaned = str(text).replace("\xa0", " ")

    patterns = [
        r"(\d[\d\s]*)\s*Ft",
        r"(\d[\d\s]*)\s*HUF"
    ]

    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            number = match.group(1).replace(" ", "")
            try:
                return int(number)
            except ValueError:
                return None

    return None


def extract_pack_count(text):
    text = normalize_text(text)

    patterns = [
        r"(\d+)\s*x\s*\d+",
        r"(\d+)\s*×\s*\d+",
        r"(\d+)\s*db",
        r"(\d+)\s*darab",
        r"(\d+)\s*pcs",
        r"(\d+)\s*pieces",
        r"(\d+)\s*pack",
        r"(\d+)\s*csomag"
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            count = int(match.group(1))
            if 1 <= count <= 100:
                return count

    return 1


def matches_product(text, product):
    text = normalize_text(text)

    must_include = product.get("must_include", [])
    exclude = product.get("exclude", [])

    for word in must_include:
        if normalize_text(word) not in text:
            return False

    for word in exclude:
        if normalize_text(word) in text:
            return False

    return True


def clean_title(raw_text):
    lines = [line.strip() for line in str(raw_text).splitlines() if line.strip()]

    if not lines:
        return str(raw_text).strip()[:220]

    bad_words = [
        "hozzáadás",
        "összehasonlításhoz",
        "szállítás",
        "tanúsítvány",
        "szuper eladó",
        "információk",
        "már ennyiért",
        "kosárba",
        "értékelés"
    ]

    candidates = []

    for line in lines:
        lower = line.lower()

        if any(bad in lower for bad in bad_words):
            continue

        if "ft" in lower or "huf" in lower:
            continue

        if len(line) < 5:
            continue

        candidates.append(line)

    if candidates:
        return candidates[0][:220]

    return lines[0][:220]


def scrape_query(page, query, product):
    encoded = quote_plus(query)
    url = f"https://allegro.hu/kereses?string={encoded}"

    page.goto(url, wait_until="commit", timeout=20000)
    time.sleep(2)

    offers = []
    articles = page.locator("article").all()

    print(f"Found article cards: {len(articles)}")

    for article in articles[:25]:
        try:
            raw_text = article.inner_text(timeout=3000)
            title = clean_title(raw_text)

            if not matches_product(raw_text, product):
                continue

            price = extract_price_huf(raw_text)
            if price is None:
                continue

            link = None
            anchors = article.locator("a").all()

            for anchor in anchors:
                href = anchor.get_attribute("href")
                if href and "/termek/" in href:
                    link = href
                    break

            if not link:
                continue

            if link.startswith("/"):
                link = "https://allegro.hu" + link

            pack_count = extract_pack_count(raw_text)
            unit_price = round(price / pack_count)

            offers.append({
                "product": product["name"],
                "title": title,
                "price": price,
                "pack_count": pack_count,
                "unit_price": unit_price,
                "url": link.split("?")[0]
            })

        except Exception as error:
            print(f"Card parse failed: {error}")
            continue

    print(f"Matched offers: {len(offers)}")
    return offers


def deduplicate_offers(offers):
    seen = set()
    unique = []

    for offer in sorted(offers, key=lambda x: x["unit_price"]):
        key = offer["url"]

        if key in seen:
            continue

        seen.add(key)
        unique.append(offer)

    return unique


def send_telegram_hotdeal(offer, chat_id, bot_token):
    if not bot_token or not chat_id:
        return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    text = (
        "🔥 라면 핫딜 발견\n\n"
        f"{offer['product']}\n"
        f"{offer['title']}\n\n"
        f"총 가격: {offer['price']} Ft\n"
        f"수량 추정: {offer['pack_count']}개\n"
        f"개당가: {offer['unit_price']} Ft\n\n"
        f"{offer['url']}"
    )

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False
    }

    response = requests.post(api_url, json=payload, timeout=20)
    response.raise_for_status()


def main():
    config = load_json(CONFIG_PATH, {})
    should_run, now = should_run_now(config)

    if not should_run:
        print(f"Skip. Budapest time now: {now.isoformat()}")
        return

    display_max = config.get("display_max_price", 500)
    alert_max = config.get("alert_max_price", 300)

    all_offers = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 "
                "AppleWebKit/537.36 "
                "Chrome/120.0 Safari/537.36 "
                "RamenDealsBot/1.0"
            ),
            viewport={"width": 1366, "height": 768}
        )

        for product in config.get("products", []):
            for query in product.get("queries", []):
                print(f"Searching: {product['name']} / {query}")

                try:
                    offers = scrape_query(page, query, product)
                    all_offers.extend(offers)
                except Exception as error:
                    print(f"Search failed: {query} / {error}")

        browser.close()

    all_offers = deduplicate_offers(all_offers)

    visible_offers = [
        offer for offer in all_offers
        if offer["unit_price"] < display_max
    ]

    checked_at = now.strftime("%Y-%m-%d %H:%M Europe/Budapest")

    for offer in visible_offers:
        offer["checked_at"] = checked_at
        offer["status"] = "hot" if offer["unit_price"] < alert_max else "show"

    save_json(OFFERS_PATH, visible_offers)

    seen_hotdeals = set(load_json(SEEN_PATH, []))
    new_seen_hotdeals = set(seen_hotdeals)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    for offer in visible_offers:
        if offer["unit_price"] >= alert_max:
            continue

        hotdeal_key = f"{offer['url']}|{offer['unit_price']}"

        if hotdeal_key in seen_hotdeals:
            continue

        try:
            send_telegram_hotdeal(offer, chat_id, bot_token)
            new_seen_hotdeals.add(hotdeal_key)
            print(f"Hotdeal sent: {offer['title']}")
        except Exception as error:
            print(f"Telegram send failed: {error}")

    save_json(SEEN_PATH, sorted(new_seen_hotdeals))

    print(f"Total scraped offers: {len(all_offers)}")
    print(f"Done. Visible offers: {len(visible_offers)}")


if __name__ == "__main__":
    main()
