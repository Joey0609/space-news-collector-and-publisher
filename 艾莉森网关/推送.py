# -*- coding: utf-8 -*-
"""
艾莉森推送 v1 · 纯转述图文发送器
================================
作用：从 配置\待推送.json 取一条"还没发给本群"的真新闻，
     用艾莉森（QQ 官方接口）把「来源行 + 正文 (+图)」**原样转述**到指定群。
     艾莉森不写文案（符合「★ 新架构」）。

不依赖常驻网关：走纯 HTTP OpenAPI（取 token → 上传图 → 发消息）。

用法（在 艾莉森网关 目录下）：
    venv\\Scripts\\python.exe 推送.py --list            只列出候选，不发送
    venv\\Scripts\\python.exe 推送.py --dry              演练：只打印要发的内容，不真发
    venv\\Scripts\\python.exe 推送.py --once             取1条（优先带图）真发到群
    venv\\Scripts\\python.exe 推送.py --once --no-image  取1条纯文字真发
    venv\\Scripts\\python.exe 推送.py --once --index 3   指定待推送.json 里第 3 条
"""
import sys
import json
import time
import hashlib
import datetime
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
HX_ROOT = HERE.parent                      # 航小麦
PENDING_FILE = HX_ROOT / "配置" / "待推送.json"
SENT_EVER_FILE = HX_ROOT / "配置" / "已发记录.json"
STATE_FILE = HERE / "state.json"
ENV_FILE = HERE / ".env"
LOG_FILE = HERE / "logs" / "推送.log"

# 目标群（群号用于 sent_to 标记；openid 从 state.json 取）
TARGET_GROUP_NUMBER = "YOUR_GROUP_ID_2"
TARGET_KIND = f"group_{TARGET_GROUP_NUMBER}"

MD5_10M_BYTES = 10002432
HTTP_TIMEOUT = 60.0


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_env() -> dict:
    env = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_token(host: str, appid: str, secret: str) -> str:
    r = httpx.post("https://bots.qq.com/app/getAppAccessToken",
                   json={"appId": appid, "clientSecret": secret}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    tok = r.json().get("access_token", "")
    if not tok:
        raise RuntimeError(f"取 token 失败: {r.text[:200]}")
    return tok


def auth_headers(token: str, appid: str) -> dict:
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json",
            "X-Union-Appid": appid}


def fmt_beijing(date_str: str, ts) -> str:
    d = None
    if ts:
        try:
            d = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        except Exception:
            d = None
    if d is None and date_str:
        try:
            s = str(date_str).strip().replace("T", " ").replace("+00:00", "").strip()
            d = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            d = None
    if d is None:
        return ""
    bj = d.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return "UTC+8-" + bj.strftime("%Y-%m-%d-%H:%M")


def build_source_line(item: dict) -> str:
    account = str(item.get("orig_account", "") or item.get("account", "") or "")
    platform = str(item.get("platform", "") or "X")
    date = fmt_beijing(str(item.get("date", "") or ""), item.get("ts"))
    if not account and not date:
        return ""
    parts = [f"@{account}"] if account else []
    if platform:
        parts.append(platform)
    if date:
        parts.append(date)
    return "来源：" + " · ".join(parts)


def build_text(item: dict) -> str:
    src = build_source_line(item)
    body = str(item.get("zh") or item.get("raw") or "").strip()
    text = (src + "\n\n" + body) if src else body
    fc = str(item.get("forward_comment", "") or "").strip()
    if fc:
        fa = str(item.get("account", "") or "")
        text += "\n\n" + (f"转发@{fa}：" + fc if fa else "转发者：" + fc)
    return text


def item_images(item: dict) -> list:
    imgs = item.get("images", [])
    if not isinstance(imgs, list):
        imgs = []
    imgs = [str(x) for x in imgs if str(x).strip()]
    if not imgs:
        single = str(item.get("image", "") or "")
        if single:
            imgs = [single]
    return imgs


def _md5_10m(data: bytes) -> str:
    return hashlib.md5(data[:MD5_10M_BYTES]).hexdigest()


def upload_group_image(host: str, hdrs: dict, goid: str, path: str) -> str:
    """分片上传一张图到群，返回 file_info。"""
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
    """发一条群消息：有图则 msg_type=7（content + media，图文一条），无图则 msg_type=0（纯文本）。"""
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


def pick_item(items: list, index=None):
    """选一条还没发给本群的条目；优先带图、分数高。"""
    if index is not None:
        if 0 <= index < len(items):
            return index, items[index]
        return None, None
    cands = []
    for i, it in enumerate(items):
        sent_to = it.get("sent_to", [])
        if not isinstance(sent_to, list):
            sent_to = []
        if TARGET_KIND in sent_to:
            continue
        has_img = 1 if item_images(it) else 0
        cands.append((has_img, it.get("score", 0) or 0, i))
    if not cands:
        return None, None
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[0][2], items[cands[0][2]]


def main():
    args = sys.argv[1:]
    do_list = "--list" in args
    do_dry = "--dry" in args
    do_once = "--once" in args
    no_image = "--no-image" in args
    index = None
    if "--index" in args:
        try:
            index = int(args[args.index("--index") + 1])
        except Exception:
            index = None

    env = load_env()
    appid = env.get("APPID", "").strip()
    secret = env.get("SECRET", "").strip()
    sandbox = str(env.get("SANDBOX", "0")).strip().lower() in ("1", "true", "yes", "on")
    host = "https://sandbox.api.sgroup.qq.com" if sandbox else "https://api.sgroup.qq.com"

    state = load_state()
    goid = state.get("group_openid", "").strip()
    log("=" * 50)
    log(f"艾莉森推送 v1 | 目标群 {TARGET_GROUP_NUMBER} openid={goid} | 环境={'沙箱' if sandbox else '正式'}")
    if not goid:
        log("❌ state.json 里没有 group_openid：先在群里 @ 一下艾莉森让网关记录")
        sys.exit(1)

    # 只测"上传图片"：推送.py --upload-only <图片路径>
    if "--upload-only" in args:
        path = args[args.index("--upload-only") + 1]
        token = get_token(host, appid, secret)
        hdrs = auth_headers(token, appid)
        try:
            fi = upload_group_image(host, hdrs, goid, path)
            log(f"[upload-only] 成功 file_info={fi[:24]}...")
        except Exception as e:
            log(f"[upload-only] 失败: {e}")
        return

    items = load_json(PENDING_FILE, [])
    log(f"待推送.json 共 {len(items)} 条")
    idx, item = pick_item(items, index)

    if do_list or item is None:
        cnt = sum(1 for it in items if TARGET_KIND not in (it.get("sent_to") or []))
        log(f"尚未发到本群的候选：{cnt} 条")
        if do_list:
            for i, it in enumerate(items):
                if TARGET_KIND in (it.get("sent_to") or []):
                    continue
                log(f"  #{i} score={it.get('score')} 图={len(item_images(it))} "
                    f"@{it.get('account')} {str(it.get('zh') or '')[:40]}")
        if item is None and not do_list:
            log("没有可发候选（都已发过本群）")
        return

    text = build_text(item)
    images = [] if no_image else item_images(item)
    log(f"选中 #{idx} @{item.get('account')} score={item.get('score')} 图={len(images)}")
    log("-" * 40)
    log(text)
    log("-" * 40)
    if do_dry:
        log("[dry] 演练模式，未真发")
        return
    if not do_once:
        log("未指定 --once，仅预览。加 --once 真发。")
        return

    token = get_token(host, appid, secret)
    hdrs = auth_headers(token, appid)

    # 图文一条：上传第一张图 → msg_type=7 带 content + media
    file_info = ""
    if images:
        try:
            file_info = upload_group_image(host, hdrs, goid, images[0])
        except Exception as e:
            log(f"[图] 上传失败({images[0]}): {e}")
    ok = send_group_rich(host, hdrs, goid, text, file_info)

    if ok:
        # 标记已发本群 + 记已发记录
        sent_to = item.get("sent_to", [])
        if not isinstance(sent_to, list):
            sent_to = []
        if TARGET_KIND not in sent_to:
            sent_to.append(TARGET_KIND)
        item["sent_to"] = sent_to
        save_json(PENDING_FILE, items)
        records = load_json(SENT_EVER_FILE, [])
        records.append(item)
        save_json(SENT_EVER_FILE, records[-500:])
        log(f"✅ 已发送并标记 {TARGET_KIND}")
    else:
        log("❌ 发送失败（文字和图都没成功），未标记")


if __name__ == "__main__":
    main()
