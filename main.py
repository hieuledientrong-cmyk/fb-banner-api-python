import os
import time
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx

app = FastAPI()

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

FREE_2K_DAILY_LIMIT = int(os.getenv("FREE_2K_DAILY_LIMIT", "3"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "10"))

def get_client_ip(request: Request) -> str:
    xf = request.headers.get("x-forwarded-for")
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"

async def redis_call(cmd: str, *args):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        raise RuntimeError("Missing Upstash env vars")
    url = f"{UPSTASH_REDIS_REST_URL}/{cmd}/" + "/".join([httpx.utils.quote(str(a), safe="") for a in args])
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"})
        r.raise_for_status()
        return r.json()

async def incr_with_expire(key: str, ttl_sec: int) -> int:
    data = await redis_call("INCR", key)
    val = int(data.get("result", 0))
    if val == 1:
        await redis_call("EXPIRE", key, ttl_sec)
    return val

async def set_cooldown(key: str, ttl_sec: int) -> bool:
    # SET key "1" NX EX ttl
    data = await redis_call("SET", key, "1", "NX", "EX", ttl_sec)
    return data.get("result") == "OK"

def ymd_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def ymdhm_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")

@app.post("/api/free2k")
async def free2k_gate(
    request: Request,
    productImage: UploadFile = File(...),
    # các field text (tối thiểu)
    title: str = Form(...),
    aspectRatio: str = Form("4:5"),
    outputCount: int = Form(1),
):
    ip = get_client_ip(request)

    # 1) cooldown
    cd_ok = await set_cooldown(f"cd:{ip}", COOLDOWN_SECONDS)
    if not cd_ok:
        raise HTTPException(status_code=429, detail="Bạn thao tác quá nhanh. Vui lòng thử lại sau vài giây.")

    # 2) rate limit / phút
    rl_key = f"rl:min:{ip}:{ymdhm_utc()}"
    rl = await incr_with_expire(rl_key, 70)
    if rl > RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="Bạn gửi quá nhiều yêu cầu. Vui lòng thử lại sau.")

    # 3) quota / ngày
    q_key = f"quota:2k:{ip}:{ymd_utc()}"
    used = await incr_with_expire(q_key, 26 * 3600)
    if used > FREE_2K_DAILY_LIMIT:
        return JSONResponse(
            status_code=429,
            content={"error": "Hết lượt 2K miễn phí hôm nay. Vui lòng quay lại ngày mai hoặc nâng cấp.", "remaining_today": 0},
        )

    # Giới hạn Free 2K: max 2 ảnh
    outputCount = max(1, min(int(outputCount), 2))

    # TODO: (Bước tiếp theo) Gọi Gemini 3 Flash để tạo ảnh 2K
    # - build prompt FULL LOCK
    # - send productImage + options
    # - nhận ảnh bytes -> upload storage -> trả url

    remaining = max(0, FREE_2K_DAILY_LIMIT - used)
    return {"ok": True, "tier": "free_2k", "used_today": used, "remaining_today": remaining,
            "note": "Gate OK. Bước tiếp theo: generate ảnh + upload storage + trả link."}
