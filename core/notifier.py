import html
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, MAX_ALERTS_PER_WAVE

TELEGRAM_MAX_LEN = 4000
_VARIANT_QUERY_KEYS = {"c", "v1", "color"}


def _split_message(message: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split oversized HTML messages at paragraph boundaries to stay within Telegram limits."""
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current = ""
    for block in message.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(block) <= limit:
                current = block
            else:
                for i in range(0, len(block), limit):
                    chunks.append(block[i : i + limit])
                current = ""
    if current:
        chunks.append(current)
    return chunks


def send_telegram_message(message: str) -> bool:
    """Send an HTML-formatted notification message to Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Token veya Chat ID eksik, mesaj gönderilmedi.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    all_ok = True
    for chunk in _split_message(message):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                print("[Telegram] Bildirim gönderildi.")
            else:
                print(f"[Telegram] Hata: {resp.status_code} - {resp.text}")
                all_ok = False
        except Exception as exc:
            print(f"[Telegram] Bağlantı hatası: {exc}")
            all_ok = False
    return all_ok


class StockWaveNotifier:
    """
    Veritabanı olmaksızın RAM üzerinde çalışır:
    - Stok + İndirim varsa en fazla 3 kere mesaj atar (1/3, 2/3, 3/3).
    - 3. mesajdan sonra stok devam ederse mesaj atmaz.
    - Stok/indirim bittiğinde 1 kere 'Stok Tükendi' atar ve sayacı sıfırlar.
    - Tekrar stoğa girdiğinde yeniden 1/3'ten saymaya başlar.
    - Actions kapanıp açıldığında RAM sıfırlandığı için baştan başlar.
    """

    def __init__(self, store_label: str):
        self.store_label = store_label
        # { clean_url: {"count": int, "in_stock": bool, "name": str, "url": str} }
        self.states: dict[str, dict] = {}

    @staticmethod
    def _clean_url(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        parts = urlsplit(raw)
        kept = sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=False)
            if k.lower() in _VARIANT_QUERY_KEYS
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))

    def process_cycle(
        self,
        matched_products: list[dict],
        uncertain_urls: set[str] | None = None,
    ) -> None:
        """Evaluate current cycle matches and send wave alerts or out-of-stock resets."""
        current_map = {self._clean_url(p["url"]): p for p in matched_products}
        uncertain_clean = {self._clean_url(u) for u in (uncertain_urls or set()) if u}
        in_stock_alerts: list[tuple[dict, int]] = []
        out_of_stock_alerts: list[dict] = []

        # 1. Şu an hem stokta hem indirimde olan ürünleri değerlendir
        for clean_url, prod in current_map.items():
            state = self.states.get(
                clean_url,
                {
                    "count": 0,
                    "in_stock": False,
                    "name": prod["name"],
                    "url": prod["url"],
                },
            )
            state["name"] = prod["name"]
            state["url"] = prod["url"]

            if state["count"] < MAX_ALERTS_PER_WAVE:
                state["count"] += 1
                state["in_stock"] = True
                in_stock_alerts.append((prod, state["count"]))
            else:
                # 3. bildirim zaten atıldı, stok devam ediyor -> sessiz kal
                state["in_stock"] = True

            self.states[clean_url] = state

        # 2. Önceki turda stokta olup bu turda stoğu/indirimi biten ürünleri tespit et
        for clean_url, state in list(self.states.items()):
            if clean_url not in current_map and clean_url not in uncertain_clean and state["in_stock"]:
                state["in_stock"] = False
                state["count"] = 0  # Tekrar stoğa girerse baştan 3 kere atabilsin
                out_of_stock_alerts.append(state)

        # 3. Yeni stok + indirim mesajlarını gönder
        if in_stock_alerts:
            msg = f"🔥 <b>{html.escape(self.store_label)} — STOK + İNDİRİM YAKALANDI!</b>\n\n"
            for prod, alert_no in in_stock_alerts:
                safe_name = html.escape(prod["name"])
                safe_sizes = html.escape(", ".join(prod.get("available_sizes", [])))
                old_p = html.escape(prod.get("oldPrice", "").strip())
                new_p = html.escape(prod.get("newPrice", "").strip())
                safe_url = html.escape(prod["url"])

                msg += (
                    f"👗 <b>{safe_name}</b> "
                    f"<i>(Bildirim {alert_no}/{MAX_ALERTS_PER_WAVE})</i>\n"
                )
                if old_p and new_p and old_p != new_p:
                    msg += f"   💰 <s>{old_p}</s> → <b>{new_p}</b>\n"
                else:
                    msg += f"   💰 Fiyat: <b>{new_p or 'İndirimli'}</b>\n"
                msg += f"   📏 Bedenler: <b>{safe_sizes}</b>\n"
                msg += f"   🔗 {safe_url}\n\n"
            send_telegram_message(msg)

        # 4. Stoğu tükenen ürünlerin bilgisini gönder
        if out_of_stock_alerts:
            msg = f"📉 <b>{html.escape(self.store_label)} — STOK TÜKENDİ</b>\n\n"
            for st in out_of_stock_alerts:
                safe_name = html.escape(st["name"])
                safe_url = html.escape(st["url"])
                msg += f"❌ <b>{safe_name}</b>\n"
                msg += "   ℹ️ Stok/indirim sona erdi. Tekrar gelirse bildirim yeniden başlayacak.\n"
                msg += f"   🔗 {safe_url}\n\n"
            send_telegram_message(msg)
