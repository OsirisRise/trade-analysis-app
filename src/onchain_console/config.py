import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost:5432/trade_analysis"
)

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
