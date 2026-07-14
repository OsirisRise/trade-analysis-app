import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost:5432/trade_analysis"
)

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

METALS_DEV_API_URL = "https://api.metals.dev/v1/latest"
EIA_API_URL = "https://api.eia.gov/v2"

# BydFi public market data — no API key exists or is needed (public
# interface); see bydfi.py's hard boundary comment.
BYDFI_PUBLIC_URL = "https://www.bydfi.com/swap/public"

# Reference spot sources (see spot_prices.py). Keys live in .env, never in git.
METALS_DEV_API_KEY = os.environ.get("METALS_DEV_API_KEY", "")
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
