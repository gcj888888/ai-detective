"""读取 .env 配置。

为了少装一个依赖（python-dotenv），这里手写解析 .env 文件。
规则：每行 KEY=VALUE，# 开头是注释，值可以去掉的引号去掉。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _load_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    # utf-8-sig：兼容 Windows 记事本保存时可能带的 BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


_file_env = _load_env_file(ENV_PATH)


def get(key: str, default: str = "") -> str:
    """环境变量优先，其次 .env 文件，最后默认值。"""
    return os.environ.get(key) or _file_env.get(key, default)


# —— 腾讯云密钥（元宝搜索和混元共用）——
TENCENT_SECRET_ID = get("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = get("TENCENT_SECRET_KEY")

# —— 元宝搜索参数（默认不传 Mode/Cnt，兼容极速版）——
WSA_MODE = get("WSA_MODE")            # 留空=不传；2=混合结果
WSA_CNT = get("WSA_CNT")              # 留空=不传；10/20/30/40/50

# —— 大模型（统一走 OpenAI 兼容接口）——
# 提供方：toklens（1024 黑客松算力伙伴，默认）/ tokenhub（腾讯）/ deepseek（备选）
# 为什么默认 toklens：本次赛事官方算力伙伴，参赛选手免费领约 1000 元额度
# 原因（tokenhub 相关）：混元旧接口 hunyuan.tencentcloudapi.com 的旧模型已于 2026-06-22 全部下线
LLM_PROVIDER = get("LLM_PROVIDER", "toklens").lower()

# toklens（1024 超能 Coding 周末算力伙伴）
# 控制台 https://toklens.ai 左侧「API Key」页可查看 / 复制 Key
TOKLENS_API_KEY = get("TOKLENS_API_KEY")
TOKLENS_MODEL = get("TOKLENS_MODEL", "gpt-5")
TOKLENS_BASE_URL = get("TOKLENS_BASE_URL", "https://lax.toklens.ai/v1")

# 腾讯 TokenHub
TOKENHUB_API_KEY = get("TOKENHUB_API_KEY")
TOKENHUB_MODEL = get("TOKENHUB_MODEL", "hy3-preview")
TOKENHUB_BASE_URL = get("TOKENHUB_BASE_URL", "https://tokenhub.tencentmaas.com/v1")

# DeepSeek（备选）
DEEPSEEK_API_KEY = get("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 任意 OpenAI 兼容网关（备选里最通用的一路）
# 只要对方提供 /chat/completions 就能接：把 base_url 指过去、填 key 和模型名即可。
OPENAI_API_KEY = get("OPENAI_API_KEY")
OPENAI_MODEL = get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# 单次判断的输出预算。推理型模型（如 deepseek-v4.1-flash）会先花 token 思考，
# 预算给小了正文会是空的，所以默认留足。
try:
    LLM_MAX_TOKENS = int(get("LLM_MAX_TOKENS", "2600"))
except ValueError:
    LLM_MAX_TOKENS = 2600

# —— 演示开关：没有密钥时用假数据跑通全流程 ——
USE_MOCK = get("USE_MOCK", "0") == "1"

# —— 服务端口 ——
try:
    PORT = int(get("PORT", "8000"))
except ValueError:
    PORT = 8000
