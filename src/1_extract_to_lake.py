from datetime import datetime, timedelta, timezone
import json
import os
import random
import time
from fast_flights import FlightData, Passengers, Result, get_flights

ORIGIN = "HKG"
DESTINATIONS = ["NRT", "HND", "KIX", "CTS", "FUK"]
LEAD_DAYS = [1, 3, 7, 14, 21, 30, 45, 60, 90]


HKT = timezone(timedelta(hours=8))
now_hkt = datetime.now(HKT)
scrape_date = now_hkt.strftime("%Y-%m-%d")

lake_records = []
total_flights_found = 0

print(f"=== [Step 1] 開始擷取原始數據至 Data Lake ({scrape_date} HKT) ===")

for dest in DESTINATIONS:
  for days in LEAD_DAYS:
    target_date = (now_hkt + timedelta(days=days)).strftime("%Y-%m-%d")
    try:
      result: Result = get_flights(
          flight_data=[
              FlightData(date=target_date, from_airport=ORIGIN, to_airport=dest)
          ],
          trip="one-way",
          seat="economy",
          passengers=Passengers(adults=1),
          fetch_mode="fallback",
      )

      flights_list = [vars(f) for f in result.flights]
      total_flights_found += len(flights_list)

      lake_records.append({
          "meta": {
              "scrape_date": scrape_date,
              "scrape_timestamp": now_hkt.isoformat(),
              "origin": ORIGIN,
              "destination": dest,
              "departure_date": target_date,
              "days_left": days,
              "price_level": getattr(result, "current_price", None),
          },
          "raw_flights": flights_list,
      })
      print(f"成功: {ORIGIN}->{dest} ({target_date}) 取得 {len(flights_list)} 筆")
      time.sleep(random.uniform(1.5, 3.0))

    except Exception as e:
      print(f"[警告] {dest} {target_date} 擷取失敗: {e}")
      time.sleep(2)

# 防呆機制：確保本次確實有抓到航班數據，才寫入/覆蓋當天的 Data Lake 檔案
os.makedirs("data/lake", exist_ok=True)
lake_path = f"data/lake/{scrape_date}.json"

if total_flights_found > 0:
  with open(lake_path, "w", encoding="utf-8") as f:
    json.dump(lake_records, f, ensure_ascii=False, indent=2)
  print(
      f"[Data Lake] 寫入成功！共 {total_flights_found} 筆航班快照已儲存至"
      f" {lake_path}"
  )
else:
  print(
      "[Data Lake] 警告：本次擷取結果為 0 筆，為保護歷史數據，不覆蓋既有檔案。"
  )
