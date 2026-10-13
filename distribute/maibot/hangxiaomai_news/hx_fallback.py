# -*- coding: utf-8 -*-
"""hx_fallback.py · FallbackMixin —— "没有合适内容时发什么" + 每日首推
==================================================================
三类兜底（都按目标 kind 各自独立执行、各自计时）：
    1) 每日首推：某目标"静默→活跃"的那一刻，向看板要最多 10 条（按分数取前 10）
       合成一张"总结卡"发出，再逐条回报结果
    2) 留档兜底：没有待发内容但 内容留档.json 有旧内容 → 发一条旧闻
    3) 闲聊兜底：既没新内容也没旧内容，连续 3 轮空 → 用麦麦人设随口说一句（受"闲聊"开关控制）
"""
import asyncio

if __package__ in (None, ""):
    from hx_llm import llm_text, maisaka_persona
    from hx_render import build_source_line, forward_line, item_images
    from hx_schedule import source_of
    from hx_state import record_daily_push, save_field
    from hx_store import load_archive, save_archive
else:
    from .hx_llm import llm_text, maisaka_persona
    from .hx_render import build_source_line, forward_line, item_images
    from .hx_schedule import source_of
    from .hx_state import record_daily_push, save_field
    from .hx_store import load_archive, save_archive


class FallbackMixin:
    """每日首推 / 留档旧闻 / 无内容闲聊。依赖 self.config / self.ctx 与调度状态。"""

    async def _do_daily_first_push(self, state_file, now: float, target: dict) -> bool:
        """单个目标的每日首推：向看板要最多 10 条 → 按分数取前 10 → 合成一条批量卡发出。

        发出的每条都回报 `ok=True`（看板据此写库）；发送失败则全部回报 `ok=False`，保持未发。
        """
        kind = target["kind"]
        source = source_of(kind)
        items = self._channel_want(source, 10)
        if not items:
            return False
        sorted_items = sorted(items, key=lambda x: x.get("score", 0) or 0, reverse=True)
        top = sorted_items[:10]
        with_img = self._bool_setting(target["settings"], "带图", True)
        sent = await self._send_summary([target["stream_id"]], top, with_img)
        if sent <= 0:
            self.ctx.logger.warning("每日首推发送失败，条目保持未发状态待下轮重试")
            for item in top:
                self._channel_result(source, item.get("id"), False, "每日首推发送失败")
            return False
        for item in top:
            self._channel_result(source, item.get("id"), True)
        save_field(state_file, f"last_push_ts_{kind}", now)
        save_field(state_file, f"next_gap_{kind}", self._next_random_interval(target["settings"]))
        record_daily_push(state_file)
        self.ctx.logger.info(f"航小麦每日首推({kind})：前{len(top)}条已汇总发送")
        return True

    async def _send_summary(self, streams: list, top_items: list, with_img: bool = True) -> int:
        """每日首推：发两条 —— ①麦麦俏皮开场白 + 各条「来源行+译文」逐行 + 各图(超上限拆消息)；
        ②麦麦综述(按条分行)。返回成功的目标数。"""
        if not streams:
            return 0
        persona = maisaka_persona()
        opening = llm_text(
            f"{persona}\n\n现在是每日航天新闻总结时间，请用你平时说话的方式，写一句俏皮、有个性的开场白，"
            "类似'听赛博牢A给你每日夜间新闻总结！'这种感觉，但更有意思一点，别套模板。只输出那一句开场白。",
            temperature=0.9, logger=self.ctx.logger,
        ).strip() or "听赛博牢A给你每日夜间新闻总结！"

        # 目录（各条 来源行 + 译文；转推附转发者评论）
        lines = []
        for it in top_items:
            src = build_source_line(it)
            body = str(it.get("zh") or it.get("raw") or "").strip()
            line = (src + "\n" + body) if src else body
            fl = forward_line(it, self.ctx.logger)
            if fl:
                line += "\n" + fl
            lines.append(line)
        card_text = opening + "\n\n" + "\n———\n".join(lines)

        all_images = []
        if with_img:
            for it in top_items:
                all_images.extend(item_images(it))

        summary = llm_text(
            f"{persona}\n\n你在执行用户交给你的任务：帮用户把他想看的航天内容挑出来并推送。"
            "下面是你今天挑出的、值得看的内容，请按条分成几点、每点一行，用你平时说话的方式简单说几句，"
            "别写成一整段、别太长。\n\n内容：\n"
            + "\n".join(f"- {(str(it.get('zh') or it.get('raw') or ''))[:80]}" for it in top_items),
            temperature=0.7, logger=self.ctx.logger,
        ) or "今天给你挑了几条航天内容，配图发你，感兴趣可以看看～"

        sent = 0
        for sid in streams:
            try:
                ok1 = await self._send_text_and_images(sid, card_text, all_images)
                if not ok1:
                    self.ctx.logger.warning(f"每日首推目录发送失败(可能消息/图被拒): {sid}")
                await asyncio.sleep(2)  # 目录发出后稍停顿，再补综述
                await self._send_text(sid, summary)
                if ok1:
                    sent += 1
            except Exception as e:
                self.ctx.logger.warning(f"每日首推发送失败: {e}")
        return sent

    async def _send_one_archive_to(self, t: dict) -> bool:
        """取一条留档旧闻（队首），按该目标的带图设置发给它。"""
        archive = load_archive()
        if not archive:
            return False
        item = archive.pop(0)
        save_archive(archive)
        with_img = self._bool_setting(t["settings"], "带图", True)
        return await self._speak_news(t["stream_id"], item, with_img)

    async def _send_casual_to(self, t: dict) -> bool:
        """没新内容也没旧闻时，用麦麦人设生成一句闲聊发给这个目标。"""
        persona = maisaka_persona()
        prompt = (
            f"{persona}\n\n"
            "现在航天圈暂时没什么大新闻，你像往常一样，用你自己平时的话，随口跟对方说点什么"
            "（可以聊聊你最近在蹲什么、随口吐槽两句没动静、说说你好奇的航天话题，哪怕一句日常也行）。"
            "要求：别像模板、别套话，就一两句，自然、有人味，像你平时的样子。\n\n"
            "你现在开口说："
        )
        reply = llm_text(prompt, temperature=0.95, logger=self.ctx.logger) or "先忙你的啦，有航天好玩的我马上喊你。"
        return await self._send_text(t["stream_id"], reply)
