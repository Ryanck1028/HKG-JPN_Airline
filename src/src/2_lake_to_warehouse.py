from datetime import datetime
import glob
import json
import os
import re
import sqlite3
import pandas as pd

LCC_AIRLINES = {"HK Express", "Peach", "Jetstar Japan", "Greater Bay Airlines"}
USD_TO_HKD = 7.80


def clean_and_convert_price(price_str):
  if not price_str:
    return None, None
  digits = re.sub(r"[^\d.]", "", price_str)
  if not digits:
    return None, None
  val = float(digits)

  if "HK$" in price_str or "HKD" in price_str:
    return "HKD", round(val, 2)
  elif "$" in price_str:
    return "USD", round(val * USD_TO_HKD, 2)
  return "OTHER", None


def parse_hour(time_str):
  match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", str(time_str), re.I)
  if not match:
    return None
  hour = int(match.group(1))
  ampm = match.group(3)
  if ampm:
    if ampm.upper() == "PM" and hour != 12:
      hour += 12
    elif ampm.upper() == "AM" and hour == 12:
      hour = 0
  return hour


print("=== [Step 2] 開始從 Data Lake 重建 Data Warehouse ===")

all_rows = []
lake_files = sorted(glob.glob("data/lake/*.json"))

if not lake_files:
  print("[Data Warehouse] 找不到任何 Data Lake 檔案，略過更新。")
  exit(0)

for file_path in lake_files:
  with open(file_path, "r", encoding="utf-8") as f:
    snapshots = json.load(f)

  for snap in snapshots:
    meta = snap["meta"]
    dep_dt = datetime.strptime(meta["departure_date"], "%Y-%m-%d")

    for fl in snap["raw_flights"]:
      orig_curr, price_hkd = clean_and_convert_price(fl.get("price"))
      if price_hkd is None:
        continue

      airline = fl.get("name", "Unknown").strip()
      dep_hour = parse_hour(fl.get("departure"))

      all_rows.append({
          "scrape_date": meta["scrape_date"],
          "departure_date": meta["departure_date"],
          "days_left": meta["days_left"],
          "dep_weekday": dep_dt.weekday(),
          "is_weekend": 1 if dep_dt.weekday() >= 4 else 0,
          "origin": meta["origin"],
          "destination": meta["destination"],
          "airline": airline,
          "carrier_type": (
              "LCC" if any(lcc in airline for lcc in LCC_AIRLINES) else "FSC"
          ),
          "departure_time": fl.get("departure"),
          "is_redeye": (
              1 if (dep_hour is not None and 0 <= dep_hour <= 5) else 0
          ),
          "stops": fl.get("stops", 0),
          "original_currency": orig_curr,
          "price_hkd": price_hkd,
      })

df_dw = pd.DataFrame(all_rows)

if not df_dw.empty:
  # 防呆機制：去除同一天搜尋中「最佳航班」與「一般列表」重複出現的相同班次
  before_dedup = len(df_dw)
  df_dw = df_dw.drop_duplicates(
      subset=[
          "scrape_date",
          "departure_date",
          "destination",
          "airline",
          "departure_time",
          "price_hkd",
      ]
  )
  after_dedup = len(df_dw)

  os.makedirs("data/warehouse", exist_ok=True)
  db_path = "data/warehouse/flights_dw.db"
  conn = sqlite3.connect(db_path)

  df_dw.to_sql("fact_flight_prices", conn, if_exists="replace", index=False)
  conn.execute(
      "CREATE INDEX IF NOT EXISTS idx_dest_days ON fact_flight_prices"
      " (destination, days_left);"
  )
  conn.close()

  print(
      f"[Data Warehouse] 更新完成！原始 {before_dedup} 筆 -> 去重後"
      f" {after_dedup} 筆已寫入 {db_path}"
  )
else:
  print("[Data Warehouse] 無有效價格資料可寫入。")
