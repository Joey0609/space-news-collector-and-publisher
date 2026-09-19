# -*- coding: utf-8 -*-
"""QQ 开放平台 OpenAPI 封装：取 token → 分片上传图片 → 发群消息。

不依赖 botpy、不依赖常驻网关：纯 HTTP，可直接由脚本/计划任务调用。
"""
import hashlib
from pathlib import Path

import httpx

from .log import log
from .paths import HTTP_TIMEOUT, MD5_10M_BYTES

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


def get_token(host: str, appid: str, secret: str) -> str:
    """取 access_token（host 只影响日志语义，token 走统一入口）。"""
    r = httpx.post(TOKEN_URL, json={"appId": appid, "clientSecret": secret}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    tok = r.json().get("access_token", "")
    if not tok:
        raise RuntimeError(f"取 token 失败: {r.text[:200]}")
    return tok


def auth_headers(token: str, appid: str) -> dict:
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json",
            "X-Union-Appid": appid}


def _md5_10m(data: bytes) -> str:
    return hashlib.md5(data[:MD5_10M_BYTES]).hexdigest()


def upload_group_image(host: str, hdrs: dict, goid: str, path: str) -> str:
    """分片上传一张图到群，返回 file_info。

    步骤：upload_prepare → 每片 PUT 预签名 URL + upload_part_finish → files 合并拿 file_info。
    """
    p = Path(path)
    data = p.read_bytes()
    size = len(data)
    md5 = hashlib.md5(data).hexdigest()
    sha1 = hashlib.sha1(data).hexdigest()
    md5_10m = _md5_10m(data)
    fname = p.name
    log(f"[上传] {fname} ({size} bytes) 预上传...")

    r = httpx.post(f"{host}/v2/groups/{goid}/upload_prepare",
                   headers=hdrs,
                   json={"file_type": 1, "file_size": str(size), "file_name": fname,
                         "md5": md5, "sha1": sha1, "md5_10m": md5_10m},
                   timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"预上传失败 {r.status_code}: {r.text[:300]}")
    prep = r.json()
    upload_id = prep["upload_id"]
    parts = prep.get("parts", [])
    log(f"[上传] upload_id={upload_id} block_size={prep.get('block_size')} "
        f"parts={[(p.get('index'), p.get('block_size')) for p in parts]}")
    log(f"[上传] upload_id={upload_id} 分片数={len(parts)}")

    for order, part in enumerate(parts):
        idx = int(part["index"])
        bs = int(part["block_size"])
        chunk = data[order * bs:(order + 1) * bs]
        # 1) PUT 到预签名 URL（不带 QQ 鉴权头）
        pr = httpx.put(part["presigned_url"], content=chunk, timeout=HTTP_TIMEOUT)
        if pr.status_code not in (200, 201, 204):
            raise RuntimeError(f"分片{idx} PUT 失败 {pr.status_code}: {pr.text[:200]}")
        # 2) 通知分片完成
        fr = httpx.post(f"{host}/v2/groups/{goid}/upload_part_finish",
                        headers=hdrs,
                        json={"upload_id": upload_id, "part_index": idx,
                              "block_size": str(len(chunk)),
                              "md5": hashlib.md5(chunk).hexdigest()},
                        timeout=HTTP_TIMEOUT)
        if fr.status_code != 200:
            raise RuntimeError(f"分片{idx} finish 失败 {fr.status_code}: {fr.text[:200]}")
        log(f"[上传] 分片 {idx} 完成")

    # 3) 合并，取 file_info
    mr = httpx.post(f"{host}/v2/groups/{goid}/files",
                    headers=hdrs,
                    json={"file_type": 1, "srv_send_msg": False,
                          "file_name": fname, "upload_id": upload_id},
                    timeout=HTTP_TIMEOUT)
    if mr.status_code != 200:
        raise RuntimeError(f"合并失败 {mr.status_code}: {mr.text[:300]}")
    file_info = mr.json().get("file_info", "")
    if not file_info:
        raise RuntimeError(f"合并未返回 file_info: {mr.text[:300]}")
    log(f"[上传] 成功 file_info={file_info[:24]}...")
    return file_info


def send_group_rich(host: str, hdrs: dict, goid: str, text: str, file_info: str = "") -> bool:
    """发一条群消息：有图 msg_type=7（content + media 图文一条），无图 msg_type=0（纯文本）。"""
    if file_info:
        body = {"msg_type": 7, "content": text, "media": {"file_info": file_info}, "msg_seq": 1}
        tag = "图文一条"
    else:
        body = {"msg_type": 0, "content": text, "msg_seq": 1}
        tag = "纯文本"
    r = httpx.post(f"{host}/v2/groups/{goid}/messages", headers=hdrs,
                   json=body, timeout=HTTP_TIMEOUT)
    log(f"[发{tag}] {r.status_code} {r.text[:160]}")
    return r.status_code == 200
