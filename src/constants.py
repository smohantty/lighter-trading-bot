"""
Central configuration constants for lighter-trading-bot.

This module contains all tunable parameters and magic numbers used throughout
the trading bot. Modify values here to adjust bot behavior without changing
business logic.
"""

from src.strategy.types import Spread

# =============================================================================
# ENGINE CONSTANTS
# =============================================================================

# Auth Token Configuration
AUTH_TOKEN_EXPIRY_SECONDS = 8 * 60 * 60  # 8 hours
TOKEN_REFRESH_BUFFER_SECONDS = 60 * 60  # Refresh 1 hour before expiry

# Reconciliation Loop
RECONCILIATION_ENABLED = True  # Set to False to disable reconciliation loop
RECONCILIATION_INTERVAL_SECONDS =  5 * 60 # 5 mins

# API Batch Processing (Lighter supports up to 50 tx per batch)
MAX_BATCH_SIZE = 49


# =============================================================================
# STRATEGY CONSTANTS
# =============================================================================

# Order Retry Limits
MAX_ORDER_RETRIES = 5

# Spread & Buffer Configuration
ACQUISITION_SPREAD = Spread("0.1")  # 0.1% spread for off-grid acquisition
INVESTMENT_BUFFER_PERP = Spread("0.05")  # 0.05% buffer for perp grids
INVESTMENT_BUFFER_SPOT = Spread("0.1")  # 0.1% buffer for spot grids
FEE_BUFFER = Spread("0.05")  # 0.05% fee buffer for spot


# =============================================================================
# GRACE PERIODS & TIMEOUTS
# =============================================================================

ORDER_PROPAGATION_GRACE_SECONDS = 10  # Time to wait before considering order "missing"
ORDER_LOST_TIMEOUT_SECONDS = 120  # Time before marking order as "lost"
