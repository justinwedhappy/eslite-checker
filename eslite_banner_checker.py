"""
誠品線上 (eslite.com) 首頁 banner 過期 / 缺貨自動檢查腳本
=========================================================

功能:
    1. 開啟 https://www.eslite.com/ 首頁,抓出所有 banner 版位的連結與名稱
    2. 判斷每個 banner 連結的活動頁是否「疑似過期」
       (404 / 被導回首頁 / 頁面文字出現「活動已結束」等關鍵字)
    3. 對未過期的 banner 頁面,抓出頁面內所有商品連結 (/product/xxxx)
    4. 逐一開啟每個商品頁,依照「加入購物車按鈕下方的狀態提示文字」
       判斷該商品是否缺貨 / 無法購買
    5. 輸出一份 CSV 報表,並在終端機印出摘要

安裝:
    pip install playwright
    playwright install chromium

執行:
    python eslite_banner_checker.py
    python eslite_banner_checker.py --output report.csv --max-products-per-banner 15

Email 通知(選用):
    有發現異常時,可自動寄一封通知信。信箱/SMTP 帳密一律用「環境變數」提供,
    不要寫死在程式碼裡。需要設定的環境變數:

        SMTP_HOST      SMTP 伺服器位址 (例如 smtp.gmail.com)
        SMTP_PORT      SMTP 連接埠 (預設 587)
        SMTP_USER      登入帳號 (通常就是寄件信箱)
        SMTP_PASSWORD  登入密碼(Gmail 請用「應用程式專用密碼」,不是登入密碼本人)
        SMTP_FROM      寄件人信箱(不設定則沿用 SMTP_USER)

    設定好環境變數後,加上 --notify-email 參數即可:
        python eslite_banner_checker.py --notify-email you@example.com

注意:
    - 這支腳本只會「跑一次」,若要自動排程,請搭配本機 cron / Windows 工作排程器 /
      GitHub Actions 的 schedule trigger 來定期呼叫這支腳本。
    - 網站的 DOM 結構可能隨時改版,若抓不到 banner 或商品連結,
      請重新用瀏覽器開發工具檢查 CSS selector 是否仍然正確(見下方 SELECTORS)。
"""

import argparse
import csv
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HOMEPAGE = "https://www.eslite.com/"

# ---------------------------------------------------------------------------
# 可能需要依網站改版調整的地方都集中在這裡
# ---------------------------------------------------------------------------

# 判斷「活動已過期」的頁面文字關鍵字
EXPIRED_KEYWORDS = [
    "活動已結束",
    "活動已過期",
    "已下架",
    "本活動已結束",
    "頁面不存在",
    "找不到頁面",
    "activity has ended",
]

# 判斷「商品缺貨/無法購買」的頁面文字關鍵字
# (誠品實際觀察到的文字範例:「熱銷補貨中，貨到後即將安排出貨，感謝您的耐心等候！」)
OUT_OF_STOCK_KEYWORDS = [
    "補貨中",
    "缺貨",
    "已售完",
    "售罄",
    "已下架",
    "無法販售",
    "暫停販售",
    "到貨通知",
]

# 商品連結的判斷規則:href 包含 /product/
PRODUCT_LINK_PATTERN = re.compile(r"/product/")


@dataclass
class BannerResult:
    name: str
    url: str
    status: str = "正常"          # 正常 / 疑似過期 / 連結失效
    reason: str = ""
    out_of_stock_products: list = field(default_factory=list)


def looks_like_homepage_redirect(final_url: str) -> bool:
    """判斷最終網址是否被導回首頁(可能代表活動已下架)"""
    parsed = urlparse(final_url)
    path = parsed.path.rstrip("/")
    return path in ("", "/") and "eslite.com" in parsed.netloc


def collect_homepage_banners(page) -> list:
    """
    抓首頁所有 banner 連結。
    這裡用「圖片周圍的 <a>」為主要策略,並用連結文字/圖片 alt 當作版位名稱。
    可依實際 DOM 結構調整 selector。
    """
    page.goto(HOMEPAGE, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)  # 給輪播 / 延遲載入的區塊多一點時間

    anchors = page.query_selector_all("a[href]")
    banners = []
    seen = set()

    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue

        full_url = urljoin(HOMEPAGE, href)

        # 只保留看起來像「活動 banner」的連結:
        #   - events.eslite.com 開頭的活動頁
        #   - /event-category/、/exhibitions/、/campaign/、/coupon/ 開頭的站內活動頁
        if not (
            "events.eslite.com" in full_url
            or "/event-category/" in full_url
            or "/exhibitions/" in full_url
            or "/campaign/" in full_url
            or "/coupon/" in full_url
        ):
            continue

        if full_url in seen:
            continue
        seen.add(full_url)

        # 版位名稱:優先用連結文字,沒有的話用圖片 alt
        name = (a.inner_text() or "").strip()
        if not name:
            img = a.query_selector("img")
            if img:
                name = (img.get_attribute("alt") or "").strip()
        if not name:
            name = full_url

        banners.append({"name": name, "url": full_url})

    return banners


def check_banner_expired(page, banner_url: str) -> tuple:
    """
    回傳 (status, reason)
    status: "正常" / "疑似過期" / "連結失效"
    """
    try:
        response = page.goto(banner_url, wait_until="networkidle", timeout=30000)
    except PWTimeout:
        return "連結失效", "頁面載入逾時"

    if response is None:
        return "連結失效", "無回應"

    if response.status >= 400:
        return "連結失效", f"HTTP {response.status}"

    final_url = page.url
    if looks_like_homepage_redirect(final_url) and final_url.rstrip("/") != banner_url.rstrip("/"):
        return "疑似過期", f"連結被導回首頁 ({final_url})"

    body_text = page.inner_text("body")
    for kw in EXPIRED_KEYWORDS:
        if kw in body_text:
            return "疑似過期", f"頁面含關鍵字「{kw}」"

    return "正常", ""


def collect_product_links(page, max_products: int) -> list:
    """在目前這個活動頁裡抓出所有商品連結"""
    anchors = page.query_selector_all("a[href]")
    links = []
    seen = set()
    for a in anchors:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        if PRODUCT_LINK_PATTERN.search(href):
            full_url = urljoin(HOMEPAGE, href)
            if full_url not in seen:
                seen.add(full_url)
                name = (a.inner_text() or "").strip()
                links.append({"url": full_url, "name": name})
        if len(links) >= max_products:
            break
    return links


def check_product_out_of_stock(page, product_url: str) -> tuple:
    """
    回傳 (is_out_of_stock: bool, matched_text: str)
    邏輯:抓「加入購物車」按鈕附近區塊的文字,比對缺貨關鍵字。
    找不到按鈕時,退而求其次比對整頁文字。
    """
    try:
        page.goto(product_url, wait_until="networkidle", timeout=30000)
    except PWTimeout:
        return False, "頁面載入逾時,略過判斷"

    page.wait_for_timeout(800)

    try:
        body_text = page.inner_text("body")
    except Exception:
        return False, "無法讀取頁面內容"

    for kw in OUT_OF_STOCK_KEYWORDS:
        if kw in body_text:
            return True, kw

    return False, ""


def build_email_body(results: list) -> str:
    """把有問題的版位整理成一封信的內文(純文字)"""
    problem_banners = [r for r in results if r.status != "正常" or r.out_of_stock_products]

    if not problem_banners:
        return "本次檢查沒有發現異常版位或缺貨商品。"

    lines = [f"本次檢查共發現 {len(problem_banners)} 個版位有問題:\n"]
    for r in problem_banners:
        lines.append(f"【{r.name}】")
        lines.append(f"  連結: {r.url}")
        if r.status != "正常":
            lines.append(f"  狀態: {r.status} - {r.reason}")
        if r.out_of_stock_products:
            lines.append(f"  缺貨商品 ({len(r.out_of_stock_products)} 個):")
            for prod in r.out_of_stock_products:
                lines.append(f"    - {prod['name']} ({prod['url']}) [關鍵字: {prod['matched']}]")
        lines.append("")

    return "\n".join(lines)


def send_email_notification(recipient: str, subject: str, body: str):
    """
    用環境變數提供的 SMTP 設定寄出通知信。
    需要的環境變數: SMTP_HOST, SMTP_PORT(選填,預設587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM(選填)
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM", user)

    missing = [name for name, val in [("SMTP_HOST", host), ("SMTP_USER", user), ("SMTP_PASSWORD", password)] if not val]
    if missing:
        print(f"[email] 缺少環境變數 {missing},略過寄信。請參考檔頭說明設定 SMTP_HOST / SMTP_USER / SMTP_PASSWORD。")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(sender, [recipient], msg.as_string())
        print(f"[email] 通知信已寄出至 {recipient}")
    except Exception as e:
        print(f"[email] 寄信失敗: {e}")


def run(
    output_path: str,
    max_products_per_banner: int,
    headless: bool,
    notify_email: str = None,
    email_always: bool = False,
):
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        print(f"[1/4] 開啟首頁抓取 banner 連結: {HOMEPAGE}")
        banners = collect_homepage_banners(page)
        print(f"      共抓到 {len(banners)} 個 banner 版位\n")

        for i, b in enumerate(banners, 1):
            print(f"[2/4] ({i}/{len(banners)}) 檢查版位:「{b['name']}」-> {b['url']}")
            status, reason = check_banner_expired(page, b["url"])
            result = BannerResult(name=b["name"], url=b["url"], status=status, reason=reason)

            if status == "正常":
                products = collect_product_links(page, max_products_per_banner)
                print(f"      找到 {len(products)} 個商品連結,開始檢查缺貨狀態...")
                for prod in products:
                    is_oos, matched_kw = check_product_out_of_stock(page, prod["url"])
                    if is_oos:
                        result.out_of_stock_products.append(
                            {"name": prod["name"] or prod["url"], "url": prod["url"], "matched": matched_kw}
                        )
                    time.sleep(0.3)  # 放慢速度,避免對網站造成太大負擔
            else:
                print(f"      -> {status} ({reason}),略過商品檢查")

            results.append(result)
            print()

        browser.close()

    print("[3/4] 寫出報表...")
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["版位名稱", "banner連結", "狀態", "原因", "缺貨商品名稱", "缺貨商品連結", "命中關鍵字"])
        for r in results:
            if r.out_of_stock_products:
                for prod in r.out_of_stock_products:
                    writer.writerow([r.name, r.url, r.status, r.reason, prod["name"], prod["url"], prod["matched"]])
            else:
                writer.writerow([r.name, r.url, r.status, r.reason, "", "", ""])

    print(f"[4/4] 完成!報表已存到: {output_path}\n")

    # 終端機摘要
    problem_banners = [r for r in results if r.status != "正常" or r.out_of_stock_products]
    print("=" * 60)
    print(f"摘要:共檢查 {len(results)} 個版位,發現 {len(problem_banners)} 個有問題")
    print("=" * 60)
    for r in problem_banners:
        print(f"\n【{r.name}】 {r.url}")
        if r.status != "正常":
            print(f"  狀態: {r.status} - {r.reason}")
        if r.out_of_stock_products:
            print(f"  缺貨商品 ({len(r.out_of_stock_products)} 個):")
            for prod in r.out_of_stock_products:
                print(f"    - {prod['name']} ({prod['url']}) [關鍵字: {prod['matched']}]")

    if not problem_banners:
        print("\n沒有發現異常版位或缺貨商品 ✅")

    # Email 通知:預設只在「有問題」時才寄信,避免每天洗版
    if notify_email:
        if problem_banners or email_always:
            subject = f"[誠品線上檢查] 發現 {len(problem_banners)} 個異常版位" if problem_banners else "[誠品線上檢查] 一切正常"
            body = build_email_body(results)
            send_email_notification(notify_email, subject, body)
        else:
            print("[email] 沒有異常,依預設設定不寄信(如需每次都收信,加上 --email-always)")


def main():
    parser = argparse.ArgumentParser(description="誠品線上首頁 banner 過期 / 商品缺貨檢查")
    parser.add_argument("--output", default="eslite_report.csv", help="輸出的 CSV 報表路徑")
    parser.add_argument(
        "--max-products-per-banner",
        type=int,
        default=20,
        help="每個 banner 頁面最多檢查幾個商品(避免執行時間過長)",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="加這個參數會顯示瀏覽器視窗(預設是無頭模式,不顯示畫面)",
    )
    parser.add_argument(
        "--notify-email",
        default=None,
        help="設定收件信箱後,有發現異常會自動寄信通知(需先設定 SMTP_* 環境變數,見檔頭說明)",
    )
    parser.add_argument(
        "--email-always",
        action="store_true",
        help="預設只有發現異常才寄信;加這個參數則每次執行都寄信(含「一切正常」的結果)",
    )
    args = parser.parse_args()

    try:
        run(
            args.output,
            args.max_products_per_banner,
            headless=not args.show_browser,
            notify_email=args.notify_email,
            email_always=args.email_always,
        )
    except KeyboardInterrupt:
        print("\n已手動中斷。")
        sys.exit(1)


if __name__ == "__main__":
    main()
