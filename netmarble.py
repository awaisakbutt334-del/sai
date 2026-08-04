"""
Netmarble Solo Leveling: Arise coupon redemption.

Talks directly to Netmarble's official public coupon API:
  POST https://coupon.netmarble.com/api/coupon
  body: { gameCode, pid, couponCode, langCd }

No Netmarble login is required — only the in-game character ID (pid).
"""

import aiohttp
import asyncio
from typing import TypedDict


class RedeemResult(TypedDict):
    character_id: str
    success: bool
    message: str


COUPON_URL = "https://coupon.netmarble.com/api/coupon"
GAME_CODE = "sololv"
LANG_CODE = "en"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://coupon.netmarble.com",
    "Referer": "https://coupon.netmarble.com/sololv",
}

# Friendly messages for known Netmarble error codes.
ERROR_MESSAGES = {
    0: "Code redeemed successfully!",
    200: "Code redeemed successfully — item delivered!",
    22001: "Game code missing (internal error).",
    22002: "Language code missing (internal error).",
    22003: "Player ID missing.",
    22004: "Coupon code missing.",
    23001: "Coupon code not found or invalid.",
    23002: "This coupon code has expired.",
    23003: "This coupon has already been used on this account.",
    23004: "Coupon usage limit reached.",
    24004: "This code has already been redeemed (or its usage limit was reached).",
    23005: "This coupon is not available in your region.",
    23006: "Coupon is not active yet.",
}


def _friendly(code: int, raw_message: str) -> str:
    if code in ERROR_MESSAGES:
        return ERROR_MESSAGES[code]
    msg = raw_message.strip() if raw_message else ""
    return f"Netmarble: {msg} (error {code})" if msg else f"Netmarble error {code}"


async def redeem_one(session: aiohttp.ClientSession, character_id: str, code: str) -> RedeemResult:
    payload = {
        "gameCode": GAME_CODE,
        "pid": character_id,
        "couponCode": code,
        "langCd": LANG_CODE,
    }
    try:
        async with session.post(COUPON_URL, json=payload, headers=HEADERS, timeout=20) as resp:
            raw = await resp.text()
            import json as _json
            try:
                data = _json.loads(raw)
            except Exception:
                return {
                    "character_id": character_id,
                    "success": False,
                    "message": f"Unexpected response (HTTP {resp.status}): {raw[:200]}",
                }

            err_code = data.get("errorCode", -1)
            err_msg = data.get("errorMessage", "")
            success = bool(data.get("success")) or err_code in (0, 200)
            return {
                "character_id": character_id,
                "success": success,
                "message": _friendly(err_code, err_msg),
            }
    except asyncio.TimeoutError:
        return {"character_id": character_id, "success": False, "message": "Request timed out."}
    except Exception as e:
        return {"character_id": character_id, "success": False, "message": f"Error: {e}"}


async def redeem_many(character_ids: list[str], code: str) -> list[RedeemResult]:
    async with aiohttp.ClientSession() as session:
        return await asyncio.gather(*(redeem_one(session, cid, code) for cid in character_ids))
