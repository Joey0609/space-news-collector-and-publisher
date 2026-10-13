# -*- coding: utf-8 -*-
"""hx_schedule.py · ScheduleMixin —— 受控节奏推送调度（本插件的核心）
==================================================================
一句话：**不是"来一条推一条"，而是按每个目标自己的设置，决定"现在该不该开口"。**

内容从哪来：看板的渠道 WebSocket（`hx_ws.ChannelClient`，**拉取模式**）。插件不再直接读数据库——
它按自己的节奏 `want(source, N)` 要一批、发完用 `result` 回报，看板收到成功回执才写库
（`items.pushed`）。协议见 `dashboard/PROTOCOL.md`。

每 30 秒醒一次，做这些判断（顺序即优先级）：
    1) 全局总闸：功能开关.toml 的"新闻推送"关掉 → 什么都不做
    2) 只看"开了 + 当前在活跃时段"的目标（活跃时段支持跨零点，如 11:00-01:00）
    3) 系统刚休眠唤醒 → 重置各目标节奏，避免唤醒后突发连发
    4) 某目标"静默→活跃"的那一刻 → 每日首推（拉 10 条合成总结卡）
       否则按 各自的上次推送时间 + 随机间隔 到点就发；一次发几条、是否带图、
       一天最多几次 都是每个目标自己的设置
    5) 所有目标都没有待发内容：有留档旧闻发旧闻；都没有且连续 3 轮空 → 看"闲聊"开关决定是否说一句

状态字段含义见 hx_state.py 顶部注释。
"""
import asyncio
import random
import time
from datetime import datetime

if __package__ in (None, ""):
    from hx_paths import STATE_FILE
    from hx_state import (
        daily_reached,
        load_field,
        load_last_push,
        record_daily_push,
        resume_occurred,
        save_field,
        save_last_push,
    )
    from hx_store import load_archive, load_switch_state, take_reminder
else:
    from .hx_paths import STATE_FILE
    from .hx_state import (
        daily_reached,
        load_field,
        load_last_push,
        record_daily_push,
        resume_occurred,
        save_field,
        save_last_push,
    )
    from .hx_store import load_archive, load_switch_state, take_reminder

CHANNEL = "maibot"   # 本插件的通道名：source = "maibot:private" / "maibot:group_<群号>"


def source_of(kind: str) -> str:
    """目标 kind → source 名（与看板、艾莉森口径一致：`通道:目标`）。"""
    return f"{CHANNEL}:{kind}"


class ScheduleMixin:
    """节奏与调度。依赖 self.config / self.ctx 以及 SendMixin/FallbackMixin 的方法。"""

    # ────────────────────────── 小工具 ──────────────────────────

    def _is_active_hours(self, settings: dict) -> bool:
        """是否处于活跃时段。支持跨零点（如 11:00-01:00 表示 11:00~次日 01:00）。"""
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
        return now >= start or now <= end   # 跨零点

    def _next_random_interval(self, settings: dict) -> int:
        """下一次推送的间隔（秒）：读配置的分钟区间取随机；区间写反了会自动纠正。"""
        mi = int(settings.get("间隔最小分钟", 20) or 20)
        ma = int(settings.get("间隔最大分钟", 40) or 40)
        if ma < mi:
            mi, ma = ma, mi
        return random.randint(mi, ma) * 60

    def _channel(self):
        """看板渠道客户端（plugin.py 在 on_load 里建好；自检里可换成假的）。"""
        return getattr(self, "_channel_client", None)

    def _channel_ready(self) -> bool:
        """渠道可用（连上看板 + 没被服务端暂停）才可以要内容。"""
        client = self._channel()
        return bool(client is not None and getattr(client, "connected", False)
                    and not getattr(client, "paused", True))

    # ────────────────────────── 主循环 ──────────────────────────

    async def _poll_loop(self) -> None:
        """受控节奏推送主循环（每 30 秒醒一次）。"""
        self.ctx.logger.info("航小麦受控节奏推送启动(活跃时段/随机间隔/兜底)")
        state_file = STATE_FILE
        while True:
            try:
                await self._maybe_push(state_file, load_last_push(state_file))
            except Exception as e:
                self.ctx.logger.warning(f"航小麦推送调度出错: {e}")
            await asyncio.sleep(30)

    async def _send_reminder_if_pending(self) -> None:
        """若存在 提醒待发.txt，给用户私聊发一条并删除（一次性）。"""
        try:
            text = take_reminder()
            if not text:
                return
            stream_id = await self._resolve_stream_id()
            if stream_id:
                await self._send_text(stream_id, text)
            else:
                self.ctx.logger.warning("提醒待发.txt 有内容，但拿不到私聊 stream_id")
        except Exception as e:
            self.ctx.logger.warning(f"发送提醒失败: {e}")

    # ────────────────────────── 一轮调度 ──────────────────────────

    async def _maybe_push(self, state_file, last_push: float) -> None:
        """受控调度：私聊/群各自独立，按各自生效设置决定该不该推、是否兜底。"""
        targets = await self._push_targets()
        if not targets:
            self.ctx.logger.warning("无推送目标流，跳过")
            return
        now = time.time()
        # 全局总闸：功能开关.toml 的"新闻推送"
        if self.config.push.auto_switch and not load_switch_state():
            return
        # 只看"开了 + 当前活跃"的目标
        active = [t for t in targets
                  if self._bool_setting(t["settings"], "新闻推送", True)
                  and self._is_active_hours(t["settings"])]
        if not active:
            return
        # 没连上看板（或服务端把本渠道暂停了）→ 这一轮什么都不做，等连上/恢复
        if not self._channel_ready():
            return

        # 精准休眠检测——若系统睡过(GetTickCount64 差值跳变)，重置推送节奏，避免唤醒后突发连发
        if resume_occurred(state_file):
            for t in targets:
                kind = t["kind"]
                save_field(state_file, f"last_push_ts_{kind}", now)
                save_field(state_file, f"next_gap_{kind}", self._next_random_interval(t["settings"]))
            self.ctx.logger.info("航小麦检测到系统休眠唤醒，已重置推送节奏")

        today = time.strftime("%Y-%m-%d")
        # ── 每日首推 = 某目标"静默→活跃"瞬间触发一次，非"当天第一条/唤醒就弹"。
        #    用 prev_active_sig_<kind> 感知"活跃时段配置变没变"：变了就重置前态，杜绝改配置误触发。
        for t in targets:
            kind = t["kind"]
            sig = str(t["settings"].get("活跃时段", "") or "")
            prev_sig = load_field(state_file, f"prev_active_sig_{kind}", "")
            is_active = self._is_active_hours(t["settings"])
            prev = load_field(state_file, f"prev_active_{kind}", "")
            if prev_sig != sig:
                save_field(state_file, f"prev_active_sig_{kind}", sig)
                save_field(state_file, f"prev_active_{kind}", int(is_active))
                continue
            if is_active and prev in ("", "0", 0, False):
                done = load_field(state_file, f"daily_summary_done_{kind}", "")
                if done != today:
                    ok = await self._do_daily_first_push(state_file, now, t)
                    if ok:
                        save_field(state_file, f"daily_summary_done_{kind}", today)
            save_field(state_file, f"prev_active_{kind}", int(is_active))

        # ── 常规：逐个"活跃且到点"的目标，按各自设置要一批、发、回报 ──
        pending_anywhere = False       # 有没有"内容但还不到点"，用来决定要不要走留档/闲聊兜底
        sent_any = False
        for t in active:
            s = t["settings"]
            kind = t["kind"]
            source = source_of(kind)
            last_t = load_field(state_file, f"last_push_ts_{kind}", 0.0)
            gap = load_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
            if last_t and (now - last_t) < gap:
                if self._channel_pending(source) > 0:
                    pending_anywhere = True
                continue
            daily_max = int(s.get("一天最多发几次", 0) or 0)
            if daily_max > 0 and daily_reached(state_file, daily_max):
                if self._channel_pending(source) > 0:
                    pending_anywhere = True
                continue

            batch = max(1, int(s.get("一次发几条", 1) or 1))
            items = self._channel_want(source, batch)
            if not items:
                continue
            # 有内容 → 重置各目标的"空内容"计数
            for other in targets:
                save_field(state_file, f"empty_cycles_{other['kind']}", 0)
            with_img = self._bool_setting(s, "带图", True)
            delivered = 0
            for item in items:
                news_text = str(item.get("zh") or item.get("raw") or "").strip()
                if not news_text:                     # 没正文的条目直接回报失败，别占着
                    self._channel_result(source, item.get("id"), False, "条目没有正文")
                    continue
                try:
                    ok = await self._speak_news(t["stream_id"], item, with_img)
                except Exception as e:
                    self.ctx.logger.warning(f"航小麦推送单条失败: {e}")
                    self._channel_result(source, item.get("id"), False, str(e))
                    continue
                # 注意：_speak_news 失败时是**返回 False**（内部吞了异常），不是抛出来。
                # 这里必须看返回值：谎报成功会让这条被写进"已发"，内容就永远丢了。
                if not ok:
                    self.ctx.logger.warning(f"航小麦推送失败（麦麦发送返回失败）: {source} "
                                            f"item={item.get('id')}")
                    self._channel_result(source, item.get("id"), False, "麦麦发送返回失败")
                    continue
                self._channel_result(source, item.get("id"), True)
                delivered += 1
            if not delivered:
                continue
            save_field(state_file, f"last_push_ts_{kind}", now)
            save_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
            record_daily_push(state_file)
            sent_any = True
            await asyncio.sleep(1.5)  # 私聊/群之间隔 1.5s，避免被 QQ 限流

        if sent_any:
            save_last_push(state_file, now)
        if sent_any or pending_anywhere:
            return

        # ── 兜底：内容都发完了 → 有留档发留档；都没有且连续 3 轮空才闲聊一句 ──
        archive = load_archive()
        for t in active:
            s = t["settings"]
            kind = t["kind"]
            last_t = load_field(state_file, f"last_push_ts_{kind}", 0.0)
            gap = load_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
            if last_t and (now - last_t) < gap:
                continue
            if archive:
                try:
                    ok = await self._send_one_archive_to(t)
                    if ok:
                        self.ctx.logger.info(f"航小麦兜底推送(留档旧闻 -> {kind})")
                        save_field(state_file, f"last_push_ts_{kind}", now)
                        save_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
                        save_field(state_file, f"empty_cycles_{kind}", 0)
                        record_daily_push(state_file)
                except Exception as e:
                    self.ctx.logger.warning(f"航小麦留档兜底失败({kind}): {e}")
            else:
                if not self._bool_setting(s, "闲聊", True):
                    continue
                empty = int(load_field(state_file, f"empty_cycles_{kind}", 0) or 0) + 1
                if empty >= 3:
                    try:
                        ok = await self._send_casual_to(t)
                        if ok:
                            self.ctx.logger.info(f"航小麦兜底推送(无消息闲聊 -> {kind})")
                            save_field(state_file, f"last_push_ts_{kind}", now)
                            save_field(state_file, f"next_gap_{kind}", self._next_random_interval(s))
                            save_field(state_file, f"empty_cycles_{kind}", 0)
                            record_daily_push(state_file)
                        else:
                            save_field(state_file, f"empty_cycles_{kind}", empty)
                    except Exception as e:
                        self.ctx.logger.warning(f"航小麦闲聊兜底失败({kind}): {e}")
                        save_field(state_file, f"empty_cycles_{kind}", empty)
                else:
                    save_field(state_file, f"empty_cycles_{kind}", empty)

    # ────────────────────────── 和看板渠道打交道（失败一律不抛出）──────────────────────────

    def _channel_want(self, source: str, count: int) -> list:
        """向看板要最多 count 条未发内容；失败/没连上返回 []。"""
        client = self._channel()
        try:
            return client.want(source, count) or []
        except Exception as e:
            self.ctx.logger.warning(f"航小麦取内容失败({source}): {e}")
            return []

    def _channel_pending(self, source: str) -> int:
        """问看板：这个目标还有多少条未发（决定要不要走兜底）。"""
        client = self._channel()
        try:
            return int(client.pending(source) or 0)
        except Exception:
            return 0

    def _channel_result(self, source: str, item_id, ok: bool, detail: str = "") -> None:
        """回报单条发送结果（ok=True 才会被看板写进库）。"""
        client = self._channel()
        try:
            client.send_result(source, item_id, ok, detail)
        except Exception as e:
            self.ctx.logger.warning(f"航小麦回报结果失败({source}/{item_id}): {e}")
