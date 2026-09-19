# -*- coding: utf-8 -*-
"""航小麦 · 新闻推送插件 (模块4 · 麦麦侧)
=======================================
作用：检测"待推送"文件里有没有值得发的航天内容；有的话，按每个目标(私聊/各群)自己的
      节奏设置，以麦麦口吻主动推送（来源行 + 译文 + 配图 + 麦麦的一句评论）。

工作链路：
    外部主流程(模块5) 判定"该发" → 写 配置/待推送.json
    → 本插件 on_load 起后台 asyncio 任务，每 30 秒判断一次"现在该不该开口"
    → 该开口时用麦麦人设生成话术 → ctx.send 发出 → 标记 sent_to / 记已发记录
    → 所有目标都发过的条目才从队列移除

本文件只做"接线"：插件生命周期 + 私聊自然语言改配置的 Hook。
其余都在同目录模块里（都不依赖麦麦宿主，便于单测）：
    hx_paths.py     路径与内置默认值（含目录层级硬约定）
    hx_config.py    配置模型 + "生效设置"(内置默认→推送默认→个人/每群偏好) + ConfigMixin
    hx_state.py     推送节奏状态（推送状态.json）
    hx_store.py     待推送/留档/已发记录/功能开关 的文件读写
    hx_toml.py      TOML 读写（中文 key 加引号）
    hx_util.py      列表归一化、sent_to 标记、去重键
    hx_llm.py       直连模型 API（文案/翻译/解析）+ 读麦麦人设
    hx_render.py    来源行、正文、转发评论、图片 base64
    hx_send.py      SendMixin：stream_id 解析 + 底层发送
    hx_fallback.py  FallbackMixin：每日首推 / 留档旧闻 / 无内容闲聊
    hx_schedule.py  ScheduleMixin：受控节奏调度（本插件核心判断）
    hx_prefs.py     自然语言改推送设置/内容偏好

宿主能力依赖见 _manifest.json 的 capabilities（少一个就会在运行时拿不到对应 ctx）。
"""
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# 先把插件目录放进 sys.path：这样无论宿主是"按包加载"还是"按文件路径加载"，
# 同目录的 hx_* 兄弟模块都能 import 到（顶层模块名带 hx_ 前缀，避免与别家插件撞名）。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from maibot_sdk import HookHandler, MaiBotPlugin  # noqa: E402
from maibot_sdk.types import HookMode, HookOrder  # noqa: E402

if __package__ in (None, ""):   # 作为顶层模块加载（sys.path 上已有插件目录）
    from hx_config import ConfigMixin, HangxiaomaiNewsPluginConfig
    from hx_fallback import FallbackMixin
    from hx_paths import STATE_FILE
    from hx_prefs import apply_setting, build_setting_reply, looks_like_push_pref, parse_push_pref
    from hx_schedule import ScheduleMixin
    from hx_send import SendMixin
    from hx_state import save_field, save_fields
else:                            # 正常包内导入
    from .hx_config import ConfigMixin, HangxiaomaiNewsPluginConfig
    from .hx_fallback import FallbackMixin
    from .hx_paths import STATE_FILE
    from .hx_prefs import apply_setting, build_setting_reply, looks_like_push_pref, parse_push_pref
    from .hx_schedule import ScheduleMixin
    from .hx_send import SendMixin
    from .hx_state import save_field, save_fields


class HangxiaomaiNewsPlugin(ConfigMixin, SendMixin, FallbackMixin, ScheduleMixin, MaiBotPlugin):
    """航小麦新闻推送插件。

    Mixin 分工见模块 docstring；本类只管生命周期与 Hook。
    """

    config_model = HangxiaomaiNewsPluginConfig
    _task: asyncio.Task | None = None

    # ────────────────────────── 生命周期 ──────────────────────────

    async def on_load(self) -> None:
        """插件加载：若启用，起后台轮询任务；并把节奏状态归零（避免一开机就猛推）。"""
        if not self.config.plugin.enabled:
            self.ctx.logger.info("航小麦新闻推送插件未启用")
            return
        self.ctx.logger.info("航小麦新闻推送插件已启用，开始后台轮询待推送")
        try:
            now = time.time()
            # 开机/重载：把所有目标的"上次推送时间"重置为现在、清空"空内容"计数
            # 注意：每个群的节奏键是 last_push_ts_<kind>（kind=group_<群号>），
            #       必须按当前配置的目标逐个重置，否则重启后会立刻补发。
            fields = {
                "last_push_ts": now,
                "last_push_ts_private": now,
                "last_push_ts_group": now,      # 旧版遗留键，保留以防旧状态文件有残留
                "empty_cycles_private": 0,
            }
            for kind in self._configured_kinds():
                fields[f"last_push_ts_{kind}"] = now
                fields[f"empty_cycles_{kind}"] = 0
            save_fields(STATE_FILE, fields)
            # 把"上次是否活跃"初始化为当前状态，避免重启被误判为"刚进入活跃"而触发首推
            for kind in ("private", "group"):
                st = self._effective_settings_for(kind)
                save_field(STATE_FILE, f"prev_active_{kind}", int(self._is_active_hours(st)))
        except Exception as e:
            self.ctx.logger.warning(f"初始化推送状态失败: {e}")
        self._task = asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._send_reminder_if_pending())

    async def on_unload(self) -> None:
        if self._task:
            self._task.cancel()

    def _configured_kinds(self) -> list:
        """当前配置里所有推送目标的 kind：私聊 "private" + 每个群 "group_<群号>"。"""
        kinds = []
        if str(self.config.push.target_qq or "").strip():
            kinds.append("private")
        for gid in (self.config.push.target_groups or []):
            gid = str(gid).strip()
            if gid:
                kinds.append(f"group_{gid}")
        return kinds

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热重载事件（当前无需额外处理）。"""
        del scope
        del config_data
        del version

    # ────────────── 私聊里用自然语言改推送设置/内容偏好 ──────────────

    @HookHandler(
        "chat.receive.after_process",
        name="hangxiaomai_push_pref",
        description="识别并处理用户用自然语言设置自己的推送偏好",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_push_pref(self, **kwargs: Any) -> dict:
        """私聊里，若用户在设置推送偏好 → 解析、落盘、确认并拦截(abort)；否则放行(continue)。

        Hook 载荷的 message 结构（serialize_session_message）：
            processed_plain_text / session_id / platform /
            message_info{user_info{user_id}, group_info}
        """
        continue_result = {"action": "continue"}
        try:
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return continue_result
            text = str(message.get("processed_plain_text", "") or "").strip()
            if not text:
                return continue_result
            # 只在私聊：有 group_info 说明是群消息
            message_info = message.get("message_info") or {}
            if message_info.get("group_info"):
                return continue_result
            user_id = str((message_info.get("user_info") or {}).get("user_id", "") or "")
            if not user_id:
                return continue_result
            if not looks_like_push_pref(text):
                return continue_result

            # 会话 id（用于取上下文 + 回执/反问）
            sid = str(message.get("session_id") or "")
            if not sid:
                sid = await self._resolve_stream_id()

            # 取最近几条聊天当"前后文"，增强"是不是在说推送设置"的判断
            context = ""
            if sid:
                try:
                    recent = await self.ctx.message.get_recent(sid, limit=6)
                    if recent:
                        ctx_text = await self.ctx.message.build_readable(recent)
                        context = str(ctx_text or "")[-1500:]
                except Exception as e:
                    self.ctx.logger.debug(f"取上下文失败: {e}")

            parsed = await parse_push_pref(text, context=context, logger=self.ctx.logger)

            # 模型判断"在说内容/推送偏好但不确定改什么"→ 反问确认，不再静默
            if parsed.get("action") == "ask":
                hint = str(parsed.get("hint") or
                           "刚这句我没太懂，你是想改推送设置吗？可以举个例子，"
                           "比如'少发二次元''别发某某账号''可以发送的内容=发射,科普'～")
                if sid:
                    await self._send_text(sid, hint)
                return {"action": "abort"}

            sections = {k: parsed.get(k) for k in ("schedule", "scoring", "preference")
                        if parsed.get(k)}
            if parsed.get("action") != "set" or not sections:
                return continue_result

            changed = apply_setting(self, user_id, parsed)
            self.ctx.logger.info(f"已更新用户 {user_id} 推送配置: {changed}")
            reply = await build_setting_reply(parsed, logger=self.ctx.logger)
            if sid and reply:
                await self._send_text(sid, reply)
            return {"action": "abort"}
        except Exception as e:
            self.ctx.logger.warning(f"推送设置事件处理异常: {e}")
            return continue_result


def create_plugin() -> HangxiaomaiNewsPlugin:
    """创建航小麦新闻推送插件实例（宿主入口）。"""
    return HangxiaomaiNewsPlugin()
