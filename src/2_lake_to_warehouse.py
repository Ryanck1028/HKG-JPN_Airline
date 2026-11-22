from datetime import datetime
import glob
import json
import os
import re
import sqlite3
import pandas as pd

LCC_AIRLINES = {"HK Express", "Peach", "Jetstar Japan", "Greater Bay Airlines"}
USD_TO_HKD = 7.80


def clean_and_convert_price(price_val):
  """相容 fast-flights v3 的純數字 (int/float) 以及舊版字串格式"""
  if price_val is None:
    return None, None

  # 情況 A：v3 版直接回傳純數字 (例如 1520 或 195.5)
  if isinstance(price_val, (int, float)):
    if price_val <= 0:
      return None, None
    # 經驗法則：香港飛日本單程機票若小於 450，極高機率是美國伺服器回傳的 USD
    if price_val < 450:
      return "USD", round(float(price_val) * USD_TO_HKD, 2)
    return "HKD", round(float(price_val), 2)

  # 情況 B：若為字串 (例如 "HK$1,520" 或 "$195")
  price_str = str(price_val)
  digits = re.sub(r"[^\d.]", "", price_str)
  if not digits:
    return None, None
  val = float(digits)

  if "HK$" in price_str or "HKD" in price_str:
    return "HKD", round(val, 2)
  elif "$" in price_str or val < 450:
    return "USD", round(val * USD_TO_HKD, 2)
  return "HKD", round(val, 2)


def parse_hour(time_val):
  """相容 v3 巢狀時間字典或一般時間字串"""
  if isinstance(time_val, dict):
    # 若 v3 回傳 {'hour': 2, 'minute': 15} 或類似結構
    if "hour" in time_val:
      return int(time_val["hour"])
    time_val = str(time_val)

  match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", str(time_val), re.I)
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
      # 相容 v3 可能是數字或字典 {'amount': 1520, 'currency': 'HKD'}
      raw_p = fl.get("price")
      if isinstance(raw_p, dict):
        orig_curr = raw_p.get("currency", "UNKNOWN")
        amt = raw_p.get("amount") or raw_p.get("value")
        price_hkd = (
            round(float(amt) * USD_TO_HKD, 2)
            if orig_curr == "USD"
            else (round(float(amt), 2) if amt else None)
        )
      else:
        orig_curr, price_hkd = clean_and_convert_price(raw_p)

      if price_hkd is None:
        continue

      # 相容不同版本的航空公司欄位名稱 (airlines 列表或 name 字串)
      airlines_field = fl.get("airlines") or fl.get("name") or "Unknown"
      if isinstance(airlines_field, list):
        airline = ", ".join(str(a) for a in airlines_field)
      else:
        airline = str(airlines_field).strip()

      # 相容出發時間與轉機次數欄位
      dep_raw = fl.get("departure") or fl.get("departure_time")
      dep_hour = parse_hour(dep_raw)

      flights_legs = fl.get("flights")
      stops = (
          len(flights_legs) - 1
          if isinstance(flights_legs, list) and len(flights_legs) > 0
          else fl.get("stops", 0)
      )

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
          "departure_time": str(dep_raw),
          "is_redeye": (
              1 if (dep_hour is not None and 0 <= dep_hour <= 5) else 0
          ),
          "stops": stops,
          "original_currency": orig_curr,
          "price_hkd": price_hkd,
      })

df_dw = pd.DataFrame(all_rows)

if not df_dw.empty:
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
  df_dw.to_csv("data/warehouse/flights_dw.csv", index=False)
  print(
      f"[Data Warehouse] 更新完成！原始 {before_dedup} 筆 -> 去重後"
      f" {after_dedup} 筆已寫入 {db_path}"
  )
else:
  print("[Data Warehouse] 無有效價格資料可寫入。")
