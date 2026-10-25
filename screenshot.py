# -*- coding: utf-8 -*-
"""截图查证：从一张截图里认出「待查证的说法」，再交给查证流程。

为什么需要它（官方进阶挑战第二条）：
  "截图查证（丢一张截图或一段视频里的说法，先认出再去查）"
  家族群里转发的谣言，几乎都是截图形式——一张图里一段话，加个"紧急通知"。
  用户不会手打一遍，直接丢图才是真实场景。

两步：
  1. 本地 OCR（rapidocr-onnxruntime，纯 pip 安装、不依赖系统级 Tesseract）
     把图里的文字抠出来。**不上传图片到任何外部服务**，隐私敏感内容不出本机。
  2. 大模型把 OCR 的零散文字整理成一句「可以拿去查证的完整说法」。
     OCR 出来的往往是被截断的断行（"每天喝八杯水" / "能排毒养颜"），
     直接拿去搜是搜不准的，必须先拼成人话。

设计上刻意保守：
  - OCR 结果必须有足够字数才认为"图里有可查证内容"（避免把表情包、风景照当谣言）
  - 提炼失败就退回原始 OCR 文本，让用户自己改——流程不能因此中断
  - 大模型只负责"整理文字"，不负责判断真假；判断真假永远走统一流程
"""

import os
import tempfile

_engine = None          # OCR 引擎单例：加载模型要 2-3 秒，绝不能每个请求重建


def _get_engine():
    """懒加载 OCR 引擎。第一次调用会慢，之后复用。

    部署环境装不上 onnxruntime 时，这里会抛 ImportError——
    上层转成"截图查证暂不可用"的提示，不影响文字查证与深度调查。
    """
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(
                "这台机器没装 OCR 引擎（rapidocr-onnxruntime），截图查证暂不可用；"
                "文字查证和深度调查不受影响") from e
        _engine = RapidOCR()
    return _engine


def ocr_image(data: bytes) -> dict:
    """对图片字节做 OCR，返回 {"text": 全文, "lines": [逐行], "elapse": 秒}。

    RapidOCR 用文件路径最稳（它内部还要做图像解码），所以先落临时文件。
    """
    if not data:
        raise ValueError("图片是空的")
    suffix = ".png"
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        engine = _get_engine()
        result, elapse = engine(path)
        lines = []
        for item in (result or []):
            # item = [box, text, score]
            if len(item) >= 2 and item[1]:
                lines.append(str(item[1]).strip())
        cost = 0.0
        try:
            cost = float(sum(elapse)) if isinstance(elapse, (list, tuple)) else float(elapse)
        except Exception:
            cost = 0.0
        return {"text": "\n".join(lines), "lines": lines, "elapse": round(cost, 2)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


MIN_CHARS = 6      # 少于这些字，认为图里没有可查证的说法


def extract_claim(ocr_text: str, api_key: str, model: str, base_url: str):
    """把 OCR 出来的零散文字整理成一句「待查证说法」。

    返回 (claim, err)：
      claim 为整理出的说法（失败时退回 OCR 原文）
      err 不为空表示提炼失败，但 claim 仍然可用
    """
    from llm_openai import LLMError, chat
    import prompts

    raw = (ocr_text or "").strip()
    if len(raw) < MIN_CHARS:
        return "", "这张图里没认出足够的文字（可能不是文字截图，或者太模糊）"

    try:
        out = chat(
            "你是信息整理助手，只输出整理后的说法，不要评论。",
            prompts.EXTRACT_CLAIM_PROMPT.format(ocr=raw[:1500]),
            api_key=api_key, model=model, base_url=base_url, timeout=60,
        )
        out = (out or "").strip().strip("`").strip()
        # 模型偶尔会带前缀，剥掉
        for prefix in ("说法：", "说法:", "待查证说法：", "待查证说法:"):
            if out.startswith(prefix):
                out = out[len(prefix):].strip()
        # 一整段太长的（模型没压缩），取第一行
        if len(out) > 120 and "\n" in out:
            out = out.splitlines()[0].strip()
        if out == "无法识别":
            return out, ""
        if len(out) >= MIN_CHARS:
            return out, ""
        return raw[:200], "提炼失败，已改用图里的原文"
    except LLMError as e:
        return raw[:200], "提炼失败（%s），已改用图里的原文" % str(e)[:60]
    except Exception as e:
        return raw[:200], "提炼失败（%s），已改用图里的原文" % type(e).__name__


MAX_BYTES = 8 * 1024 * 1024     # 单张图上限 8MB，再大前端会先压缩


def check_size(data: bytes) -> str:
    """返回错误说明，None 表示大小正常。"""
    if not data:
        return "没有收到图片内容"
    if len(data) > MAX_BYTES:
        return "图片太大（%.1f MB），请压缩到 8MB 以内" % (len(data) / 1024 / 1024)
    return None
