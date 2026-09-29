"""Interactive QR login flow — separated to keep client.py small."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from synapse_core.timeutil import local_now

from ._auth import CONFIG_DIR, ILINK_BASE_URL, save_token, validate_base_url

if TYPE_CHECKING:
    from .client import ILinkClient

logger = logging.getLogger(__name__)


def run_qr_login(client: ILinkClient) -> None:
    """Block until WeChat scan confirms or QR times out. Mutates client state."""
    resp = client._client.get(
        f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
        params={"bot_type": "3"},
    )
    resp.raise_for_status()
    data = resp.json()

    qrcode_id = data["qrcode"]
    qr_content = data.get("qrcode_img_content", "")
    _display_qr(qrcode_id, qr_content)

    print("\nWaiting for WeChat scan...")
    max_wait = 300  # 5 minutes
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        resp = client._client.get(
            f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode_id},
            headers={"iLink-App-ClientVersion": "1"},
        )
        resp.raise_for_status()
        status_data = resp.json()
        status = status_data.get("status", "")
        if status == "confirmed":
            client.bot_token = status_data["bot_token"]
            client.base_url = validate_base_url(
                status_data.get("baseurl", ILINK_BASE_URL)
            )
            save_token(
                {
                    "bot_token": client.bot_token,
                    "base_url": client.base_url,
                    "login_time": local_now().strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
            print("Login successful!")
            return
        elif status == "expired":
            raise TimeoutError("QR code expired. Please try again.")
        time.sleep(2)
    raise TimeoutError("QR code login timed out after 5 minutes.")


def _display_qr(qrcode_id: str, qr_content: str) -> None:
    content = qr_content or qrcode_id
    try:
        import qrcode as qr_lib

        qr = qr_lib.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("\nScan the QR code above with WeChat.")
    except ImportError:
        pass
    try:
        import qrcode as qr_lib

        qr_path = CONFIG_DIR / "qr.png"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        img = qr_lib.make(content)
        img.save(str(qr_path))
        print(f"QR image also saved to: {qr_path}")
    except Exception:
        print(f"QR content: {content}")
