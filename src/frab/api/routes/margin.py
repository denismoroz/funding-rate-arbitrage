import time
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("")
async def get_margin_state(request: Request) -> dict:
    """Live two-tier margin assessment for UI."""
    watchdog = getattr(request.app.state, "margin_watchdog", None)
    if watchdog is None:
        raise HTTPException(status_code=503, detail="MarginWatchdog not configured")

    a = await watchdog.dry_assess()
    return {
        "ts_ms": int(time.time() * 1000),
        "account": {
            "ratio": a.account_ratio,
            "status": a.account_status.value,
            "equity_usdc": a.account_equity_usdc,
            "total_maintenance_usdc": a.total_maintenance_usdc,
        },
        "thresholds": {
            "healthy": watchdog._mgr.top_up_trigger,
            "forced_close": watchdog._mgr.forced_close_trigger,
            "liquidation": 1.0,
        },
        "per_fp": [
            {
                "farb_position_id": fp.farb_position_id,
                "coin": fp.coin,
                "virtual_ratio": fp.virtual_ratio,
                "status": fp.status.value,
                "virtual_equity_usdc": fp.virtual_equity,
                "virtual_maintenance_usdc": fp.virtual_maintenance,
            }
            for fp in a.per_fp
        ],
        "weakest_fp_id": a.weakest_fp_id,
    }
