import os
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# Inställningar för Tyskland/Luxemburg (DE-LU)
EIC_DE_LU = "10Y1001A1001A82H"
OUTPUT_FILE = "prices.json"


def parse_entsoe_xml(xml_data: str) -> list[dict]:
    """Parsar ENTSO-E XML-svar och fyller i eventuella saknade intervall."""
    root = ET.fromstring(xml_data)
    # Hämta XML-namespace dynamiskt
    ns_uri = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    ns = {"ns": ns_uri} if ns_uri else {}

    def find_all(elem, path):
        return elem.findall(path, ns) if ns else elem.findall(path.replace("ns:", ""))

    def find_one(elem, path):
        return elem.find(path, ns) if ns else elem.find(path.replace("ns:", ""))

    prices = []

    for period in find_all(root, ".//ns:Period"):
        start_str = find_one(period, "ns:timeInterval/ns:start").text
        end_str = find_one(period, "ns:timeInterval/ns:end").text
        resolution = find_one(period, "ns:resolution").text

        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        # Stöd för 15-minuterspriser (PT15M) och timpriser (PT60M)
        if resolution == "PT15M":
            step = timedelta(minutes=15)
        elif resolution == "PT60M":
            step = timedelta(hours=1)
        else:
            raise ValueError(f"Okänd upplösning: {resolution}")

        total_steps = int((end_dt - start_dt) / step)

        # Läs in alla rapporterade punkter (position -> EUR/MWh)
        points = {}
        for point in find_all(period, "ns:Point"):
            pos = int(find_one(point, "ns:position").text)
            amount = float(find_one(point, "ns:price.amount").text)
            points[pos] = amount

        # Fyll i samtliga intervall (inkl. eventuella utelämnade positioner)
        last_price_mwh = None
        for pos in range(1, total_steps + 1):
            if pos in points:
                last_price_mwh = points[pos]
            if last_price_mwh is None:
                continue

            slot_start = start_dt + step * (pos - 1)
            slot_end = slot_start + step
            price_kwh = round(last_price_mwh / 1000.0, 5)

            prices.append({
                "start": slot_start.isoformat().replace("+00:00", "Z"),
                "end": slot_end.isoformat().replace("+00:00", "Z"),
                "price_eur_kwh": price_kwh,
                "price_eur_mwh": round(last_price_mwh, 2),
            })

    # Sortera kronologiskt och ta bort eventuella dubbletter
    unique_prices = {item["start"]: item for item in prices}
    return [unique_prices[k] for k in sorted(unique_prices.keys())]


def fetch_day_ahead(token: str) -> list[dict]:
    """Hämtar priser från igår kväll UTC (dagens start i CET) till övermorgon."""
    now_utc = datetime.now(timezone.utc)
    # Starta kl. 22:00 UTC dagen före (täcker midnatt CET/CEST)
    period_start = (now_utc - timedelta(days=1)).replace(
        hour=22, minute=0, second=0, microsecond=0
    )
    period_end = (now_utc + timedelta(days=2)).replace(
        hour=23, minute=0, second=0, microsecond=0
    )

    params = {
        "documentType": "A44",
        "in_Domain": EIC_DE_LU,
        "out_Domain": EIC_DE_LU,
        "periodStart": period_start.strftime("%Y%m%d%H%M"),
        "periodEnd": period_end.strftime("%Y%m%d%H%M"),
        "securityToken": token,
    }

    url = f"https://web-api.tp.entsoe.eu/api?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "EnergyPriceFetcher/1.0"})

    with urllib.request.urlopen(req, timeout=30) as response:
        xml_content = response.read().decode("utf-8")

    return parse_entsoe_xml(xml_content)


def main():
    token = os.environ.get("ENTSOE_TOKEN")
    if not token:
        raise RuntimeError("Miljövariabeln ENTSOE_TOKEN saknas!")

    prices = fetch_day_ahead(token)
    if not prices:
        raise RuntimeError("Inga prisdata returnerades från ENTSO-E.")

    payload = {
        "zone": "DE-LU",
        "currency": "EUR",
        "unit": "kWh",
        "source": "ENTSO-E Transparency Platform",
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(prices),
        "prices": prices,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"Sparade {len(prices)} prisintervall till {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
