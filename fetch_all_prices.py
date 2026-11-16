import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# Konfiguration: Filnamn (utan .json) -> (Landskod, Lokal valuta, ENTSO-E EIC-kod)
ZONES = {
    # Tyskland (EUR)
    "DE":  ("DE", "EUR", "10Y1001A1001A82H"),
    # Luxemburg (EUR) - egen förfrågan mot DE-LU
    "LU":  ("LU", "EUR", "10Y1001A1001A82H"),
    # Sverige (SEK)
    "SE1": ("SE", "SEK", "10Y1001A1001A44P"),
    "SE2": ("SE", "SEK", "10Y1001A1001A45N"),
    "SE3": ("SE", "SEK", "10Y1001A1001A46L"),
    "SE4": ("SE", "SEK", "10Y1001A1001A47J"),
    # Danmark (DKK)
    "DK1": ("DK", "DKK", "10YDK-1--------W"),
    "DK2": ("DK", "DKK", "10YDK-2--------M"),
    # Norge (NOK)
    "NO1": ("NO", "NOK", "10YNO-1--------2"),
    "NO2": ("NO", "NOK", "10YNO-2--------T"),
    "NO3": ("NO", "NOK", "10YNO-3--------J"),
    "NO4": ("NO", "NOK", "10YNO-4--------9"),
    "NO5": ("NO", "NOK", "10Y1001A1001A48H"),
    # Finland & Baltikum (EUR)
    "FI":  ("FI", "EUR", "10YFI-1--------U"),
    "EE":  ("EE", "EUR", "10Y1001A1001A39I"),
    "LV":  ("LV", "EUR", "10YLV-1001A00074"),
    "LT":  ("LT", "EUR", "10YLT-1001A0008Q"),
    # Nederländerna (EUR)
    "NL":  ("NL", "EUR", "10YNL----------L"),
    # Storbritannien (GBP)
    "GB":  ("GB", "GBP", "10YGB----------A"),
    # Polen (PLN)
    "PL":  ("PL", "PLN", "10YPL-AREA-----S"),
    # Ukraina (UAH)
    "UA":  ("UA", "UAH", "10Y1001C00003F"),
    # Spanien & Portugal (EUR)
    "ES":  ("ES", "EUR", "10YES-REE------0"),
    "PT":  ("PT", "EUR", "10YPT-REN------W"),
}

# Reservkurser (1 EUR = X lokal valuta) om valutakurs-API:et tillfälligt inte svarar
FALLBACK_RATES_FROM_EUR = {
    "EUR": 1.0,
    "SEK": 11.40,
    "DKK": 7.46,
    "NOK": 11.75,
    "GBP": 0.84,
    "PLN": 4.30,
    "UAH": 44.50,
}


def get_eur_exchange_rates() -> dict[str, float]:
    """Hämtar aktuella växelkurser med EUR som basvaluta."""
    url = "https://open.er-api.com/v6/latest/EUR"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "EnergyPriceFetcher/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("result") == "success" and "rates" in data:
                rates = FALLBACK_RATES_FROM_EUR.copy()
                for curr in rates.keys():
                    if curr in data["rates"]:
                        rates[curr] = float(data["rates"][curr])
                print(f"Hämtade aktuella växelkurser (EUR-bas): {rates}")
                return rates
    except Exception as e:
        print(f"Varning: Kunde inte hämta live-växelkurser ({e}), använder reservkurser.")
    return FALLBACK_RATES_FROM_EUR.copy()


def convert_currency(amount: float, from_curr: str, to_curr: str, eur_rates: dict[str, float]) -> float:
    """Konverterar belopp mellan två valutor via EUR."""
    if from_curr == to_curr:
        return amount
    if from_curr not in eur_rates or to_curr not in eur_rates:
        raise ValueError(f"Saknar växelkurs för konvertering {from_curr} -> {to_curr}")
    amount_in_eur = amount / eur_rates[from_curr]
    return amount_in_eur * eur_rates[to_curr]


def parse_entsoe_xml(xml_data: str, target_currency: str, eur_rates: dict[str, float]) -> list[dict]:
    """Parsar ENTSO-E XML, hanterar valutaomvandling och fyller i saknade intervall."""
    root = ET.fromstring(xml_data)
    ns_uri = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    ns = {"ns": ns_uri} if ns_uri else {}

    def find_all(elem, path):
        return elem.findall(path, ns) if ns else elem.findall(path.replace("ns:", ""))

    def find_one(elem, path):
        return elem.find(path, ns) if ns else elem.find(path.replace("ns:", ""))

    prices = []

    for ts in find_all(root, ".//ns:TimeSeries"):
        curr_elem = find_one(ts, "ns:currency_Unit.name")
        source_currency = curr_elem.text.strip() if curr_elem is not None else "EUR"

        for period in find_all(ts, "ns:Period"):
            start_str = find_one(period, "ns:timeInterval/ns:start").text
            end_str = find_one(period, "ns:timeInterval/ns:end").text
            resolution = find_one(period, "ns:resolution").text

            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

            if resolution == "PT15M":
                step = timedelta(minutes=15)
            elif resolution == "PT60M":
                step = timedelta(hours=1)
            else:
                raise ValueError(f"Okänd upplösning: {resolution}")

            total_steps = int((end_dt - start_dt) / step)

            points = {}
            for point in find_all(period, "ns:Point"):
                pos = int(find_one(point, "ns:position").text)
                amount = float(find_one(point, "ns:price.amount").text)
                points[pos] = amount

            last_price_mwh = None
            for pos in range(1, total_steps + 1):
                if pos in points:
                    last_price_mwh = points[pos]
                if last_price_mwh is None:
                    continue

                slot_start = start_dt + step * (pos - 1)
                slot_end = slot_start + step

                local_mwh = convert_currency(last_price_mwh, source_currency, target_currency, eur_rates)
                local_kwh = local_mwh / 1000.0

                prices.append({
                    "start": slot_start.isoformat().replace("+00:00", "Z"),
                    "end": slot_end.isoformat().replace("+00:00", "Z"),
                    "price_kwh": round(local_kwh, 5),
                    "price_mwh": round(local_mwh, 2),
                })

    unique_prices = {item["start"]: item for item in prices}
    return [unique_prices[k] for k in sorted(unique_prices.keys())]


def fetch_zone_prices(token: str, eic_code: str, target_currency: str, eur_rates: dict[str, float]) -> list[dict]:
    """Hämtar day-ahead-priser för ett elområde från ENTSO-E."""
    now_utc = datetime.now(timezone.utc)
    period_start = (now_utc - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    period_end = (now_utc + timedelta(days=2)).replace(hour=23, minute=0, second=0, microsecond=0)

    params = {
        "documentType": "A44",
        "in_Domain": eic_code,
        "out_Domain": eic_code,
        "periodStart": period_start.strftime("%Y%m%d%H%M"),
        "periodEnd": period_end.strftime("%Y%m%d%H%M"),
        "securityToken": token,
    }

    url = f"https://web-api.tp.entsoe.eu/api?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "EnergyPriceFetcher/1.0"})

    with urllib.request.urlopen(req, timeout=30) as response:
        xml_content = response.read().decode("utf-8")

    return parse_entsoe_xml(xml_content, target_currency, eur_rates)


def main():
    token = os.environ.get("ENTSOE_TOKEN")
    if not token:
        raise RuntimeError("Miljövariabeln ENTSOE_TOKEN saknas!")

    eur_rates = get_eur_exchange_rates()
    updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    success_count = 0

    for file_prefix, (country_code, currency, eic_code) in ZONES.items():
        output_file = f"{file_prefix}.json"
        print(f"Hämtar {file_prefix} ({country_code}, {currency})...")

        try:
            prices = fetch_zone_prices(token, eic_code, currency, eur_rates)
            if not prices:
                print(f"Varning: Inga priser hittades för {file_prefix}.")
                continue

            payload = {
                "zone": file_prefix,
                "country": country_code,
                "currency": currency,
                "unit": "kWh",
                "exchange_rate_eur": eur_rates.get(currency, 1.0),
                "source": "ENTSO-E Transparency Platform",
                "updated_at": updated_at,
                "count": len(prices),
                "prices": prices,
            }

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            print(f"-> Sparade {len(prices)} intervall till {output_file}")
            success_count += 1

        except urllib.error.HTTPError as e:
            print(f"Varning: HTTP-fel {e.code} vid hämtning av {file_prefix}. Hoppar över.")
        except Exception as e:
            print(f"Varning: Kunde inte bearbeta {file_prefix}: {e}")

        time.sleep(0.4)  # Paus för att respektera ENTSO-E API rate limit

    if success_count == 0:
        raise RuntimeError("Kunde inte hämta data för något enda elområde!")

    print(f"Klart! Uppdaterade {success_count} av {len(ZONES)} filer.")


if __name__ == "__main__":
    main()
