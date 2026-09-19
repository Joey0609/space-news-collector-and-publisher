# -*- coding: utf-8 -*-
"""艾莉森网关内部库（ali_core）
==============================

艾莉森 = QQ 官方机器人通道（botpy WebSocket 收事件 + OpenAPI HTTP 主动发消息）。
本包放可复用的零件，入口脚本只做接线：

    艾莉森网关/推送.py          纯转述图文发送器（主入口，带 CLI）
    艾莉森网关/临时定时推送.py  每 N 分钟发一条"最新的、本群没发过的"
    艾莉森网关/gateway.py       常驻网关：连 WebSocket、记录 group_openid/user_openid
    艾莉森网关/send_test.py     诊断：OpenAPI 直发一条
    艾莉森网关/send_botpy.py    诊断：botpy API 直发一条
    艾莉森网关/try_variants.py  诊断：主动消息参数变体试验

模块分工：
    paths      路径与常量（唯一的"文件在哪"出处）
    log        日志（推送.log）
    env        .env / state.json 读写
    jsonio     JSON 读写
    qq_api     QQ 开放平台 OpenAPI（token / 分片上传图片 / 发群消息）
    render     文本与图片排版（来源行、正文、候选挑选）
"""
