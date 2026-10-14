"""自然语言调整渠道推送时段、频率和图片设置；内容判定统一由完整分析完成。"""
import json
import re
if __package__ in (None, ""):
    from hx_llm import llm_text
    from hx_paths import BUILTIN_PUSH_DEFAULTS
    from hx_toml import write_toml
    from hx_util import as_bool
else:
    from .hx_llm import llm_text
    from .hx_paths import BUILTIN_PUSH_DEFAULTS
    from .hx_toml import write_toml
    from .hx_util import as_bool
SETTING_TOKENS=["推送","发新闻","别推","时段","几点","每次","每隔","频率","带图","几条","一次","每天"]
SCHEDULE_KEYS=set(BUILTIN_PUSH_DEFAULTS)

def looks_like_push_pref(text):
    return any(token in text for token in SETTING_TOKENS)

async def parse_push_pref(text,context="",logger=None):
    prompt=('请解析用户的推送方式设置，仅返回JSON：{"action":"set|none|ask","schedule":{},"hint":""}。'
            'schedule仅允许新闻推送(bool)、活跃时段(HH:MM-HH:MM)、间隔最小分钟、间隔最大分钟、一次发几条、带图(bool)、一天最多发几次。'
            '只解析实际提出的设置；不处理内容评分、白名单、偏好或排除。\n上下文：'+context[-1200:]+'\n用户：'+text)
    raw=llm_text(prompt,temperature=0.0,logger=logger).strip()
    try: data=json.loads(raw)
    except (ValueError,TypeError):return {'action':'none','schedule':{}}
    if not isinstance(data,dict):return {'action':'none','schedule':{}}
    return {'action':data.get('action','none'),'schedule':data.get('schedule') if isinstance(data.get('schedule'),dict) else {},'hint':str(data.get('hint') or '')}

def apply_setting(cfg,user_id,parsed):
    fields=parsed.get('schedule') or {}
    if not fields:return {}
    apply_schedule(cfg,user_id,fields)
    return {'推送方式':list(fields)}

def apply_schedule(cfg, user_id: str, fields: dict) -> None:
    """写 推送偏好_<QQ>.toml 的 [push]（带校验，防非法值落盘）。"""
    path = cfg._user_pref_path(user_id)
    new = dict(cfg._read_toml(path).get("push", {}))
    for k, v in fields.items():
        if k not in SCHEDULE_KEYS or v is None or v == "":
            continue
        if k == "活跃时段":
            w = str(v).strip()
            if not re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", w):
                continue  # 格式不对，不写
            s_, e_ = w.split("-")
            if s_ == e_:
                continue  # 零点/畸形窗，跳过
            new[k] = w
        elif k in ("间隔最小分钟", "间隔最大分钟", "一次发几条", "一天最多发几次"):
            try:
                n = int(v)
                new[k] = max(0, n)
            except Exception:
                continue
        elif k in ("新闻推送", "带图"):
            new[k] = as_bool(v)
    write_toml(path, {"push": new})


async def build_setting_reply(parsed,logger=None):
    fields=parsed.get('schedule') or {}
    if not fields:return ''
    return '推送设置已更新：'+'、'.join(f'{k}={v}' for k,v in fields.items())
