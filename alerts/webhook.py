import os
import logging
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def send_fallback_alert(portal_name: str, layer_failed: str, failure_reason: str):
    """
    Sends an alert notification via webhook (Slack/Email/Telegram/HTTP Webhook) when Layer 4 fallback is reached.
    """
    webhook_url = os.getenv("ALERT_WEBHOOK_URL")
    timestamp_str = datetime.now(timezone.utc).isoformat()

    alert_payload = {
        "text": f"🚨 [Pipeline Alert] Layer 4 Fallback Triggered for '{portal_name}'!",
        "portal": portal_name,
        "failed_layer": layer_failed,
        "reason": failure_reason,
        "timestamp": timestamp_str,
        "status": "DEGRADED_SERVED_CACHE",
    }

    logger.error(
        f"🚨 [Layer 4 Alert] Portal '{portal_name}' failed at {layer_failed}. Serving last known good data. Reason: {failure_reason}"
    )

    if not webhook_url:
        logger.warning("[Alerts] ALERT_WEBHOOK_URL is not set in environment. Skipping webhook POST.")
        return

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(webhook_url, json=alert_payload)
            if res.status_code in [200, 201, 202, 204]:
                logger.info(f"[Alerts] Webhook alert successfully sent for '{portal_name}' (HTTP {res.status_code})")
            else:
                logger.warning(f"[Alerts] Webhook endpoint returned HTTP {res.status_code}")
    except Exception as e:
        logger.error(f"[Alerts] Error sending webhook alert for '{portal_name}': {e}")
