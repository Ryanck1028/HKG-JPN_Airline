from datetime import datetime, timedelta, timezone
import json
import os
import random
import time
from fast_flights import FlightQuery, Passengers, create_query, get_flights

ORIGIN = "HKG"
DESTINATIONS = ["NRT", "HND", "KIX", "CTS", "FUK"]
LEAD_DAYS = [1, 3, 7, 14, 21, 30, 45, 60, 90]


HKT = timezone(timedelta(hours=8))
now_hkt = datetime.now(HKT)
scrape_date = now_hkt.strftime("%Y-%m-%d")

lake_records = []
total_flights_found = 0


def serialize_flight(flight_obj):
  """相容 Pydantic 模型或一般物件的序列化轉字典"""
  if hasattr(flight_obj, "model_dump"):
    return flight_obj.model_dump()
  elif hasattr(flight_obj, "dict"):
    return flight_obj.dict()
  elif hasattr(flight_obj, "__dict__"):
    return vars(flight_obj)
  return str(flight_obj)


print(f"=== [Step 1] 開始擷取原始數據至 Data Lake ({scrape_date} HKT) ===")

for dest in DESTINATIONS:
  for days in LEAD_DAYS:
    target_date = (now_hkt + timedelta(days=days)).strftime("%Y-%m-%d")
    try:
      # 使用 fast-flights v3 最新語法建立查詢
      query = create_query(
          flights=[
              FlightQuery(date=target_date, from_airport=ORIGIN, to_airport=dest)
          ],
          trip="one-way",
          seat="economy",
          passengers=Passengers(adults=1),
      )
      result = get_flights(query)

      # 相容 result 直接是 list 或帶有 .flights 屬性的回傳結構
      raw_items = getattr(result, "flights", result) or []
      flights_list = [serialize_flight(f) for f in raw_items]
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
    json.dump(lake_records, f, ensure_ascii=False, indent=2, default=str)
  print(
      f"[Data Lake] 寫入成功！共 {total_flights_found} 筆航班快照已儲存至"
      f" {lake_path}"
  )
else:
  print(
      "[Data Lake] 警告：本次擷取結果為 0 筆，為保護歷史數據，不覆蓋既有檔案。"
  )
