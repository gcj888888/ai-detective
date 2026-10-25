"""AI 侦探社 · 服务入口（FastAPI）。

路由：
  GET  /            前端页面（static/index.html）
  GET  /api/status  服务状态（是否配了密钥、是否演示模式）——自己排查用
  POST /api/check   查证一条说法，返回查证卡数据
  POST /api/investigate  深度调查，返回带证据链的调查报告

部署说明：
  - 监听地址由 IS_PUBLIC 决定：公网部署时绑 0.0.0.0 并读 PORT 环境变量，
    本地跑还是 127.0.0.1（避免把服务暴露给局域网）
  - 公网部署时开启每日查询限额，保护搜索额度不被单个用户烧光
"""
import os
import threading
import time
import webbrowser
from collections import defaultdict

import config
import detective
import guard
import investigate as inv
import llm_openai
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import json
import queue
from pydantic import BaseModel

app = FastAPI(title="AI 侦探社 · 查证卡")

# 是否公网部署：平台会注入 PORT 环境变量，本地 start.bat 不会设
IS_PUBLIC = bool(os.environ.get("PORT"))

# 每日查询限额（仅公网部署生效）。
# 快速查证烧 1-2 次搜索额度，深度调查烧 3-5 次，所以后者限得更紧。
# 截图查证只烧大模型 token（OCR 在本机跑），限得宽松些。
DAILY_LIMIT = {"check": 20, "investigate": 5, "screenshot": 15}
_usage = defaultdict(lambda: defaultdict(int))
_public_total = defaultdict(int)
_quota_lock = threading.Lock()
# 同步和流式查证/调查共享进程内名额；多个服务进程各自计数。
_inflight_slots = threading.BoundedSemaphore(2)
PUBLIC_DAILY_TOTAL = 100  # 全站每日请求上限；内存计数，进程重启后清零


def _check_quota(request: Request, kind: str):
    """返回 None 表示放行，否则返回 429 响应。"""
    if not IS_PUBLIC:
        return None
    ip = request.client.host if request.client else "unknown"
    today = time.strftime("%Y-%m-%d")
    key = (today, kind)
    limit = DAILY_LIMIT.get(kind, 10)
    with _quota_lock:
        used = _usage[ip][key]
        total = _public_total[today]
        if used >= limit or total >= PUBLIC_DAILY_TOTAL:
            return JSONResponse(status_code=429, content={
                "error": "今天的试用额度已用完，请明天再试。演示版设有每类查询和全站总量限制。",
            })
        _usage[ip][key] = used + 1
        _public_total[today] = total + 1
    return None


class CheckRequest(BaseModel):
    query: str


class ShotRequest(BaseModel):
    image: str      # data URL（data:image/png;base64,...）或纯 base64


def _b64_to_bytes(s: str) -> bytes:
    """把 data URL / 纯 base64 统一解码成字节。"""
    import base64
    s = (s or "").strip()
    if s.startswith("data:"):
        _, _, s = s.partition(",")
    return base64.b64decode(s, validate=False)


def _validate(query: str):
    """空值与长度校验，返回 (ok, error_response)。"""
    q = (query or "").strip()
    if not q:
        return None, JSONResponse(status_code=400, content={"error": "请输入要查证的说法"})
    if len(q) > 200:
        return None, JSONResponse(status_code=400, content={"error": "说法太长啦，200 字以内"})
    return q, None


def _guard(q: str):
    """赛事规则边界：不碰时政和涉及个人的消息（命中则不调用任何 API）。"""
    notice = guard.check_query(q)
    if notice:
        return {"blocked": True, "notice": notice, "query": q}
    return None


def _admit(request, kind):
    if not _inflight_slots.acquire(blocking=False):
        return JSONResponse(status_code=503, content={
            "error": "查证服务繁忙，已有两项查证或调查正在处理，请稍后再试。",
        })
    try:
        limited = _check_quota(request, kind)
    except BaseException:
        _inflight_slots.release()
        raise
    if limited:
        _inflight_slots.release()
    return limited


def _incomplete_report(query, deep=False):
    report = detective.unavailable_report(query, "查证服务异常，本次调查未完成，请稍后再试。")
    report["detail"] = "处理过程中发生异常，无法确认本次调查的完整结果；请稍后重新查证。"
    if deep:
        report.update(mode="investigate", rounds=[], steps=[], duplicates=[], domains=[], domain_summary=[])
    return report


@app.get("/")
def index():
    # HTML 每次向服务器确认，避免旧页面被浏览器缓存后看不到新修复。
    return FileResponse(
        config.BASE_DIR / "static" / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/status")
def status():
    _, _, model, _ = llm_openai.resolve()
    return {
        "mock": config.USE_MOCK,
        "has_keys": bool(config.TENCENT_SECRET_ID and config.TENCENT_SECRET_KEY),
        "model": model,
        "provider": config.LLM_PROVIDER,
        "port": config.PORT,
        "runtime_version": detective.RUNTIME_VERSION,
        "verdicts": list(detective.VERDICTS),
    }


@app.get("/api/authority")
def authority_info():
    """信源库概况——供作品说明和界面展示"我们怎么判断来源可信度"。"""
    import authority
    return {
        "tiers": {
            "primary": authority.TIER_LABEL[authority.TIER_PRIMARY],
            "secondary": authority.TIER_LABEL[authority.TIER_SECONDARY],
            "tertiary": authority.TIER_LABEL[authority.TIER_TERTIARY],
            "unknown": authority.TIER_LABEL[authority.TIER_UNKNOWN],
            "farm": authority.TIER_LABEL[authority.TIER_FARM],
        },
        "scores": authority.TIER_SCORE,
        "exclude_filter": authority.build_exclude_filter(),
        "farm_domains": sorted(authority.FARM_DOMAINS),
        "domains": authority.domain_summary(list(authority.DOMAIN_SOURCE_MAP.keys())),
        "note": "标准版不返回 authority_level 字段（旗舰版独有），故按域名分层自建",
    }


@app.post("/api/check")
def api_check(req: CheckRequest, request: Request):
    """快速查证：元宝搜索与三态证据核对，网络失败标注未完成。"""
    query, err = _validate(req.query)
    if err:
        return err
    blocked = _guard(query)
    if blocked:
        return blocked
    limited = _admit(request, "check")
    if limited:
        return limited
    try:
        return detective.check(query)
    except Exception:
        return _incomplete_report(query)
    finally:
        _inflight_slots.release()


@app.post("/api/screenshot")
def api_screenshot(req: ShotRequest, request: Request):
    """截图查证第一步：认出图里的说法（不判断真假）。

    官方进阶挑战第二条："丢一张截图或一段视频里的说法，先认出再去查"。
    这里只做识别，识别出来的说法交给前端填进输入框，用户确认后再走 /api/check
    或 /api/investigate —— 识别和判断分开，用户始终看得见"要查的到底是什么"。
    """
    limited = _check_quota(request, "screenshot")
    if limited:
        return limited
    import screenshot

    try:
        data = _b64_to_bytes(req.image)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "图片格式不对，请重新上传"})

    size_err = screenshot.check_size(data)
    if size_err:
        return JSONResponse(status_code=400, content={"error": size_err})

    try:
        ocr = screenshot.ocr_image(data)
    except Exception as e:
        return JSONResponse(status_code=422, content={
            "error": "这张图读不出来（%s）——可能不是有效图片，也可以直接手打说法查证"
                     % type(e).__name__,
        })

    if not (ocr["text"] or "").strip():
        return JSONResponse(status_code=422, content={
            "error": "这张图里没认出文字。可能是纯图片、太模糊，或者文字太小——"
                     "可以直接手打说法查证",
            "ocr_lines": [],
        })

    _, api_key, model, base_url = llm_openai.resolve()
    claim, warn = screenshot.extract_claim(ocr["text"], api_key, model, base_url)

    if claim == "无法识别":
        return JSONResponse(status_code=422, content={
            "error": "图里的文字看起来不像一条可以查证的说法（可能是表情包或聊天）",
            "ocr_text": ocr["text"],
            "ocr_lines": ocr["lines"],
        })

    return {
        "claim": claim,
        "warn": warn or None,
        "ocr_text": ocr["text"],
        "ocr_lines": ocr["lines"],
        "elapse": ocr["elapse"],
    }


@app.post("/api/investigate")
def api_investigate(req: CheckRequest, request: Request):
    """深度调查：多角度元宝检索、疑似重复和引文核对；耗时以实测为准。"""
    query, err = _validate(req.query)
    if err:
        return err
    blocked = _guard(query)
    if blocked:
        return blocked
    limited = _admit(request, "investigate")
    if limited:
        return limited
    try:
        return inv.investigate(query)
    except Exception:
        return _incomplete_report(query, deep=True)
    finally:
        _inflight_slots.release()


def _stream_check(req, request, deep=False):
    query, err = _validate(req.query)
    if err:
        return err
    blocked = _guard(query)
    if blocked:
        return JSONResponse(blocked)
    limited = _admit(request, "investigate" if deep else "check")
    if limited:
        return limited
    try:
        events = queue.Queue()

        def run():
            try:
                result = (inv.investigate if deep else detective.check)(query, progress=events.put)
                events.put({"type": "result", "result": result})
            except Exception:
                events.put({"type": "result", "result": _incomplete_report(query, deep)})
            finally:
                # 客户端断连后仍可能继续处理；不取消外部请求，名额随实际任务结束释放。
                _inflight_slots.release()
                events.put(None)

        def lines():
            while True:
                try:
                    event = events.get(timeout=1)
                except queue.Empty:
                    yield json.dumps({"type": "heartbeat"}) + "\n"
                    continue
                if event is None:
                    break
                yield json.dumps(event, ensure_ascii=False) + "\n"

        response = StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        threading.Thread(target=run, daemon=True).start()
    except BaseException as exc:
        _inflight_slots.release()
        if not isinstance(exc, Exception):
            raise
        return JSONResponse(_incomplete_report(query, deep))
    return response


@app.post("/api/check/stream")
def api_check_stream(req: CheckRequest, request: Request):
    return _stream_check(req, request)


@app.post("/api/investigate/stream")
def api_investigate_stream(req: CheckRequest, request: Request):
    return _stream_check(req, request, deep=True)


@app.on_event("startup")
def _open_browser():
    """本地跑时服务起来 1 秒后自动打开浏览器，省得用户手动输地址。
    公网部署（有 PORT 环境变量）时没有显示器，跳过。"""
    if IS_PUBLIC:
        return

    def _open():
        try:
            webbrowser.open(f"http://127.0.0.1:{config.PORT}")
        except Exception:
            pass
    threading.Timer(1.2, _open).start()


if __name__ == "__main__":
    import uvicorn

    host = "0.0.0.0" if IS_PUBLIC else "127.0.0.1"
    port = int(os.environ.get("PORT", config.PORT))

    print("=" * 46)
    print("  AI 侦探社 · 查证卡")
    if config.USE_MOCK:
        print("  当前是【演示模式】：使用假数据，未调用真实 API")
    elif not (config.TENCENT_SECRET_ID and config.TENCENT_SECRET_KEY):
        print("  警告：.env 里没有密钥，查询会失败；想看界面请设 USE_MOCK=1")
    if IS_PUBLIC:
        print("  公网部署模式：已开启每日查询限额（查证 %d 次 / 调查 %d 次）"
              % (DAILY_LIMIT["check"], DAILY_LIMIT["investigate"]))
        print(f"  监听：{host}:{port}")
    else:
        print(f"  浏览器访问：http://127.0.0.1:{port}")
        print("  停止：按 Ctrl+C")
    print("=" * 46)
    uvicorn.run(app, host=host, port=port)
