"""OpenAI 兼容格式的大模型客户端（toklens / 腾讯 TokenHub / DeepSeek 共用一套代码）。

toklens（1024 超能 Coding 周末的算力伙伴，默认提供方）：
    base_url  https://lax.toklens.ai/v1
    模型       gpt- 开头（具体名单跑 list_models.py 查）
    鉴权       API Key（sk- 开头，Bearer）
    额度       参赛选手免费领，折合约 1000 元，兑换后 3 天内有效

背景（2026-10-23 核实）：
腾讯混元旧接口 hunyuan.tencentcloudapi.com 的旧模型已于 2026-06-22 全部下线，
官方迁移到 TokenHub（OpenAI 兼容）：
    base_url  https://tokenhub.tencentmaas.com/v1
    URL       https://tokenhub.tencentmaas.com/v1/chat/completions
    模型       hy3-preview（旧模型均映射到它）
    鉴权       TokenHub API Key（Bearer）
参考：https://cloud.tencent.com/document/product/1729/131925

DeepSeek 同样是 OpenAI 兼容：
    base_url  https://api.deepseek.com，模型 deepseek-chat

两家只差 base_url / model / api_key，所以共用同一个实现，用标准库 urllib 发请求。
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime

import config


class LLMError(Exception):
    """大模型调用失败，保留原有字符串消息。"""

    def __init__(self, message: str, *, code=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


_call_lock = threading.Lock()
_state_lock = threading.Lock()
_cooldown_until = 0.0
_DEFAULT_COOLDOWN = 60.0
_MAX_COOLDOWN = 120.0

# 限流（429）才做有界重试：最多 2 次、单次最多等 6 秒、累计最多等 12 秒。
# 目的是让偶发的共享限流不直接毁掉一次查证；每次重试都通过 on_retry 上报，不静默。
_MAX_429_RETRIES = 2
_MAX_TOTAL_RETRIES = 3
_RETRY_WAIT_CAP = 6.0
_RETRY_TOTAL_WAIT_BUDGET = 12.0
_MAX_FALLBACK_MODELS = 2
# 实测（2026-10-25）：限流是按模型分的，同一时刻 gpt-6.1-sol 全 429，
# 而 gpt-6-astra / gpt-5.6-sol 仍可正常返回。所以主模型不可用时降级到同提供方备用模型。
_TOKLENS_FALLBACK_DEFAULT = "gpt-6-astra,gpt-5.6-sol"


def fallback_models(primary: str):
    """同提供方内的备用模型链；只有 toklens 有实测可用的备选。

    可用 .env 的 LLM_FALLBACK_MODELS 覆盖（逗号分隔），留空则关闭降级。
    """
    provider = (config.LLM_PROVIDER or "toklens").lower()
    getter = getattr(config, "get", None)
    # 只有 toklens 有实测过的默认备选；其他提供方必须显式配置，避免瞎猜模型名。
    default = _TOKLENS_FALLBACK_DEFAULT if provider == "toklens" else ""
    raw = getter("LLM_FALLBACK_MODELS", default) if getter else default
    names = [name.strip() for name in str(raw or "").split(",")]
    return [name for name in names if name and name != primary][:_MAX_FALLBACK_MODELS]


def _admission_error():
    """调用方须持有状态锁；只读取准入状态。"""
    remaining = _cooldown_until - time.monotonic()
    if remaining > 0:
        seconds = math.ceil(remaining)
        return LLMError(
            f"大模型限流冷却中，请在 {seconds} 秒后再试。",
            code="rate_limited", retry_after=seconds,
        )
    if _call_lock.locked():
        return LLMError("大模型正在处理其他请求，请稍后再试。", code="busy")
    return None


def preflight() -> str | None:
    """只读检查进程内模型准入状态；可用时返回 None，否则返回提示。"""
    with _state_lock:
        error = _admission_error()
        return str(error) if error is not None else None


def _retry_after_seconds(headers):
    value = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError):
            return _DEFAULT_COOLDOWN
    if not math.isfinite(seconds) or seconds <= 0:
        return _DEFAULT_COOLDOWN
    return min(seconds, _MAX_COOLDOWN)


def resolve():
    """按 .env 的 LLM_PROVIDER 返回 (provider, api_key, model, base_url)。"""
    provider = (config.LLM_PROVIDER or "toklens").lower()
    if provider == "toklens":
        return (
            "toklens",
            config.TOKLENS_API_KEY,
            config.TOKLENS_MODEL,
            config.TOKLENS_BASE_URL,
        )
    if provider == "deepseek":
        return (
            "deepseek",
            config.DEEPSEEK_API_KEY,
            config.DEEPSEEK_MODEL,
            config.DEEPSEEK_BASE_URL,
        )
    if provider == "openai":
        return (
            "openai",
            config.OPENAI_API_KEY,
            config.OPENAI_MODEL,
            config.OPENAI_BASE_URL,
        )
    return (
        "tokenhub",
        config.TOKENHUB_API_KEY,
        config.TOKENHUB_MODEL,
        config.TOKENHUB_BASE_URL,
    )


_KEY_HINTS = {
    "toklens": "TOKLENS_API_KEY",
    "tokenhub": "TOKENHUB_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _read_body(error):
    """读取错误响应正文片段，读不出也不影响主流程。"""
    try:
        return error.read().decode("utf-8", errors="ignore")[:300]
    except Exception:
        return "错误响应正文无法读取"
    finally:
        try:
            error.close()
        except Exception:
            pass


def _content_of(data: dict, max_tokens: int) -> str:
    """从 OpenAI 兼容响应里取正文；取不到时给出能看懂的原因。"""
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("大模型返回格式异常：响应里没有 choices。", code="bad_format") from e

    content = message.get("content")
    # 有的网关把正文拆成 [{"type":"text","text":...}] 的分片数组。
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if isinstance(content, str) and content.strip():
        return content

    finish = str(choice.get("finish_reason") or "")
    usage = data.get("usage") or {}
    thinking = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if finish == "length":
        # 推理型模型会把预算先花在 thinking 上，正文没来得及写。
        extra = f"，其中思考用了 {thinking} tokens" if thinking else ""
        raise LLMError(
            f"大模型输出被截断：{max_tokens} tokens 用完了{extra}，正文为空。"
            "这是输出预算不足，不是内容有问题。",
            code="truncated",
        )
    raise LLMError(
        f"大模型返回空正文（finish_reason={finish or '未知'}），没有可核验的内容。",
        code="empty",
    )


def chat(system_prompt: str, user_prompt: str,
         api_key: str, model: str, base_url: str, timeout: int = 90,
         max_tokens: int = 1600, on_retry=None, on_fallback=None) -> str:
    """调用模型；429 做有界重试，主模型不可用时降级到同提供方备用模型。

    所有等待与降级都通过 on_retry(第几次, 等待秒数) / on_fallback(新模型, 原因) 上报，
    不静默重试、不跨提供方、不改变证据门槛。
    """
    if not api_key:
        provider, _, _, _ = resolve()
        hint_key = _KEY_HINTS.get(provider, "TOKLENS_API_KEY")
        raise LLMError(f"未配置 {hint_key}：请在 .env 里填，或切换 LLM_PROVIDER")

    global _cooldown_until
    with _state_lock:
        error = _admission_error()
        if error is not None:
            raise error
        if not _call_lock.acquire(blocking=False):
            raise LLMError("大模型正在处理其他请求，请稍后再试。", code="busy")

    attempted = 0
    waited = 0.0
    chain = [model] + fallback_models(model)
    # 总时限：主模型超时后仍有剩余时间才切备用模型，避免"降级把总时间翻倍"。
    deadline = time.monotonic() + max(1, timeout)
    try:
        data = None
        got_response = False
        last_error = None
        for index, current in enumerate(chain):
            payload = {
                "model": current,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "stream": False,
                "max_tokens": max_tokens,
            }
            req = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    # 部分网关在 Cloudflare 后面，会按 UA 拦截无头客户端（返回 403 / error code 1010）。
                    # 带上常见浏览器 UA 避免被误判为爬虫，这在纯服务端调用里是必需的。
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/141.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
                method="POST",
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    last_error = LLMError("大模型在总时限内没有返回结果，已停止等待。", code="timeout")
                    break
                try:
                    with urllib.request.urlopen(req, timeout=max(5, min(remaining, timeout))) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    got_response = True
                    if attempted or index:
                        # 重试/降级成功后清掉冷却，避免刚成功还被自己挡住。
                        with _state_lock:
                            _cooldown_until = 0.0
                    break
                except urllib.error.HTTPError as e:
                    # 400/404/5xx 多为"这个模型此刻不可用"，换链上的下一个模型比直接失败有用。
                    if e.code in (400, 404, 500, 502, 503, 504):
                        last_error = LLMError(
                            f"模型 {current} 暂时不可用（HTTP {e.code}）。", code=e.code)
                        break
                    if e.code != 429:
                        raise LLMError(
                            f"大模型调用失败 HTTP {e.code}：{_read_body(e)}", code=e.code
                        ) from e
                    retry_after = _retry_after_seconds(e.headers)
                    with _state_lock:
                        _cooldown_until = time.monotonic() + retry_after
                    body = _read_body(e)
                    wait = min(retry_after, _RETRY_WAIT_CAP)
                    if (attempted < _MAX_TOTAL_RETRIES
                            and waited + wait <= _RETRY_TOTAL_WAIT_BUDGET):
                        attempted += 1
                        waited += wait
                        if on_retry is not None:
                            try:
                                on_retry(attempted, wait)
                            except Exception:
                                pass  # 进度上报失败不能影响查证本身
                        time.sleep(wait)
                        with _state_lock:
                            _cooldown_until = 0.0
                        continue
                    last_error = LLMError(
                        f"大模型调用失败 HTTP 429：请求被限流，请在 {math.ceil(retry_after)} 秒后再试。{body}",
                        code="rate_limited", retry_after=retry_after,
                    )
                    break
                except Exception as e:  # 网络、超时等
                    left = deadline - time.monotonic()
                    has_next = index + 1 < len(chain)
                    if has_next and left > 8:
                        # 还有备用模型、也还有时间：记下原因，换下一个继续。
                        last_error = LLMError(
                            f"模型 {current} 请求超时或网络异常（{e}），已改用备用模型。",
                            code="timeout",
                        )
                        break
                    raise LLMError(f"大模型请求出错：{e}") from e
            if got_response:
                try:
                    return _content_of(data, max_tokens)
                except LLMError as err:
                    # 空正文 / 被截断 / 结构异常：都视为"这个模型这次没给出可用答案"。
                    # 有备用模型就继续降级，避免用户只看到一句莫名其妙的未完成。
                    last_error = err
                    got_response = False
            nxt = chain[index + 1] if index + 1 < len(chain) else None
            if nxt and on_fallback is not None:
                try:
                    on_fallback(nxt, str(last_error or "主模型不可用"))
                except Exception:
                    pass
        if not got_response:
            raise last_error or LLMError("大模型调用未完成。")
    finally:
        _call_lock.release()
