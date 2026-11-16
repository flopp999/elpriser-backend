import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# --- HISTORISK HÄMTNING (Valfritt) ---
# Lämna som None vid vanlig daglig körning via GitHub Actions.
# Fyll i datum (ÅÅÅÅ-MM-DD) om du vill backfilla historik manuellt.
CUSTOM_START_DATE = "2026-11-01" #None  # Ex: "2026-01-01"
CUSTOM_END_DATE = "2026-11-16" #None  # Ex: "2026-01-31"

# Konfiguration: Katalog/Område -> (Landskod, Lokal valuta, ENTSO-E EIC-kod)
ZONES = {
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
    # Tyskland & Luxemburg (EUR)
    "DE":  ("DE", "EUR", "10Y1001A1001A82H"),
    "LU":  ("LU", "EUR", "10Y1001A1001A82H"),
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
    return (amount / eur_rates[from_curr]) * eur_rates[to_curr]


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
    # Hämta från 00:00 UTC idag till 23:00 UTC imorgon
    period_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = (now_utc + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)

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

def fetch_custom_period(
    token: str,
    eic_code: str,
    target_currency: str,
    eur_rates: dict[str, float],
    period_start: datetime,
    period_end: datetime,
) -> list[dict]:
  """Hämtar priser från ENTSO-E för ett valfritt tidsintervall."""
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

  with urllib.request.urlopen(req, timeout=60) as response:
    xml_content = response.read().decode("utf-8")

  return parse_entsoe_xml(xml_content, target_currency, eur_rates)
    
def main():
  token = os.environ.get("ENTSOE_TOKEN")
  if not token:
    raise RuntimeError("Miljövariabeln ENTSOE_TOKEN saknas!")

  eur_rates = get_eur_exchange_rates()
  now_utc = datetime.now(timezone.utc)
  updated_at = now_utc.isoformat().replace("+00:00", "Z")

  # Bestäm vilka datum som ska hämtas
  if CUSTOM_START_DATE and CUSTOM_END_DATE:
    start_date_obj = datetime.strptime(CUSTOM_START_DATE, "%Y-%m-%d").date()
    end_date_obj = datetime.strptime(CUSTOM_END_DATE, "%Y-%m-%d").date()

    # Bygg en lista med alla datum mellan start och slut (inklusive)
    valid_dates = []
    curr = start_date_obj
    while curr <= end_date_obj:
      valid_dates.append(curr)
      curr += timedelta(days=1)

    # Sätt API-anropets start och slut baserat på dina variabler
    api_period_start = datetime.combine(
        start_date_obj, datetime.min.time(), tzinfo=timezone.utc
    )
    api_period_end = datetime.combine(
        end_date_obj + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    print(
        f"Kör historiskt läge från {CUSTOM_START_DATE} till"
        f" {CUSTOM_END_DATE}..."
    )
  else:
    today_utc = now_utc.date()
    valid_dates = [today_utc, today_utc + timedelta(days=1)]
    api_period_start = now_utc.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    api_period_end = (now_utc + timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

  success_count = 0

  for zone_dir, (country_code, currency, eic_code) in ZONES.items():
    print(f"Hämtar {zone_dir} ({country_code}, {currency})...")

    try:
      # Skicka med de dynamiska start- och slutdatumen till ENTSO-E-anropet
      prices = fetch_custom_period(
          token, eic_code, currency, eur_rates, api_period_start, api_period_end
      )
      if not prices:
        print(f"Varning: Inga priser hittades för {zone_dir}.")
        continue

      prices_by_day: dict[datetime.date, list[dict]] = {}
      for item in prices:
        slot_utc = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
        if slot_utc.date() in valid_dates:
          prices_by_day.setdefault(slot_utc.date(), []).append(item)

      if not prices_by_day:
        print(f"Varning: Inga giltiga dagpriser inom intervallet för {zone_dir}.")
        continue

      for day_date, day_prices in sorted(prices_by_day.items()):
        year_str = day_date.strftime("%Y")
        month_str = day_date.strftime("%m")
        date_str = day_date.strftime("%Y-%m-%d")

        dir_path = os.path.join(zone_dir, year_str, month_str)
        os.makedirs(dir_path, exist_ok=True)
        output_file = os.path.join(dir_path, f"{date_str}.json")

        payload = {
            "zone": zone_dir,
            "country": country_code,
            "date": date_str,
            "timezone": "UTC",
            "currency": currency,
            "unit": "kWh",
            "exchange_rate_eur": eur_rates.get(currency, 1.0),
            "source": "ENTSO-E Transparency Platform",
            "updated_at": updated_at,
            "count": len(day_prices),
            "prices": day_prices,
        }

        with open(output_file, "w", encoding="utf-8") as f:
          json.dump(payload, f, indent=2, ensure_ascii=False)

        print(f"-> Sparade {len(day_prices)} intervall till {output_file}")

      success_count += 1

    except urllib.error.HTTPError as e:
      print(f"Varning: HTTP-fel {e.code} vid hämtning av {zone_dir}. Hoppar över.")
    except Exception as e:
      print(f"Varning: Kunde inte bearbeta {zone_dir}: {e}")

    time.sleep(0.4)

  if success_count == 0:
    raise RuntimeError("Kunde inte hämta data för något enda elområde!")

  print(f"Klart! Uppdaterade {success_count} av {len(ZONES)} områden.")


if __name__ == "__main__":
    main()
