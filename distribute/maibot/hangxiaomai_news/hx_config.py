# -*- coding: utf-8 -*-
"""hx_config.py · 插件配置模型 + "生效设置"计算
============================================
配置来源三层，后者覆盖前者：
    1) BUILTIN_PUSH_DEFAULTS                     插件内置默认（代码里）
    2) 配置/推送默认.toml  [push]                 管理员基线
    3) 配置/推送偏好_<QQ>.toml  /  推送偏好_群<群号>.toml  [push]   个人/每群偏好

配置文件的命名约定（航小麦/配置/）：
    推送默认.toml              全局基线           [push]
    推送偏好_<QQ>.toml         某人的推送方式      [push]
    推送偏好_群<群号>.toml      某群的推送方式      [push]

ConfigMixin 提供的方法都以 self.config / self.ctx 为前提，由插件类继承使用。
"""
from pathlib import Path

from maibot_sdk import Field, PluginConfigBase

if __package__ in (None, ""):
    from hx_paths import BUILTIN_PUSH_DEFAULTS, CONFIG_DIR, PUSH_DEFAULT_FILE
    from hx_toml import read_toml
    from hx_util import as_bool
else:
    from .hx_paths import BUILTIN_PUSH_DEFAULTS, CONFIG_DIR, PUSH_DEFAULT_FILE
    from .hx_toml import read_toml
    from .hx_util import as_bool


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
    target_groups: list[str] = Field(default_factory=lambda: ["YOUR_GROUP_ID_1"],
                                     description="推送到哪些QQ群(群号列表)")
    poll_interval: int = Field(default=30, description="轮询分发队列(数据库)的间隔(秒)")
    auto_switch: bool = Field(default=True, description="是否遵守功能开关(新闻推送 off 则不推)")


class HangxiaomaiNewsPluginConfig(PluginConfigBase):
    plugin: InitConfig = Field(default_factory=InitConfig)
    push: PushConfig = Field(default_factory=PushConfig)


class ConfigMixin:
    """配置读取与"生效设置"计算（不涉及调度/发送）。"""

    # ── 基础读取 ──

    def _read_toml(self, path: Path) -> dict:
        return read_toml(path)

    # ── 各配置文件的路径 ──

    def _user_pref_path(self, user_id: str) -> Path:
        return CONFIG_DIR / f"推送偏好_{user_id}.toml"

    def _group_pref_path(self, group_id: str) -> Path:
        return CONFIG_DIR / f"推送偏好_群{group_id}.toml"

    # ── 生效设置 ──

    def _effective_settings(self, user_id: str) -> dict:
        """该用户实际的推送设置 = 内置默认 + 推送默认.toml + 用户偏好(覆盖)。"""
        settings = dict(BUILTIN_PUSH_DEFAULTS)
        settings.update(self._read_toml(PUSH_DEFAULT_FILE).get("push", {}))
        settings.update(self._read_toml(self._user_pref_path(user_id)).get("push", {}))
        return settings

    def _effective_settings_for(self, kind: str) -> dict:
        """按目标(私聊/群)取各自的生效设置；私聊与各群互相独立。

        kind: "private" 或 "group_<群号>"。
        """
        base = dict(BUILTIN_PUSH_DEFAULTS)
        base.update(self._read_toml(PUSH_DEFAULT_FILE).get("push", {}))
        if kind.startswith("group_"):
            gid = kind[len("group_"):]
            base.update(self._read_toml(self._group_pref_path(gid)).get("push", {}))
        else:
            base.update(self._read_toml(
                self._user_pref_path(str(self.config.push.target_qq or ""))).get("push", {}))
        return base

    def _bool_setting(self, settings: dict, key: str, default: bool) -> bool:
        """读布尔设置（兼容 true/1/yes/on 与 0/1）；键不存在时按 default 判。"""
        if key not in settings:
            return bool(default)
        return as_bool(settings.get(key))
