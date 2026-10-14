# -*- coding: utf-8 -*-
"""hx_send.py · SendMixin —— 聊天流解析 + 底层发送（文字/图文/评论）
================================================================
职责边界：
    - 只负责"怎么把东西发出去"（拿 stream_id、拼消息段、调用 ctx.send）
    - "什么时候发" 在 hx_schedule.py，"发什么内容兜底" 在 hx_fallback.py

关键设计（别改坏）：
    - 不触发麦麦 planner，直接 ctx.send，保证稳定送达（planner 会加载海量上下文导致 400）
    - 一条内容发成两条：①来源行+译文(+图) ②麦麦的一句评论，中间停 2 秒
    - 图片超过 MAX_IMG_PER_MSG 张会自动另凑纯图消息（QQ 单条消息图片上限）
"""
import asyncio

if __package__ in (None, ""):
    from hx_llm import llm_text, maisaka_persona
    from hx_paths import MAX_IMG_PER_MSG
    from hx_render import build_source_line, img_to_base64, item_images
else:
    from .hx_llm import llm_text, maisaka_persona
    from .hx_paths import MAX_IMG_PER_MSG
    from .hx_render import build_source_line, img_to_base64, item_images


class SendMixin:
    """聊天流解析 + 发送。依赖 self.config / self.ctx。"""

    # ────────────────────────── 目标解析 ──────────────────────────

    @staticmethod
    def _extract_stream_id(resp) -> str:
        """从能力返回里稳健取 stream_id（兼容 {stream_id:..} 与 {stream:{stream_id:..}}）。"""
        if not isinstance(resp, dict):
            return ""
        sid = str(resp.get("stream_id") or "").strip()
        if sid:
            return sid
        inner = resp.get("stream")
        if isinstance(inner, dict):
            return str(inner.get("stream_id") or inner.get("session_id") or "").strip()
        return ""

    async def _resolve_stream_id(self) -> str:
        """拿目标 QQ 的 stream_id。优先已有会话，否则 open_session。"""
        target = self.config.push.target_qq
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(str(target), platform="qq")
            sid = self._extract_stream_id(stream)
            if sid:
                return sid
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

    async def _push_targets(self) -> list:
        """返回推送目标列表：[{kind, stream_id, settings}]，私聊/各群各带自己的生效设置。

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

    # ────────────────────────── 底层发送 ──────────────────────────

    async def _send_text_and_images(self, stream_id: str, text: str, image_paths: list) -> bool:
        """发一条文字 + 图片（超 MAX_IMG_PER_MSG 则另凑纯图消息）。返回首条是否成功。"""
        first_b64 = []
        for p in image_paths[:MAX_IMG_PER_MSG]:
            b = img_to_base64(p)
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
            segs = [{"type": "image", "content": b} for b in (img_to_base64(p) for p in chunk) if b]
            if not segs:
                continue
            try:
                await self.ctx.send.hybrid(segs, stream_id, return_details=True)
            except Exception as e:
                self.ctx.logger.warning(f"发送多出图片失败: {e}")
        return ok

    async def _send_text(self, stream_id: str, text: str) -> bool:
        """发一条纯文本，返回是否成功。"""
        try:
            r = await self.ctx.send.text(text, stream_id, return_details=True)
            return bool(r) if not isinstance(r, dict) else bool(r.get("sent"))
        except Exception as e:
            self.ctx.logger.warning(f"发送文本失败: {e}")
            return False

    async def _speak_news(self, stream_id: str, item: dict, with_img: bool = True) -> bool:
        """把一条新闻说给某个聊天流：①来源行+译文(+图) ②麦麦的一句评论。

        文案用麦麦真实人设轻量生成，不触发 planner，可稳定送达。
        """
        if not stream_id:
            return False
        translation = str(item.get("zh") or item.get("raw") or "").strip()
        if not translation:
            return False
        title = str(item.get("ai_title") or "").strip()
        source = build_source_line(item)
        text1 = "\n\n".join(part for part in (title, source, translation) if part)
        persona = maisaka_persona()
        prompt = (
            f"{persona}\n\n"
            "下面是刚给你的一条航天内容，请你用你平时说话的方式，写一句你自己的想法/评论。"
            "自然、真诚、别套话、别长篇大论，一两句就行。\n\n"
            f"内容：{translation[:500]}"
        )
        reply = llm_text(prompt, temperature=0.7, logger=self.ctx.logger) or "这条有点意思，你可以看看。"
        images = item_images(item) if with_img else []
        ok_main = await self._send_text_and_images(stream_id, text1, images)
        await asyncio.sleep(2)  # 原文/图之后稍作停顿再补评论
        ok_reply = await self._send_text(stream_id, reply)
        return ok_main or ok_reply
