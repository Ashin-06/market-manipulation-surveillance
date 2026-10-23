"""
mktsurv: Level-3 Limit Order Book (LOB) Microstructure Surveillance Engine.

Detects:
  1. Spoofing & Multi-Level Layering
  2. Self-Trade & Collusive Wash-Trading Rings
  3. Marking-the-Close / Closing-Bell Ramps
  4. VPIN Order-Flow Toxicity & Pre-Block Front-Running
"""

from .lob import (
    BookSnapshot,
    LimitOrderBook,
    OrderEvent,
    OrderLifecycle,
    RestingOrder,
    TradeFill,
)
from .vpin import (
    FrontRunningAlert,
    VPINBucket,
    VPINResult,
    compute_vpin,
    detect_front_running,
)
from .detectors import (
    ClosingRampAlert,
    SpoofingAlert,
    SurveillanceAlert,
    SurveillanceReport,
    WashTradingAlert,
    detect_marking_the_close,
    detect_spoofing_layering,
    detect_wash_trading,
    naive_otr_baseline,
    run_surveillance,
)

__all__ = [
    "BookSnapshot",
    "LimitOrderBook",
    "OrderEvent",
    "OrderLifecycle",
    "RestingOrder",
    "TradeFill",
    "VPINBucket",
    "VPINResult",
    "FrontRunningAlert",
    "compute_vpin",
    "detect_front_running",
    "SpoofingAlert",
    "WashTradingAlert",
    "ClosingRampAlert",
    "SurveillanceAlert",
    "SurveillanceReport",
    "detect_spoofing_layering",
    "detect_wash_trading",
    "detect_marking_the_close",
    "naive_otr_baseline",
    "run_surveillance",
]

__version__ = "1.0.0"
