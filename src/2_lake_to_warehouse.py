from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import pandas as pd

# ==============================================================================
# 1. 路徑與業務邏輯常數定義 (Paths & Business Rules)
# ==============================================================================
# 自動鎖定專案根目錄 (無論從哪個資料夾執行，都能精準找到 data/lake 與 data/warehouse)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAKE_DIR = PROJECT_ROOT / "data" / "lake"
WAREHOUSE_DIR = PROJECT_ROOT / "data" / "warehouse"

# 香港飛日本主要廉價航空 (LCC) 名單，其餘歸類為傳統全服務航空 (FSC)
LCC_AIRLINES = {
    "Hong Kong Express",
    "HK Express",
    "Peach",
    "Jetstar",
    "Greater Bay Airlines",
    "Spring Airlines",
}


# ==============================================================================
# 2. 解析輔助函式 (Raw Card Lines Parser)
# ==============================================================================
def normalize_airline_name(raw_name: str) -> str:
  """
    清理 Google Flights 卡片上聯營航班黏在一起的名稱：
    例如將 'Cathay PacificJAL' 統一整理為 'Cathay Pacific / JAL'
    """
  if not raw_name:
    return "Unknown"
  cleaned = re.sub(r"([a-z])([A-Z])", r"\1 / \2", raw_name).strip()
  return cleaned


def parse_card_datetime(base_date_str: str, time_str: str):
  """將基準日期與時間字串 (如 '2:55 PM' 或 '6:20 AM+1') 轉為 Python datetime"""
  if not base_date_str or not time_str:
    return None
  try:
    base_dt = datetime.strptime(base_date_str, "%Y-%m-%d")
    day_offset = 0
    offset_match = re.search(r"\+(\d+)", time_str)
    if offset_match:
      day_offset = int(offset_match.group(1))

    clean_t = re.sub(r"\+\d+", "", time_str).strip()
    t_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", clean_t, re.I)
    if not t_match:
      return None

    hour = int(t_match.group(1))
    minute = int(t_match.group(2))
    ampm = t_match.group(3)

    if ampm:
      if ampm.upper() == "PM" and hour != 12:
        hour += 12
      elif ampm.upper() == "AM" and hour == 12:
        hour = 0

    return base_dt + timedelta(days=day_offset, hours=hour, minutes=minute)
  except Exception:
    return None


def parse_from_raw_card(flight_item: dict, base_date_str: str):
  """
    直接從 Data Lake 的 raw_card_lines 精準還原所有結構化指標：
    起降時間、航空公司、純飛行時數、轉機等候時數、總耗時與價格。
    """
  price = flight_item.get("price")
  lines = flight_item.get("raw_card_lines") or []
  if price is None or not lines:
    return None

  # 1. 解析起飛時間與降落時間 (支援跨行格式：第0個時間為起飛, 第1個時間為降落)
  time_pattern = r"^(\d{1,2}:\d{2}\s*(?:AM|PM)?(?:\+\d)?)$"
  time_indices = [
      i for i, line in enumerate(lines) if re.match(time_pattern, line, re.I)
  ]

  dep_str, arr_str, airline_raw = "", "", "Unknown"
  if len(time_indices) >= 2:
    dep_idx = time_indices[0]
    arr_idx = time_indices[1]
    dep_str = lines[dep_idx]
    arr_str = lines[arr_idx]
    # 航空公司名稱固定出現在降落時間的下一行
    if arr_idx + 1 < len(lines):
      airline_raw = lines[arr_idx + 1]

  airline = normalize_airline_name(airline_raw)
  dep_dt = parse_card_datetime(base_date_str, dep_str)
  arr_dt = parse_card_datetime(base_date_str, arr_str)

  # 2. 解析總旅程耗時 (分鐘)
  total_mins = int(flight_item.get("duration_mins") or 0)
  if total_mins == 0:
    for line in lines:
      m = re.match(r"^(\d+)\s*hr(?:\s*(\d+)\s*min)?$", line)
      if m:
        total_mins = int(m.group(1)) * 60 + (
            int(m.group(2)) if m.group(2) else 0
        )
        break

  # 3. 解析轉機次數與轉機等候時間 (例如 '2 hr 15 min TPE')
  stops = int(flight_item.get("stops") or 0)
  layover_mins = 0
  for line in lines:
    lay_m = re.search(
        r"(\d+)\s*hr(?:\s*(\d+)\s*min)?\s+(?:in\s+)?[A-Z]{3}\b", line
    )
    if lay_m:
      layover_mins += int(lay_m.group(1)) * 60 + (
          int(lay_m.group(2)) if lay_m.group(2) else 0
      )

  pure_flight_mins = max(0, total_mins - layover_mins)

  return {
      "airline": airline,
      "price": float(price),
      "stops": stops,
      "dep_dt": dep_dt,
      "arr_dt": arr_dt,
      "dep_time_str": dep_dt.strftime("%Y-%m-%d %H:%M") if dep_dt else dep_str,
      "arr_time_str": arr_dt.strftime("%Y-%m-%d %H:%M") if arr_dt else arr_str,
      "flight_mins": pure_flight_mins,
      "layover_mins": layover_mins,
      "total_mins": total_mins,
  }


# ==============================================================================
# 3. 讀取 Data Lake 並執行商業配對邏輯 (Transform)
# ==============================================================================
print("=== [Step 2] 從 Data Lake (raw_card_lines) 重建 Data Warehouse ===")

all_matched_rows = []
lake_files = sorted(LAKE_DIR.glob("*_roundtrip.json"))

for file_path in lake_files:
  with open(file_path, "r", encoding="utf-8") as f:
    snapshots = json.load(f)

  for snap in snapshots:
    meta = snap["meta"]
    dep_date_str = meta["departure_date"]
    ret_date_str = meta["return_date"]

    out_parsed = [
        parse_from_raw_card(x, dep_date_str)
        for x in (snap.get("outbound_raw") or [])
    ]
    out_parsed = [x for x in out_parsed if x is not None]

    in_parsed = [
        parse_from_raw_card(x, ret_date_str)
        for x in (snap.get("inbound_raw") or [])
    ]
    in_parsed = [x for x in in_parsed if x is not None]

    if not out_parsed:
      continue

    airlines_in_search = sorted(set(x["airline"] for x in out_parsed))

    # 雙層配對邏輯：每間航空公司最便宜且最快去程 + 同公司最便宜且最快回程
    for airline in airlines_in_search:
      airline_outs = [x for x in out_parsed if x["airline"] == airline]
      best_out = sorted(
          airline_outs, key=lambda x: (x["price"], x["total_mins"])
      )[0]

      airline_ins = [x for x in in_parsed if x["airline"] == airline]
      best_in = (
          sorted(airline_ins, key=lambda x: (x["price"], x["total_mins"]))[0]
          if airline_ins
          else None
      )

      out_flight_m = best_out["flight_mins"]
      out_layover_m = best_out["layover_mins"]
      in_flight_m = best_in["flight_mins"] if best_in else 0
      in_layover_m = best_in["layover_mins"] if best_in else 0

      total_flight_m = out_flight_m + in_flight_m
      total_layover_m = out_layover_m + in_layover_m

      dep_dt_obj = datetime.strptime(dep_date_str, "%Y-%m-%d")

      all_matched_rows.append({
          "scrape_date": meta["scrape_date"],
          "origin": meta["origin"],
          "destination": meta["destination"],
          "departure_date": dep_date_str,
          "return_date": ret_date_str,
          "days_left": meta["days_left"],
          "trip_duration_days": meta["trip_duration_days"],
          "dep_weekday": dep_dt_obj.weekday(),
          "is_weekend_dep": 1 if dep_dt_obj.weekday() >= 4 else 0,
          "airline": airline,
          "carrier_type": (
              "LCC" if any(lcc in airline for lcc in LCC_AIRLINES) else "FSC"
          ),
          "roundtrip_price_hkd": best_out["price"],
          # 去程時間指標
          "out_departure_time": best_out["dep_time_str"],
          "out_arrival_time": best_out["arr_time_str"],
          "out_stops": best_out["stops"],
          "out_flight_mins": out_flight_m,
          "out_layover_mins": out_layover_m,
          "is_out_redeye": (
              1
              if (best_out["dep_dt"] and 0 <= best_out["dep_dt"].hour <= 5)
              else 0
          ),
          # 回程時間指標
          "in_departure_time": best_in["dep_time_str"] if best_in else None,
          "in_arrival_time": best_in["arr_time_str"] if best_in else None,
          "in_stops": best_in["stops"] if best_in else None,
          "in_flight_mins": in_flight_m if best_in else None,
          "in_layover_mins": in_layover_m if best_in else None,
          # 總計時間指標
          "total_layover_mins": total_layover_m,
          "total_flight_mins": total_flight_m,
          "total_flight_hours": round(total_flight_m / 60.0, 2),
          "total_journey_hours": round(
              (total_flight_m + total_layover_m) / 60.0, 2
          ),
      })

# ==============================================================================
# 4. 寫入 Data Warehouse (SQLite + CSV 雙格式輸出)
# ==============================================================================
df_dw = pd.DataFrame(all_matched_rows)

if not df_dw.empty:
  df_dw = df_dw.drop_duplicates(
      subset=[
          "scrape_date",
          "destination",
          "departure_date",
          "return_date",
          "airline",
      ]
  )

  WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
  db_path = WAREHOUSE_DIR / "flights_dw.db"
  conn = sqlite3.connect(db_path)

  df_dw.to_sql("fact_roundtrip_flights", conn, if_exists="replace", index=False)
  conn.execute(
      "CREATE INDEX IF NOT EXISTS idx_rt_dest_days ON fact_roundtrip_flights"
      " (destination, days_left);"
  )
  conn.close()

  csv_path = WAREHOUSE_DIR / "fact_roundtrip_flights.csv"
  df_dw.to_csv(csv_path, index=False, encoding="utf-8-sig")
  print(
      f"[Data Warehouse] 更新完成！共產出 {len(df_dw)} 筆「最佳去回程配對」資料。"
  )
else:
  print("[Data Warehouse] 無有效資料可寫入。")
