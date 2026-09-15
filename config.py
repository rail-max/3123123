import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6150661049"))
PORT = int(os.getenv("PORT", "8080"))
# Platega: values are issued in the Platega dashboard.
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "").strip()
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "").strip()
PLATEGA_API_URL = os.getenv("PLATEGA_API_URL", "https://app.platega.io").rstrip("/")

# Public Railway domain, for example: https://your-service.up.railway.app
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

# These values are displayed in the public legal documents.
PROJECT_NAME = os.getenv("PROJECT_NAME", "DialogDelBot").strip() or "DialogDelBot"

# Optional public legal-document links, for example Telegraph pages.
PRIVACY_POLICY_URL = os.getenv("PRIVACY_POLICY_URL", "").strip()
TERMS_URL = os.getenv("TERMS_URL", "").strip()
