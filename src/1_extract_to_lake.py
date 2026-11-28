from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import re
import time
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ==============================================================================
# 1. 路徑與專案參數設定區 (Paths & Configuration)
# ==============================================================================
# 永遠精準指向最外層專案根目錄 (HKG-JPN_Airline)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAKE_DIR = PROJECT_ROOT / "data" / "lake"

ORIGIN = "HKG"  # 出發地：香港國際機場
DESTINATIONS = [
    "NRT",  # 東京成田 (Narita)
    "HND",  # 東京羽田 (Haneda)
    "KIX",  # 大阪關西 (Kansai)
    "CTS",  # 札幌新千歲 (New Chitose)
    "FUK",  # 福岡 (Fukuoka)
]
# 前兩週每天抓（觀察臨近起飛跳價）+ 15~45天每3天抓 + 45~90天每5天抓
LEAD_DAYS = sorted(
      list(range(3, 15)) + list(range(15, 45, 3)) + list(range(45, 91, 5))
  )
TRIP_DURATIONS = [4,6]# 日本停留天數 (5日4夜、7日6夜)
CURRENCY = "HKD"  # 強制向 Google Flights 請求港幣報價

# 鎖定香港時區 (UTC+8)
HKT = timezone(timedelta(hours=8))
now_hkt = datetime.now(HKT)
scrape_date = now_hkt.strftime("%Y-%m-%d")

# 分段組合基礎網址，確保路徑完整不被截斷
BASE_FLIGHTS_URL = "https://google.com" + "/travel/flights"


# ==============================================================================
# 2. Playwright 頁面解析器（完整抓取綠色最佳航班 + 一般航班）
# ==============================================================================
def scrape_google_flights_page(page, url: str, max_retries: int = 2):
  """
    使用 Playwright 無頭瀏覽器開啟 Google Flights 搜尋頁面，完整解析所有航班卡片。
    優點：
    1) 同時抓得到網頁最上方「最佳航班（綠色金額）」與下方「其他航班（黑色金額）」。
    2) 內建頁面捲動與重試機制，確保回程列表完整渲染不漏抓。
    """
  for attempt in range(1, max_retries + 1):
    try:
      page.goto(url, wait_until="domcontentloaded", timeout=45000)

      # 等待航班列表卡片 (li.pIav2d) 載入
      page.wait_for_selector("li.pIav2d", timeout=15000)

      # 模擬向下捲動並稍作等待，確保動態價格與下方列表完全渲染
      page.mouse.wheel(0, 800)
      time.sleep(2.0)

      flights_data = []
      cards = page.locator("li.pIav2d").all()

      for card in cards:
        text = card.inner_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
          continue

        # 提取價格 (格式如 "HK$2,450"，涵蓋綠色最低價與一般價格)
        price_match = re.search(r"HK\$\s?([\d,]+)", text)
        if not price_match:
          continue
        price_val = float(price_match.group(1).replace(",", ""))

        # 提取總旅程耗時並換算為分鐘 (格式如 "4 hr 10 min" 或 "6 hr")
        dur_match = re.search(r"(\d+)\s?hr(?:\s?(\d+)\s?min)?", text)
        if dur_match:
          hrs = int(dur_match.group(1))
          mins = int(dur_match.group(2)) if dur_match.group(2) else 0
          duration_mins = hrs * 60 + mins
        else:
          duration_mins = 0

        # 提取轉機次數 ("Nonstop" -> 0, "1 stop" -> 1)
        if "Nonstop" in text:
          stops = 0
        else:
          stop_match = re.search(r"(\d+)\s?stop", text)
          stops = int(stop_match.group(1)) if stop_match else 0

        flights_data.append({
            "price": price_val,
            "duration_mins": duration_mins,
            "stops": stops,
            "raw_card_lines": lines,  # 完整保留卡片文字行，供 Warehouse 精準解析時間與航空公司
        })

      return flights_data

    except Exception as e:
      if attempt < max_retries:
        print(f"    [頁面重試 {attempt}/{max_retries-1}] 載入超時或未渲染，3秒後重試...")
        time.sleep(3)
      else:
        return []


# ==============================================================================
# 3. 核心擷取流程 (去程來回總價 + 單程回程班表 + 快取去重)
# ==============================================================================
lake_records = []
total_flights_found = 0

# 回程查詢快取：key = (目的地機場, 回程日期), value = 該日返港單程航班列表
# 目的：避免不同去程組合碰到同一天回程時重複發送請求，大幅提升執行速度
inbound_cache = {}

print(
    f"=== [Step 1] 啟動 Playwright 擷取【去程來回總價＋單程回程班表】({scrape_date}"
    " HKT) ==="
)

with sync_playwright() as p:
  browser = p.chromium.launch(headless=True)
  context = browser.new_context(
      locale="en-US",
      timezone_id="Asia/Hong_Kong",
      user_agent=(
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
      ),
  )
  page = context.new_page()

  for dest in DESTINATIONS:
    for days in LEAD_DAYS:
      dep_dt = now_hkt + timedelta(days=days)
      dep_date_str = dep_dt.strftime("%Y-%m-%d")

      for duration in TRIP_DURATIONS:
        ret_dt = dep_dt + timedelta(days=duration)
        ret_date_str = ret_dt.strftime("%Y-%m-%d")
        route_label = f"{ORIGIN}<->{dest} ({dep_date_str} ~ {ret_date_str})"

        try:
          # ------------------------------------------------------------------
          # 步驟 3A：去程搜尋 (Round-trip 模式) -> 取得「來回總價」與「去程時間明細」
          # ------------------------------------------------------------------
          out_params = urlencode({
              "q": (
                  f"Flights to {dest} from {ORIGIN} on {dep_date_str} through"
                  f" {ret_date_str}"
              ),
              "curr": CURRENCY,
              "hl": "en",
          })
          out_url = f"{BASE_FLIGHTS_URL}?{out_params}"
          out_flights = scrape_google_flights_page(page, out_url)

          time.sleep(random.uniform(1.0, 1.8))

          # ------------------------------------------------------------------
          # 步驟 3B：單程回程搜尋 (One-way 模式) -> 取得「回程班表起降與轉機明細」
          # ------------------------------------------------------------------
          cache_key = (dest, ret_date_str)
          if cache_key in inbound_cache:
            in_flights = inbound_cache[cache_key]
            cache_tag = "(快取命中)"
          else:
            in_params = urlencode({
                "q": f"Flights to {ORIGIN} from {dest} on {ret_date_str} oneway",
                "curr": CURRENCY,
                "hl": "en",
            })
            in_url = f"{BASE_FLIGHTS_URL}?{in_params}"
            in_flights = scrape_google_flights_page(page, in_url)
            inbound_cache[cache_key] = in_flights
            cache_tag = "(新抓取)"
            time.sleep(random.uniform(1.0, 1.8))

          total_flights_found += len(out_flights) + len(in_flights)

          # 寫入 Data Lake 結構
          lake_records.append({
              "meta": {
                  "scrape_date": scrape_date,
                  "scrape_timestamp": now_hkt.isoformat(),
                  "origin": ORIGIN,
                  "destination": dest,
                  "departure_date": dep_date_str,
                  "return_date": ret_date_str,
                  "days_left": days,
                  "trip_duration_days": duration,
                  "currency_requested": CURRENCY,
              },
              "outbound_raw": out_flights,
              "inbound_raw": in_flights,
          })

          min_p = (
              min(f["price"] for f in out_flights) if out_flights else "N/A"
          )
          print(
              f"成功: {route_label} | 去:{len(out_flights)}班,"
              f" 回:{len(in_flights)}班 {cache_tag} (最低來回價: HK${min_p})"
          )

        except Exception as e:
          print(f"[警告] {route_label} 擷取失敗: {e}")
          time.sleep(2)

  browser.close()

# ==============================================================================
# 4. 防呆寫入 Data Lake (Idempotent Snapshot Storage)
# ==============================================================================
LAKE_DIR.mkdir(parents=True, exist_ok=True)
lake_path = LAKE_DIR / f"{scrape_date}_roundtrip.json"

if total_flights_found > 0:
  with open(lake_path, "w", encoding="utf-8") as f:
    json.dump(lake_records, f, ensure_ascii=False, indent=2)
  print(
      f"\n[Data Lake] 寫入成功！共 {total_flights_found} 筆航班快照已儲存至"
      f" {lake_path}"
  )
else:
  print(
      "\n[Data Lake] 警告：本次擷取結果為 0 筆，為保護歷史數據，不覆蓋既有檔案。"
  )
