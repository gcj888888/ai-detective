"""元宝搜索（腾讯云 wsa）封装。

API 事实来自腾讯云官方文档（2026-10-24 查阅）：
https://cloud.tencent.com/document/product/1806/121811
- 请求域名 wsa.tencentcloudapi.com，Action=SearchPro，Version=2025-05-08
- 入参：Query 必选；Mode(0公开网页/1垂直信源/2混合)、Cnt(10~50)、
  Industry(gov/news/acad/finance)、Freshness(d7/m3/y2/日期)、Site(指定或排除网址) 可选
  注意：极速版不支持 Mode/Cnt/Industry/Freshness，所以默认不传，保兼容
- 出参：Pages 是"JSON 字符串"数组，每个元素含
  title/date/url/passage/content/site/score/authority_level/pics/favicon
  （authority_level 仅旗舰版返回；content 动态摘要极速版不返回）
- 常见错误码：ResourceNotFound=服务未开通；RequestLimitExceeded=频率限制；
  UnauthorizedOperation=未授权；InvalidParameter=参数错误
"""
import json
import math
from dataclasses import dataclass, asdict

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.wsa.v20250508 import wsa_client, models


class WsaSearchError(Exception):
    """元宝搜索调用失败（密钥缺失/未开通/频率限制等）。"""


@dataclass
class SearchResult:
    title: str = ""
    url: str = ""
    site: str = ""
    date: str = ""
    passage: str = ""
    content: str = ""      # 动态摘要（标准版及以上返回，信息通常比 passage 全）
    score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_pages(pages) -> list:
    """Pages 数组里每个元素是一段 JSON 字符串，要逐个解析。"""
    if pages is None:
        return []
    if not isinstance(pages, (list, tuple)):
        raise WsaSearchError("搜索响应格式异常：Pages不是结果列表。")
    results = []
    for raw in pages:
        try:
            item = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(item, dict):
                continue
            values = {name: item.get(name) or "" for name in ("title","url","site","date","passage","content")}
            if not all(isinstance(v,str) for v in values.values()):
                continue
            if not values["url"] or not any(values[k].strip() for k in ("title","passage","content")):
                continue
            try:
                score = float(item.get("score") or 0)
                if not math.isfinite(score):
                    score = 0.0
            except (TypeError, ValueError, OverflowError):
                score = 0.0
            values["passage"] = (values["passage"] or values["content"])[:6000]
            values["content"] = values["content"][:6000]
            results.append(SearchResult(**values,score=score))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    if pages and not results:
        raise WsaSearchError("搜索响应格式异常：返回结果均无法解析，不代表未查到资料。")
    return results


def search(query: str, secret_id: str, secret_key: str, mode: str = "", cnt: str = "",
           industry: str = "", freshness: str = "", site: str = "", timeout: float = 12):
    """调用元宝搜索 SearchPro。

    返回 (results: list[SearchResult], version: str)
    version 是 lite/standard/flagship，由服务端返回。

    可选参数（官方文档 https://cloud.tencent.com/document/product/1806/121811 ）：
      industry  垂直领域，枚举 gov/news/acad/finance（极速版不支持）
      freshness 时效范围，如 d7/m3/y2/2026-08-20/2026-08-20,2026-08-30（极速版不支持）
      site      指定网址搜索 "qq.com"，或排除 "exclude:qq.com|sohu.com"（最多5个域名）
    """
    if not secret_id or not secret_key:
        raise WsaSearchError(
            "未配置腾讯云密钥：请在 .env 里填 TENCENT_SECRET_ID / TENCENT_SECRET_KEY"
        )

    cred = credential.Credential(secret_id, secret_key)
    http_profile = HttpProfile()
    http_profile.endpoint = "wsa.tencentcloudapi.com"
    http_profile.reqTimeout = max(1, int(timeout))
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    client = wsa_client.WsaClient(cred, "", client_profile)

    params = {"Query": query}
    # Mode/Cnt 极速版不支持，默认不传；用户在 .env 显式配置了才传
    if mode:
        params["Mode"] = int(mode)
    if cnt:
        params["Cnt"] = int(cnt)
    if industry:
        params["Industry"] = industry
    if freshness:
        params["Freshness"] = freshness
    if site:
        params["Site"] = site

    req = models.SearchProRequest()
    req.from_json_string(json.dumps(params))
    try:
        resp = client.SearchPro(req)
    except TencentCloudSDKException as e:
        raise WsaSearchError(_friendly_error(e)) from e

    results = _parse_pages(resp.Pages)
    return results, (resp.Version or "")


def _friendly_error(e: TencentCloudSDKException) -> str:
    """把腾讯云错误码翻译成高中生能看懂的人话。"""
    code = str(getattr(e, "code", ""))
    table = {
        "ResourceNotFound": "元宝搜索服务还没开通（控制台 → 元宝搜索 → 开通服务）",
        "ResourceUnavailable": "元宝搜索资源不可用（可能没购买/额度问题，去控制台看看）",
        "RequestLimitExceeded": "搜索太频繁被限流了，等几秒再试",
        "UnauthorizedOperation": "密钥没有权限（确认账号已实名、密钥填对）",
        "InvalidParameter": "搜索参数不对（检查 .env 里的 WSA_MODE / WSA_CNT）",
    }
    hint = table.get(code)
    return f"元宝搜索调用失败[{code}]：{hint or e}"
