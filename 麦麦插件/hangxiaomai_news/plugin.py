# -*- coding: utf-8 -*-
"""
航小麦 · 新闻推送插件 (模块4)
================================
作用：检测"待推送"文件里有没有值得发的航天内容，若有，
       以麦麦口吻调用宿主 LLM 生成一句话，主动发送到指定 QQ 私聊。

工作原理：
  - 外部主流程(模块5) 判定"该发"的内容 → 写入 配置/待推送.json
  - 本插件 on_load 起后台 asyncio 任务，每 N 秒检测该文件
  - 检测到且"新闻推送"开关开 → 拿目标 stream_id → ctx.llm.generate生成口吻 → ctx.send.text 发出
  - 发出后从待推送列表移除该条

推送方式（时段/间隔/条数/带图/每日上限）现在由 配置/推送默认.toml（管理员基线）
+ 配置/推送偏好_<QQ>.toml（用户个人，覆盖默认）决定，见 _effective_settings()。
用户可在私聊用自然语言改自己的偏好，见 handle_push_pref(HookHandler, chat.receive.after_process)。

配置：见 hangxiaomai_news/config.toml（enabled / 目标QQ / 开关 / 轮询间隔）
"""

import asyncio
import base64
import json
import os
import random
import re
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # 麦麦项目根(MaiBot-main)
HX_ROOT = PROJECT_ROOT.parent  # E:\AI程序员\03_实用程序\航小Q\航小麦
PENDING_FILE = HX_ROOT / "配置" / "待推送.json"
SWITCH_FILE = HX_ROOT / "配置" / "功能开关.toml"
PUSH_DEFAULT_FILE = HX_ROOT / "配置" / "推送默认.toml"
# 推送群列表改由插件配置 [push].target_groups 决定（默认 ["YOUR_GROUP_ID_1"]；可加多个群）
MAIBOT_CONFIG = PROJECT_ROOT / "config" / "bot_config.toml"  # 麦麦人设源
ARCHIVE_FILE = HX_ROOT / "配置" / "内容留档.json"  # 每天前10之外的内容，无新消息时拿来填充
SENT_EVER_FILE = HX_ROOT / "配置" / "已发记录.json"  # 已推送过的内容，去重用（永不重推）
REMINDER_FILE = HX_ROOT / "配置" / "提醒待发.txt"  # 存在则插件启动时给用户发一条提醒并删除

# 直连 DeepSeek（用于"在 Hook 里解析/生成确认"，避免宿主 cap.call 30s 限额）
load_dotenv(HX_ROOT / "抓取脚本" / ".env", encoding="utf-8")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "").strip()
DEEPSEEK_BASE = "https://api.deepseek.com/v1"

# 内置默认（推送默认.toml 缺失时兜底；/推送默认.toml 与此保持一致）
BUILTIN_PUSH_DEFAULTS: dict = {
    "新闻推送": True,
    "活跃时段": "09:00-21:00",
    "间隔最小分钟": 20,
    "间隔最大分钟": 40,
    "一次发几条": 1,
    "带图": True,
    "一天最多发几次": 0,
    "闲聊": True,
}

# 单条消息最多带几张图（QQ 限制）；超过则另凑一条消息
MAX_IMG_PER_MSG: int = 9


class InitConfig(PluginConfigBase):
    """插件基础配置。"""
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用新闻推送插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class PushConfig(PluginConfigBase):
    """推送配置。"""
    __ui_label__ = "推送"
    __ui_order__ = 1

    target_qq: str = Field(default="YOUR_QQ_ID", description="推送到哪个QQ私聊")
    target_groups: list[str] = Field(default_factory=lambda: ["YOUR_GROUP_ID_1"], description="推送到哪些QQ群(群号列表)")
    poll_interval: int = Field(default=30, description="检测待推送文件的间隔(秒)")
    auto_switch: bool = Field(default=True, description="是否遵守功能开关(新闻推送 off 则不推)")


class HangxiaomaiNewsPluginConfig(PluginConfigBase):
    plugin: InitConfig = Field(default_factory=InitConfig)
    push: PushConfig = Field(default_factory=PushConfig)


def _load_switch_state() -> bool:
    """读功能开关.toml 的"新闻推送"，默认 true。"""
    try:
        if not SWITCH_FILE.exists():
            return True
        txt = SWITCH_FILE.read_text(encoding="utf-8")
        # "新闻推送 = true/false"
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("新闻推送") and "=" in line:
                val = line.split("=", 1)[1].strip().lower()
                return val == "true"
        return True
    except Exception:
        return True


def _load_pending() -> list:
    """读待推送.json，返回新闻列表。"""
    try:
        if not PENDING_FILE.exists():
            return []
        return json.loads(PENDING_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return []


def _save_pending(items: list) -> None:
    """把剩余待推送写回。"""
    try:
        PENDING_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_pending_file(path: Path, default):
    """读一个 json 文件，失败返回 default。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def _save_pending_file(path: Path, data) -> None:
    """写一个 json 文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _to_list(v) -> list:
    """把值转成字符串列表（兼容 list/逗号字符串）。"""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return []


def _add_words(dct: dict, key: str, words: list) -> None:
    """往 dct[key] 列表追加去重后的词。"""
    cur = [str(x) for x in (dct.get(key, []) or [])]
    for w in words:
        w = str(w).strip()
        if w and w not in cur:
            cur.append(w)
    dct[key] = cur


class HangxiaomaiNewsPlugin(MaiBotPlugin):
    """航小麦新闻推送插件。"""

    config_model = HangxiaomaiNewsPluginConfig
    _task: asyncio.Task | None = None

    async def on_load(self) -> None:
        """插件加载：若启用，起后台轮询任务。"""
        if not self.config.plugin.enabled:
            self.ctx.logger.info("航小麦新闻推送插件未启用")
            return
        self.ctx.logger.info("航小麦新闻推送插件已启用，开始后台轮询待推送")
        # 开机/重载：把"上次推送时间"重置为现在、清空"空内容"计数，避免一开机就猛推
        try:
            state_file = HX_ROOT / "配置" / "推送状态.json"
            self._save_state(state_file, time.time())
            self._save_state_field(state_file, "last_push_ts_private", time.time())
            self._save_state_field(state_file, "last_push_ts_group", time.time())
            self._save_state_field(state_file, "empty_cycles_private", 0)
            self._save_state_field(state_file, "empty_cycles_group", 0)
            # D: 把"上次是否活跃"初始化为当前状态，避免重启被误判为"刚进入活跃"而触发首推
            for kind in ("private", "group"):
                st = self._effective_settings_for(kind)
                self._save_state_field(state_file, f"prev_active_{kind}", int(self._is_active_hours(st)))
        except Exception as e:
            self.ctx.logger.warning(f"初始化推送状态失败: {e}")
        self._task = asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._send_reminder_if_pending())

    async def _send_reminder_if_pending(self) -> None:
        """若存在提醒标记，给用户私聊发一条，发完删除（一次性）。"""
        try:
            if not REMINDER_FILE.exists():
                return
            text = REMINDER_FILE.read_text(encoding="utf-8").strip()
            stream_id = await self._resolve_stream_id()
            if stream_id and text:
                await self.ctx.send.text(text, stream_id)
            REMINDER_FILE.unlink(missing_ok=True)
        except Exception as e:
            self.ctx.logger.warning(f"发送提醒失败: {e}")

    async def on_unload(self) -> None:
        if self._task:
            self._task.cancel()

    # ────────────────────────── 推送调度（读有效设置）──────────────────────────

    def _read_toml(self, path: Path) -> dict:
        """读一个 toml，返回 {表名: {key:value}}；不存在或解析失败返回 {}。"""
        try:
            if not path.exists():
                return {}
            with open(path, "rb") as f:
                return dict(tomllib.load(f))
        except Exception:
            return {}

    def _user_pref_path(self, user_id: str) -> Path:
        return HX_ROOT / "配置" / f"推送偏好_{user_id}.toml"

    def _user_scoring_path(self, user_id: str) -> Path:
        return HX_ROOT / "配置" / f"推送规则_{user_id}.toml"

    def _pref_path(self, user_id: str) -> Path:
        return HX_ROOT / "配置" / f"偏好_{user_id}.toml"

    def _group_pref_path(self, group_id: str) -> Path:
        return HX_ROOT / "配置" / f"推送偏好_群{group_id}.toml"

    def _effective_settings(self, user_id: str) -> dict:
        """该用户实际的推送设置 = 内置默认 + 推送默认.toml + 用户偏好(覆盖)。"""
        settings = dict(BUILTIN_PUSH_DEFAULTS)
        settings.update(self._read_toml(PUSH_DEFAULT_FILE).get("push", {}))
        settings.update(self._read_toml(self._user_pref_path(user_id)).get("push", {}))
        return settings

    def _effective_settings_for(self, kind: str) -> dict:
        """按目标(私聊/群)取各自的生效设置，私聊和各群独立。kind: private / group_<群号>。"""
        base = dict(BUILTIN_PUSH_DEFAULTS)
        base.update(self._read_toml(PUSH_DEFAULT_FILE).get("push", {}))
        if kind.startswith("group_"):
            gid = kind[len("group_"):]
            base.update(self._read_toml(self._group_pref_path(gid)).get("push", {}))
        else:
            base.update(self._read_toml(self._user_pref_path(str(self.config.push.target_qq or ""))).get("push", {}))
        return base

    def _bool_setting(self, settings: dict, key: str, default: bool) -> bool:
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        return str(v).strip().lower() in ("true", "1", "yes", "on")

    def _is_active_hours(self, settings: dict) -> bool:
        """是否处于活跃时段。支持跨零点（如 11:00-01:00 表示 11:00~次日01:00）。"""
        w = str(settings.get("活跃时段", "11:00-01:00")).strip()
        try:
            s, e = w.split("-", 1)
            sh, sm = map(int, s.strip().split(":"))
            eh, em = map(int, e.strip().split(":"))
        except Exception:
            return True
        start = sh * 60 + sm
        end = eh * 60 + em
        now = datetime.now().hour * 60 + datetime.now().minute
        if start <= end:
            return start <= now <= end
        # 跨零点
        return now >= start or now <= end

    def _next_random_interval(self, settings: dict) -> int:
        """下一次推送的间隔（秒）。读配置的间隔分钟区间，取随机。"""
        mi = int(settings.get("间隔最小分钟", 20) or 20)
        ma = int(settings.get("间隔最大分钟", 40) or 40)
        if ma < mi:
            mi, ma = ma, mi
        return random.randint(mi, ma) * 60

    def _daily_reached(self, state_file: Path, max_count: int) -> bool:
        """今日推送数是否已达上限。跨天自动清零。"""
        if max_count <= 0:
            return False
        day_key = time.strftime("%Y-%m-%d")
        cur_day = self._load_state_field(state_file, "last_push_date", "")
        if cur_day != day_key:
            self._save_state_field(state_file, "last_push_date", day_key)
            self._save_state_field(state_file, "daily_push_count", 0)
            return False
        count = int(self._load_state_field(state_file, "daily_push_count", 0) or 0)
        return count >= max_count

    def _record_daily_push(self, state_file: Path) -> None:
        """记录一次成功推送，累加今日次数。"""
        day_key = time.strftime("%Y-%m-%d")
        cur_day = self._load_state_field(state_file, "last_push_date", "")
        if cur_day == day_key:
            count = int(self._load_state_field(state_file, "daily_push_count", 0) or 0)
            self._save_state_field(state_file, "daily_push_count", count + 1)
        else:
            self._save_state_field(state_file, "last_push_date", day_key)
            self._save_state_field(state_file, "daily_push_count", 1)

    async def _poll_loop(self) -> None:
        """受控节奏推送：按活跃时段 + 随机间隔 + 兜底 推送，不是来一条推一条。"""
        self.ctx.logger.info("航小麦受控节奏推送启动(活跃时段/随机间隔/兜底)")
        state_file = HX_ROOT / "配置" / "推送状态.json"

        while True:
            try:
                last_push = self._load_state(state_file)
                await self._maybe_push(state_file, last_push)
            except Exception as e:
                self.ctx.logger.warning(f"航小麦推送调度出错: {e}")
            await asyncio.sleep(30)

    def _load_state(self, state_file: Path) -> float:
        """读上次推送时间戳。"""
        try:
            if state_file.exists():
                d = json.loads(state_file.read_text(encoding="utf-8-sig"))
                return float(d.get("last_push_ts", 0))
        except Exception:
            pass
        return 0.0

    def _save_state(self, state_file: Path, ts: float) -> None:
        """记录上次推送时间（合并写入，不清空已有字段）。"""
        try:
            d = {}
            if state_file.exists():
                d = json.loads(state_file.read_text(encoding="utf-8-sig"))
            d["last_push_ts"] = ts
            state_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_state_field(self, state_file: Path, key: str, default):
        try:
            if state_file.exists():
                d = json.loads(state_file.read_text(encoding="utf-8-sig"))
                return d.get(key, default)
        except Exception:
            pass
        return default

    def _save_state_field(self, state_file: Path, key: str, value) -> None:
        try:
            d = {}
            if state_file.exists():
                d = json.loads(state_file.read_text(encoding="utf-8-sig"))
            d[key] = value
            state_file.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _item_sent_to(self, item: dict) -> list:
        """条目已发给哪些目标 kind（用于"每目标独立、不漏发"）。"""
        v = item.get("sent_to", [])
        return [str(x) for x in (v if isinstance(v, list) else [])]

    def _mark_item_sent(self, item: dict, kind: str) -> None:
        """记录某条目已发给某目标 kind。"""
        cur = [str(x) for x in (item.get("sent_to", []) if isinstance(item.get("sent_to", []), list) else [])]
        if kind and kind not in cur:
            cur.append(kind)
        item["sent_to"] = cur

    def _resume_occurred(self, state_file: Path) -> bool:
        """用 GetTickCount64(不含睡眠) 与 time.time()(华尔街钟) 差值判断系统是否睡过。
        两者差值基本恒定；若相对基线上跳 >60s → 期间系统休眠过(唤醒)，返回 True。"""
        try:
            import ctypes
            tick = ctypes.windll.kernel32.GetTickCount64() / 1000.0
        except Exception:
            return False
        wall = time.time()
        est = wall - tick  # 恒定的"开机墙钟偏移"，睡眠时会增大
        base = self._load_state_field(state_file, "boot_wall_base", 0.0)
        if not base:
            self._save_state_field(state_file, "boot_wall_base", est)
            return False
        if est - base > 60:
            self._save_state_field(state_file, "boot_wall_base", est)
            return True
        return False

    async def _maybe_push(self, state_file: Path, last_push: float) -> None:
        """受控调度：私聊/群各自独立，按各自生效设置决定该不该推、是否兜底。"""
        targets = await self._push_targets()
        if not targets:
            self.ctx.logger.warning("无推送目标流，跳过")
            return
        now = time.time()
        # 全局总闸：功能开关.toml 的"新闻推送"
        if self.config.push.auto_switch and not _load_switch_state():
            return
        # 只看"开了 + 当前活跃"的目标
        active = [t for t in targets
                  if self._bool_setting(t["settings"], "新闻推送", True)
                  and self._is_active_hours(t["settings"])]
        if not active:
            return

        # E: 精准休眠检测——若系统睡过(GetTickCount64差值跳变)，重置推送节奏，避免唤醒后突发连发
        if self._resume_occurred(state_file):
            for t in targets:
                kind = t["kind"]
                self._save_state_field(state_file, f"last_push_ts_{kind}", now)
                self._save_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(t["settings"]))
            self.ctx.logger.info("航小麦检测到系统休眠唤醒，已重置推送节奏")

        items = _load_pending()
        if items:
            # 有新内容 → 重置各目标的"空内容"计数
            for t in targets:
                self._save_state_field(state_file, f"empty_cycles_{t['kind']}", 0)
            today = time.strftime("%Y-%m-%d")
            # D: 每日首推=某目标"静默→活跃(该目标活跃开始)"瞬间触发一次，非"当天第一条/唤醒就弹"。
            #     用 prev_active_sig_<kind> 感知"活跃时段配置变没变"：变了就重置前态，杜绝改配置误触发。
            for t in targets:
                kind = t["kind"]
                sig = str(t["settings"].get("活跃时段", "") or "")
                prev_sig = self._load_state_field(state_file, f"prev_active_sig_{kind}", "")
                is_active = self._is_active_hours(t["settings"])
                prev = self._load_state_field(state_file, f"prev_active_{kind}", "")
                if prev_sig != sig:
                    # 活跃时段配置变了 → 重置"上次是否活跃/标签"，避免被误判成"刚进入活跃"
                    self._save_state_field(state_file, f"prev_active_sig_{kind}", sig)
                    self._save_state_field(state_file, f"prev_active_{kind}", int(is_active))
                    continue
                if is_active and prev in ("", "0", 0, False):
                    done = self._load_state_field(state_file, f"daily_summary_done_{kind}", "")
                    if done != today:
                        ok = await self._do_daily_first_push(state_file, now, t, items)
                        if ok:
                            self._save_state_field(state_file, f"daily_summary_done_{kind}", today)
                self._save_state_field(state_file, f"prev_active_{kind}", int(is_active))
            # 非首次：逐个"活跃且到点"的目标，按各自设置推送。
            # 每个目标独立从同一候选池取"自己还没发过"的 top 条（互不占用同一队列），
            # 保证 私聊/群 各自按自己配置推同一候选、且群不会因为私聊先发而漏掉。
            all_kinds = [t["kind"] for t in targets]
            sent_any = False
            for t in active:
                s = t["settings"]
                kind = t["kind"]
                last_t = self._load_state_field(state_file, f"last_push_ts_{kind}", 0.0)
                gap = self._load_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
                if last_t and (now - last_t) < gap:
                    continue
                daily_max = int(s.get("一天最多发几次", 0) or 0)
                if daily_max > 0 and self._daily_reached(state_file, daily_max):
                    continue
                batch = max(1, int(s.get("一次发几条", 1) or 1))
                # 只在这个目标"还没发过"的条目里取（别的目标发过但自己没发的也要发）
                candidates = [it for it in items if kind not in self._item_sent_to(it)]
                to_send = candidates[:batch]
                if not to_send:
                    continue
                with_img = self._bool_setting(s, "带图", True)
                for item in to_send:
                    news_text = str(item.get("zh") or item.get("raw") or "").strip()
                    if not news_text:
                        continue
                    try:
                        await self._speak_news(t["stream_id"], item, with_img)
                    except Exception as e:
                        self.ctx.logger.warning(f"航小麦推送单条失败: {e}")
                for item in to_send:
                    self._mark_item_sent(item, kind)
                self._record_sent(to_send)
                self._save_state_field(state_file, f"last_push_ts_{kind}", now)
                self._save_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
                self._record_daily_push(state_file)
                sent_any = True
                await asyncio.sleep(1.5)  # 私聊/群之间隔 1.5s，避免被 QQ 限流
            if sent_any:
                self._save_state(state_file, now)
                # 只保留"还有目标没发到"的条目（所有目标都发过才从候选池移除）
                keep = [it for it in items if not all(k in self._item_sent_to(it) for k in all_kinds)]
                _save_pending(keep)
            return

        # 队列空：有留档发留档；都空则按目标各自"闲聊"开关+间隔+连续3次空才来一句（独立，不再共享）
        archive = _load_pending_file(ARCHIVE_FILE, [])
        for t in active:
            s = t["settings"]
            kind = t["kind"]
            # 该目标的间隔还没到 → 先不打扰
            last_t = self._load_state_field(state_file, f"last_push_ts_{kind}", 0.0)
            gap = self._load_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
            if last_t and (now - last_t) < gap:
                continue
            if archive:
                # 有旧闻 → 发一条留档（内容型兜底），并按该目标带图
                try:
                    ok = await self._send_one_archive_to(t)
                    if ok:
                        self.ctx.logger.info(f"航小麦兜底推送(留档旧闻 -> {kind})")
                        self._save_state_field(state_file, f"last_push_ts_{kind}", now)
                        self._save_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
                        self._save_state_field(state_file, f"empty_cycles_{kind}", 0)
                        self._record_daily_push(state_file)
                except Exception as e:
                    self.ctx.logger.warning(f"航小麦留档兜底失败({kind}): {e}")
            else:
                # 没有旧闻也没新内容：连续3次空 → 由该目标决定是否闲聊
                if not self._bool_setting(s, "闲聊", True):
                    continue
                empty = int(self._load_state_field(state_file, f"empty_cycles_{kind}", 0) or 0) + 1
                if empty >= 3:
                    try:
                        ok = await self._send_casual_to(t)
                        if ok:
                            self.ctx.logger.info(f"航小麦兜底推送(无消息闲聊 -> {kind})")
                            self._save_state_field(state_file, f"last_push_ts_{kind}", now)
                            self._save_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
                            self._save_state_field(state_file, f"empty_cycles_{kind}", 0)
                            self._record_daily_push(state_file)
                        else:
                            self._save_state_field(state_file, f"empty_cycles_{kind}", empty)
                    except Exception as e:
                        self.ctx.logger.warning(f"航小麦闲聊兜底失败({kind}): {e}")
                        self._save_state_field(state_file, f"empty_cycles_{kind}", empty)
                else:
                    self._save_state_field(state_file, f"empty_cycles_{kind}", empty)

    async def _do_daily_first_push(self, state_file: Path, now: float, target: dict, items: list) -> bool:
        """单个目标的每日首推：按分数取前10 → 合成一条批量卡(卡+综述)发给该目标；成功则把前10标记"已发给该目标"，
        不再全局清空待推送（其他目标仍可各自取）；条目最终由"所有目标都发过"后再移除。"""
        kind = target["kind"]
        sorted_items = sorted(items, key=lambda x: x.get("score", 0) or 0, reverse=True)
        top = sorted_items[:10]
        with_img = self._bool_setting(target["settings"], "带图", True)
        sent = await self._send_summary([target["stream_id"]], top, with_img)
        if sent <= 0:
            self.ctx.logger.warning("每日首推发送失败，保留待推送稍后重试")
            return False
        for it in top:
            self._mark_item_sent(it, kind)
        self._record_sent(top)
        self._save_state_field(state_file, f"last_push_ts_{kind}", now)
        self._save_state_field(state_file, f"next_gap_{kind}", self._next_random_interval(target["settings"]))
        self._record_daily_push(state_file)
        self.ctx.logger.info(f"航小麦每日首推({kind})：前{len(top)}条已汇总发送")
        return True

    async def _send_summary(self, streams: list, top_items: list, with_img: bool = True) -> int:
        """每日首推：发两条 —— ①麦麦俏皮开场白 + 各条目「来源行+原文翻译」逐行 + 各图(超上限拆消息)；②麦麦综述(按条分行)。"""
        if not streams:
            return 0
        persona = self._maisaka_persona()
        # 开场白（麦麦口吻，俏皮，不拘泥固定句）
        opening = ""
        try:
            opening = self._llm_text(
                f"{persona}\n\n现在是每日航天新闻总结时间，请用你平时说话的方式，写一句俏皮、有个性的开场白，"
                "类似'听赛博牢A给你每日夜间新闻总结！'这种感觉，但更有意思一点，别套模板。只输出那一句开场白。",
                temperature=0.9,
            )
        except Exception as e:
            self.ctx.logger.warning(f"生成开场白失败: {e}")
        opening = str(opening or "").strip() or "听赛博牢A给你每日夜间新闻总结！"
        # 目录（各条 来源行 + 原文翻译；转推附转发者评论译文）
        lines = []
        for it in top_items:
            src = self._build_source_line(it)
            body = str(it.get("zh") or it.get("raw") or "").strip()
            line = (src + "\n" + body) if src else body
            fl = self._forward_line(it)
            if fl:
                line += "\n" + fl
            lines.append(line)
        card_text = opening + "\n\n" + "\n———\n".join(lines)
        # 汇总所有条目的图
        all_images = []
        if with_img:
            for it in top_items:
                all_images.extend(self._item_images(it))
        # 综述（按条分行）
        try:
            summary = self._llm_text(
                f"{persona}\n\n你在执行用户交给你的任务：帮用户把他想看的航天内容挑出来并推送。"
                "下面是你今天挑出的、值得看的内容，请按条分成几点、每点一行，用你平时说话的方式简单说几句，"
                "别写成一整段、别太长。\n\n内容：\n"
                + "\n".join(f"- {(str(it.get('zh') or it.get('raw') or ''))[:80]}" for it in top_items),
                temperature=0.7,
            )
        except Exception as e:
            self.ctx.logger.warning(f"生成综述失败: {e}")
            summary = ""
        if not summary:
            summary = "今天给你挑了几条航天内容，配图发你，感兴趣可以看看～"
        sent = 0
        for sid in streams:
            try:
                ok1 = await self._send_text_and_images(sid, card_text, all_images)
                if not ok1:
                    self.ctx.logger.warning(f"每日首推目录发送失败(可能消息/图被拒): {sid}")
                await asyncio.sleep(2)  # 目录发出后稍停顿，再补综述
                r2 = await self.ctx.send.text(summary, sid, return_details=True)
                ok2 = bool(r2) if not isinstance(r2, dict) else bool(r2.get("sent"))
                if ok1:
                    sent += 1
            except Exception as e:
                self.ctx.logger.warning(f"每日首推发送失败: {e}")
        return sent

    def _archive_to_store(self, items: list) -> None:
        """把条目追加进 内容留档.json（按 账号+内容 去重）。"""
        if not items:
            return
        archive = _load_pending_file(ARCHIVE_FILE, [])
        seen = {(str(x.get("account", "")), str(x.get("zh", ""))) for x in archive}
        for it in items:
            k = (str(it.get("account", "")), str(it.get("zh", "")))
            if k not in seen:
                archive.append(it)
                seen.add(k)
        _save_pending_file(ARCHIVE_FILE, archive)

    def _record_sent(self, items: list) -> None:
        """把已推送的条目记入 已发记录.json（按 账号+内容 去重，保留最近500条）。"""
        if not items:
            return
        records = _load_pending_file(SENT_EVER_FILE, [])
        seen = {(str(x.get("account", "")), str(x.get("zh", ""))) for x in records}
        for it in items:
            k = (str(it.get("account", "")), str(it.get("zh", "")))
            if k not in seen:
                records.append(it)
                seen.add(k)
        _save_pending_file(SENT_EVER_FILE, records[-500:])

    async def _send_one_archive_to(self, t: dict) -> bool:
        """取一条留档旧闻，按该目标的带图设置发给这个目标。"""
        archive = _load_pending_file(ARCHIVE_FILE, [])
        if not archive:
            return False
        item = archive.pop(0)
        _save_pending_file(ARCHIVE_FILE, archive)
        self._record_sent([item])
        news_text = str(item.get("zh") or item.get("raw") or "").strip() or "最近攒了点航天内容，挑一条给你～"
        with_img = self._bool_setting(t["settings"], "带图", True)
        return await self._speak_news(t["stream_id"], item, with_img)

    async def _send_casual_to(self, t: dict) -> bool:
        """没新内容也没旧闻时，用麦麦人设生成一句闲聊发给这个目标。"""
        persona = self._maisaka_persona()
        prompt = (
            f"{persona}\n\n"
            "现在航天圈暂时没什么大新闻，你像往常一样，用你自己平时的话，随口跟对方说点什么"
            "（可以聊聊你最近在蹲什么、随口吐槽两句没动静、说说你好奇的航天话题，哪怕一句日常也行）。"
            "要求：别像模板、别套话，就一两句，自然、有人味，像你平时的样子。\n\n"
            "你现在开口说："
        )
        reply = self._llm_text(prompt, temperature=0.95)
        if not reply:
            reply = "先忙你的啦，有航天好玩的我马上喊你。"
        try:
            r = await self.ctx.send.text(reply, t["stream_id"], return_details=True)
            return bool(r) if not isinstance(r, dict) else bool(r.get("sent"))
        except Exception as e:
            self.ctx.logger.warning(f"闲聊发送失败: {e}")
            return False

    async def _resolve_stream_id(self) -> str:
        """拿目标QQ的 stream_id。优先已有会话，否则 open_session。"""
        target = self.config.push.target_qq
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(str(target), platform="qq")
            if isinstance(stream, dict) and str(stream.get("stream_id") or "").strip():
                return str(stream["stream_id"])
        except Exception as e:
            self.ctx.logger.warning(f"get_stream_by_user_id 失败: {e}")
        try:
            opened = await self.ctx.chat.open_session(
                platform="qq", chat_type="private", user_id=str(target)
            )
            if isinstance(opened, dict):
                return str(opened.get("session_id") or opened.get("stream_id") or "")
        except Exception as e:
            self.ctx.logger.warning(f"open_session 失败: {e}")
        return ""

    def _img_to_base64(self, path: str) -> str:
        """读本地图片转 base64（发原图，不压缩）。失败返回 ''。"""
        try:
            p = Path(path)
            if not p.exists():
                return ""
            return base64.b64encode(p.read_bytes()).decode("utf-8")
        except Exception:
            return ""

    @staticmethod
    def _extract_stream_id(resp) -> str:
        """从能力返回里稳健取 stream_id（兼容 {stream_id:..} 与 {stream:{stream_id:..}} 两种结构）。"""
        if not isinstance(resp, dict):
            return ""
        sid = str(resp.get("stream_id") or "").strip()
        if sid:
            return sid
        inner = resp.get("stream")
        if isinstance(inner, dict):
            return str(inner.get("stream_id") or inner.get("session_id") or "").strip()
        return ""

    async def _push_targets(self) -> list:
        """返回推送目标列表：[{kind, stream_id, settings}]，私聊/各群各自带自己的生效设置。

        kind: "private"(私聊 target_qq) 或 "group_<群号>"。
        """
        targets = []
        priv = str(self.config.push.target_qq or "")
        if priv:
            sid = ""
            try:
                s = await self.ctx.chat.get_stream_by_user_id(priv, platform="qq")
                sid = self._extract_stream_id(s)
            except Exception as e:
                self.ctx.logger.warning(f"私聊 get_stream_by_user_id 失败: {e}")
            if not sid:
                try:
                    opened = await self.ctx.chat.open_session(platform="qq", chat_type="private", user_id=priv)
                    sid = self._extract_stream_id(opened)
                except Exception as e:
                    self.ctx.logger.warning(f"私聊 open_session 失败: {e}")
            if sid:
                targets.append({"kind": "private", "stream_id": sid,
                                "settings": self._effective_settings_for("private")})
        for gid in (self.config.push.target_groups or []):
            gid = str(gid).strip()
            if not gid:
                continue
            kind = f"group_{gid}"
            sid = ""
            try:
                g = await self.ctx.chat.get_stream_by_group_id(gid, platform="qq")
                sid = self._extract_stream_id(g)
            except Exception as e:
                self.ctx.logger.warning(f"群 {gid} get_stream_by_group_id 失败: {e}")
            if not sid:
                # 没有现成群会话 → 主动创建（像私聊那样）
                try:
                    opened = await self.ctx.chat.open_session(platform="qq", chat_type="group", group_id=gid)
                    sid = self._extract_stream_id(opened)
                except Exception as e:
                    self.ctx.logger.warning(f"群 {gid} open_session 失败: {e}")
            if sid:
                targets.append({"kind": kind, "stream_id": sid,
                                "settings": self._effective_settings_for(kind)})
            else:
                self.ctx.logger.warning(f"群 {gid} 拿不到会话，跳过")
        return [t for t in targets if t.get("stream_id")]

    def _maisaka_persona(self) -> str:
        """读麦麦真实人设（[personality] personality + reply_style），用于生成麦麦口吻。

        读取麦麦自己的配置，避免我们手编导致风格割裂。
        """
        try:
            import tomllib
            cfg = tomllib.load(open(MAIBOT_CONFIG, "rb"))
            p = cfg.get("personality", {}) or {}
            personality = str(p.get("personality", "") or "")
            reply_style = str(p.get("reply_style", "") or "")
        except Exception:
            personality, reply_style = "", ""
        parts = [x for x in (personality, reply_style) if x.strip()]
        return "\n".join(parts) if parts else "你是麦麦，一个刚开始入坑、爱探索的航天爱好者。"

    @staticmethod
    def _fmt_beijing(date_str: str, ts) -> str:
        """把 UTC 时间转成 北京时间 'UTC+8-YYYY-MM-DD-HH:MM'；失败返回 ''."""
        import datetime as dt
        d = None
        if ts:
            try:
                d = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)
            except Exception:
                d = None
        if d is None and date_str:
            try:
                s = str(date_str).strip().replace("T", " ").replace("+00:00", "").strip()
                d = dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
            except Exception:
                d = None
        if d is None:
            return ""
        bj = d.astimezone(dt.timezone(dt.timedelta(hours=8)))
        return "UTC+8-" + bj.strftime("%Y-%m-%d-%H:%M")

    def _build_source_line(self, item: dict) -> str:
        """构造来源行：来源：@账户 · 平台 · 时间(北京时间)。平台现全为 X，留字段以后扩展。
        转推时若带 orig_account(原帖作者)，来源行显示原帖作者。"""
        account = str(item.get("orig_account", "") or item.get("account", "") or "")
        platform = str(item.get("platform", "") or "X")
        date = self._fmt_beijing(str(item.get("date", "") or ""), item.get("ts"))
        if not account and not date:
            return ""
        parts = [f"@{account}"] if account else []
        if platform:
            parts.append(platform)
        if date:
            parts.append(date)
        return "来源：" + " · ".join(parts)

    def _forward_line(self, item: dict) -> str:
        """若该条是转推且转发者加了评论，返回 `转发@<账号>：<译文>`（译文=翻译成中文）；无则返回 ''。"""
        fc = str(item.get("forward_comment", "") or "").strip()
        if not fc:
            return ""
        fa = str(item.get("account", "") or "")
        try:
            tr = self._llm_text(
                f"把下面这句比较短的转发者评论翻译成简体中文，直接输出译文，不要加任何前缀或引号：\n{fc}",
                temperature=0.3,
            )
        except Exception:
            tr = ""
        tr = str(tr or "").strip() or fc
        return (f"转发@{fa}：" + tr) if fa else ("转发者：" + tr)

    def _item_images(self, item: dict) -> list:
        """从 item 取图片路径列表（优先 images，回退 image）。"""
        imgs = item.get("images", [])
        if not isinstance(imgs, list):
            imgs = []
        imgs = [str(x) for x in imgs if str(x).strip()]
        if not imgs:
            single = str(item.get("image", "") or "")
            if single:
                imgs = [single]
        return imgs

    async def _send_text_and_images(self, stream_id: str, text: str, image_paths: list) -> bool:
        """发送一条文字 + 图片（超 MAX_IMG_PER_MSG 则另凑纯图消息）。返回首条是否成功。"""
        first_b64 = []
        for p in image_paths[:MAX_IMG_PER_MSG]:
            b = self._img_to_base64(p)
            if b:
                first_b64.append(b)
        ok = False
        try:
            if first_b64:
                segs = [{"type": "text", "content": text}] + [{"type": "image", "content": b} for b in first_b64]
                r = await self.ctx.send.hybrid(segs, stream_id, return_details=True)
                self.ctx.logger.info(f"航小麦带图发送({len(first_b64)}张)")
            else:
                r = await self.ctx.send.text(text, stream_id, return_details=True)
            ok = bool(r) if not isinstance(r, dict) else bool(r.get("sent"))
        except Exception as e:
            self.ctx.logger.warning(f"发送(文字+图)失败(可能图/消息被拒): {e}")
        # 超出上限的图：每 MAX 张一条纯图消息
        extra = image_paths[MAX_IMG_PER_MSG:]
        for i in range(0, len(extra), MAX_IMG_PER_MSG):
            chunk = extra[i:i + MAX_IMG_PER_MSG]
            segs = []
            for p in chunk:
                b = self._img_to_base64(p)
                if b:
                    segs.append({"type": "image", "content": b})
            if not segs:
                continue
            try:
                await self.ctx.send.hybrid(segs, stream_id, return_details=True)
            except Exception as e:
                self.ctx.logger.warning(f"发送多出图片失败: {e}")
        return ok

    async def _speak_news(self, stream_id: str, item: dict, with_img: bool = True) -> bool:
        """用麦麦真实人设轻量生成口吻，再由航小麦发送到该聊天流。

        新格式：发两条 —— ①来源行 + 原文翻译(+图，无图则纯文字)；②麦麦评论。图超上限会自动另凑消息。
        不触发麦麦 planner（避免加载海量上下文导致 400），可稳定送达；文案=麦麦本人设。
        """
        if not stream_id:
            return False
        translation = str(item.get("zh") or item.get("raw") or "").strip()
        if not translation:
            return False
        source = self._build_source_line(item)
        text1 = (source + "\n\n" + translation) if source else translation
        fwd = self._forward_line(item)
        if fwd:
            text1 += "\n\n" + fwd
        persona = self._maisaka_persona()
        prompt = (
            f"{persona}\n\n"
            "下面是刚给你的一条航天内容，请你用你平时说话的方式，写一句你自己的想法/评论。"
            "自然、真诚、别套话、别长篇大论，一两句就行。\n\n"
            f"内容：{translation[:500]}"
        )
        try:
            reply = self._llm_text(prompt, temperature=0.7)
        except Exception as e:
            self.ctx.logger.warning(f"生成麦麦评论失败: {e}")
            reply = ""
        if not reply:
            reply = "这条有点意思，你可以看看。"
        images = self._item_images(item) if with_img else []
        ok_main = await self._send_text_and_images(stream_id, text1, images)
        await asyncio.sleep(2)  # 原文/图之后稍作停顿再补评论
        ok_reply = False
        try:
            r2 = await self.ctx.send.text(reply, stream_id, return_details=True)
            ok_reply = bool(r2) if not isinstance(r2, dict) else bool(r2.get("sent"))
        except Exception as e:
            self.ctx.logger.warning(f"发送评论失败: {e}")
        return ok_main or ok_reply

    # ────────────────── 自然语言设置推送相关配置（仅私聊，按人各改各的）──────────────────

    _SETTING_TOKENS = [
        "推送", "发新闻", "别推", "不发", "少发", "多发", "时段", "几点",
        "每次", "每隔", "频率", "带图", "带不带图", "几条", "一次", "每间隔", "每天", "推送设置",
        "判定", "阈值", "权重", "发射", "任务", "科普", "历史", "商业", "故障", "爱看", "少来", "多来", "更喜欢",
    ]

    _SCHEDULE_KEYS = set(BUILTIN_PUSH_DEFAULTS.keys())  # 推送方式
    _SCORE_WEIGHT_KEYS = {"发射", "任务", "重大任务", "科普", "历史", "商业", "故障", "带图视频", "重复旧闻"}
    _SCORE_THRESHOLD_KEYS = {"高阈值", "低阈值"}
    _SCORE_ALLOW_KEYS = {"可发送领域", "可发送账号", "可发送关键词"}
    _PREF_LIKE_KEYS = {"喜欢账号", "喜欢领域", "喜欢关键词"}
    _PREF_DISLIKE_KEYS = {"不喜欢领域", "不喜欢关键词"}

    def _looks_like_push_pref(self, text: str) -> bool:
        """粗判是否是"设置推送相关配置"的指令，避免每次都要调 LLM 也避免劫持日常聊天。"""
        return any(tok in text for tok in self._SETTING_TOKENS)

    def _llm_text(self, prompt: str, temperature: float = 0.0) -> str:
        """直连 DeepSeek（deepseek-chat，纯文本）取一段话；失败返回 ''。

        用于在 Hook 内做解析/确认——不依赖宿主 cap.call，避免 30s 宿主限额超时。
        """
        if not DEEPSEEK_KEY:
            return ""
        try:
            client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE, timeout=20)
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            self.ctx.logger.warning(f"DeepSeek 直连失败: {e}")
            return ""

    async def _parse_push_pref(self, text: str, context: str = "") -> dict:
        """让 LLM 用自然语言理解意图（不限定航天词），返回"""
        prompt = (
            "你是航小麦配置解析器。用自然语言理解用户这句话里的'推送/内容偏好'，任意词都认（二次元、DDLC、广告、某账号都行）。\n"
            "结合上下文最近几句聊天，判断这句是不是在针对'推送/内容'做设置。\n"
            "输出JSON：{\"action\":\"set\",\"schedule\":{},\"scoring\":{},\"preference\":{}}。\n"
            "- schedule 推送方式：新闻推送(bool)、活跃时段(\"HH:MM-HH:MM\")、间隔最小分钟(int)、间隔最大分钟(int)、"
            "一次发几条(int)、带图(bool)、一天最多发几次(int)\n"
            "- scoring 评分/可发送内容：高阈值(int)、低阈值(int)、发射/任务/重大任务/科普/历史/商业/故障/带图视频/重复旧闻(int权重)、"
            "可发送领域/可发送账号/可发送关键词(字符串数组，'可以发送的内容=X,y,z'即这些)\n"
            "- preference 内容偏好（任意词列表）：\n"
            "   多发(list)：爱看/喜欢/多给我发 X\n"
            "   少发(list)：少发/别太多/少来点 X\n"
            "   排除(list)：别发/不要/不要再发 X\n"
            "每段只放用户提到的字段，没提到的别出现。只有明确'与配置/内容偏好完全无关'才输出 {\"action\":\"none\"}；"
            "若用户明显在说内容/推送偏好但你不确定具体改什么，输出 {\"action\":\"ask\",\"hint\":\"你想改推送偏好吗？比如'少发二次元'/\"别发某某账号\"\"。\n"
            "只输出一个JSON对象，不要任何其他文字、不要代码块。\n"
        )
        if context.strip():
            prompt += f"\n最近聊天上下文：\n{context.strip()[-1200:]}\n"
        prompt += f"\n用户当前的这句话：{text}"
        try:
            raw = self._llm_text(prompt, temperature=0.0)
        except Exception as e:
            self.ctx.logger.warning(f"推送配置解析失败(调用): {e}")
            return {"action": "none"}
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"action": "none"}
        try:
            data = json.loads(m.group(0))
        except Exception:
            return {"action": "none"}
        if not isinstance(data, dict):
            return {"action": "none"}
        action = data.get("action", "none")
        out = {"action": action}
        if action == "ask":
            out["hint"] = str(data.get("hint") or "")
        for sec in ("schedule", "scoring", "preference"):
            v = data.get(sec)
            out[sec] = v if isinstance(v, dict) else {}
        return out

    @staticmethod
    def _toml_value(v) -> str:
        """把简单值格式化为 TOML 字面量（支持列表）。"""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(HangxiaomaiNewsPlugin._toml_value(x) for x in v) + "]"
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'

    def _write_toml(self, path: Path, tables: dict) -> None:
        """把一个 {表名: {key:value}} 写成嵌套 TOML。"""
        lines = []
        for table, fields in tables.items():
            if not fields:
                continue
            lines.append(f"[{table}]")
            for k, v in fields.items():
                lines.append(f'"{k}" = {self._toml_value(v)}')
            lines.append("")
        if not lines:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception as e:
            self.ctx.logger.warning(f"写配置失败: {e}")

    def _apply_setting(self, user_id: str, parsed: dict) -> dict:
        """把解析出的三段分别写进对应的按用户配置，返回改了哪些。"""
        changed = {}
        sched = parsed.get("schedule") or {}
        if sched:
            self._apply_schedule(user_id, sched)
            changed["推送方式"] = list(sched)
        scoring = parsed.get("scoring") or {}
        if scoring:
            self._apply_scoring(user_id, scoring)
            changed["评分"] = list(scoring)
        pref = parsed.get("preference") or {}
        if pref:
            self._apply_preference(user_id, pref)
            changed["偏好"] = list(pref)
        return changed

    def _apply_schedule(self, user_id: str, fields: dict) -> None:
        """写 推送偏好_<QQ>.toml 的 [push]（带校验，防非法值落盘）。"""
        path = self._user_pref_path(user_id)
        existing = self._read_toml(path).get("push", {})
        new = dict(existing)
        for k, v in fields.items():
            if k not in self._SCHEDULE_KEYS or v is None or v == "":
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
                    if n < 0:
                        n = 0
                    new[k] = n
                except Exception:
                    continue
            elif k in ("新闻推送", "带图"):
                new[k] = bool(v) if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
        self._write_toml(path, {"push": new})

    def _apply_scoring(self, user_id: str, fields: dict) -> None:
        """写 推送规则_<QQ>.toml 的 [weights]/[threshold]/[allowlist]（带校验/钳制）。"""
        path = self._user_scoring_path(user_id)
        existing = self._read_toml(path)
        weights = dict(existing.get("weights", {}))
        threshold = dict(existing.get("threshold", {}))
        allow = dict(existing.get("allowlist", {}))
        for k, v in fields.items():
            if v is None or v == "":
                continue
            if k in self._SCORE_WEIGHT_KEYS:
                try:
                    weights[k] = int(v)
                except Exception:
                    continue
            elif k in self._SCORE_THRESHOLD_KEYS:
                try:
                    threshold[k] = max(0, min(200, int(v)))
                except Exception:
                    continue
            elif k in self._SCORE_ALLOW_KEYS:
                if isinstance(v, list):
                    allow[k] = [str(x) for x in v if str(x).strip()]
                elif isinstance(v, str):
                    allow[k] = [x.strip() for x in v.split(",") if x.strip()]
        tables = {}
        if weights:
            tables["weights"] = weights
        if threshold:
            tables["threshold"] = threshold
        if allow:
            tables["allowlist"] = allow
        if tables:
            self._write_toml(path, tables)

    def _apply_preference(self, user_id: str, fields: dict) -> None:
        """写 偏好_<QQ>.toml：多发→[like]喜欢关键词(+15)、少发→[dislike]不喜欢关键词(-15)、排除→[exclude]排除(硬丢)。

        任意词都支持（不限定航天）。
        """
        path = self._pref_path(user_id)
        existing = self._read_toml(path)
        like = dict(existing.get("like", {}))
        dislike = dict(existing.get("dislike", {}))
        exclude = dict(existing.get("exclude", {}))
        more = _to_list(fields.get("多发"))
        less = _to_list(fields.get("少发"))
        block = _to_list(fields.get("排除"))
        if more:
            _add_words(like, "喜欢关键词", more)
        if less:
            _add_words(dislike, "不喜欢关键词", less)
        if block:
            _add_words(exclude, "排除关键词", block)
        # 兼容模型直接给 喜欢/不喜欢 列表
        for k in self._PREF_LIKE_KEYS:
            if fields.get(k):
                like[k] = _to_list(fields[k])
        for k in self._PREF_DISLIKE_KEYS:
            if fields.get(k):
                dislike[k] = _to_list(fields[k])
        tables = {}
        if like:
            tables["like"] = like
        if dislike:
            tables["dislike"] = dislike
        if exclude:
            tables["exclude"] = exclude
        if tables:
            self._write_toml(path, tables)

    async def _build_setting_reply(self, parsed: dict) -> str:
        """生成一句自然、具体的确认话。"""
        desc = []
        naming = {"schedule": "推送方式", "scoring": "评分", "preference": "偏好"}
        for sec in ("schedule", "scoring", "preference"):
            fields = parsed.get(sec) or {}
            if fields:
                desc.append(f"{naming[sec]}=" + "、".join(f"{k}={v}" for k, v in fields.items()))
        if not desc:
            return ""
        combined = "；".join(desc)
        try:
            prompt = (
                "你是麦麦，刚接收了用户对\u2018航天内容推送\u2019的修改。"
                "请用一句简洁自然、有人味的话确认，让用户知道以后会照办，"
                "要提到具体改动点，不要说套话、不要长篇。\n"
                f"改动：{combined}\n只输出你确认的那一句话。"
            )
            reply = self._llm_text(prompt, temperature=0.7)
            if reply:
                return reply
        except Exception as e:
            self.ctx.logger.warning(f"生成确认话失败: {e}")
        return f"好嘞，我记下了：{combined}，之后推送就这么来。"

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
            processed_plain_text / session_id / platform / message_info{user_info{user_id}, group_info}
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
            if not self._looks_like_push_pref(text):
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

            parsed = await self._parse_push_pref(text, context=context)

            # 模型判断"在说内容/推送偏好但不确定改什么"→ 反问确认，不再静默
            if parsed.get("action") == "ask":
                hint = str(parsed.get("hint") or "刚这句我没太懂，你是想改推送设置吗？可以举个例子，比如'少发二次元''别发某某账号''可以发送的内容=发射,科普'～")
                if sid:
                    await self.ctx.send.text(hint, sid)
                return {"action": "abort"}

            sections = {k: parsed.get(k) for k in ("schedule", "scoring", "preference") if parsed.get(k)}
            if parsed.get("action") != "set" or not sections:
                return continue_result

            changed = self._apply_setting(user_id, parsed)
            self.ctx.logger.info(f"已更新用户 {user_id} 推送配置: {changed}")
            reply = await self._build_setting_reply(parsed)
            if sid and reply:
                await self.ctx.send.text(reply, sid)
            return {"action": "abort"}
        except Exception as e:
            self.ctx.logger.warning(f"推送设置事件处理异常: {e}")
            return continue_result

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """处理配置热重载事件。"""
        del scope
        del config_data
        del version


def create_plugin() -> HangxiaomaiNewsPlugin:
    """创建航小麦新闻推送插件实例。"""
    return HangxiaomaiNewsPlugin()
