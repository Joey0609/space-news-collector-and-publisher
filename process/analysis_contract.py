"""完整分析与看板共同使用的唯一字段契约。"""
import json

def missing_analysis_fields(item):
    missing = []
    for key, label in (("zh", "译文"), ("ai_title", "标题"), ("ai_summary", "摘要")):
        if not str(item.get(key) or "").strip(): missing.append(label)
    if item.get("importance") not in {"高", "中", "低"}: missing.append("重要度")
    score = item.get("ai_score")
    if type(score) is not int or not 0 <= score <= 100: missing.append("AI分")
    tags = item.get("ai_tags")
    if isinstance(tags, str):
        try: tags = json.loads(tags)
        except (ValueError,TypeError): tags = None
    if not isinstance(tags,list) or not tags or any(not isinstance(t,str) or not t.strip() for t in tags): missing.append("标签")
    decision = item.get("decision")
    reason = str(item.get("invalid_reason") or "").strip()
    if decision not in {"发", "丢"}: missing.append("最终判定")
    elif (decision == "丢" and not reason) or (decision == "发" and (reason or item.get("repeat_of"))): missing.append("无效原因")
    if str(item.get("quoted_raw") or "").strip() and not str(item.get("quoted_zh") or "").strip(): missing.append("引用译文")
    if any(mark in str(item.get("zh") or "").lower() for mark in ("http://", "https://", "www.", "information as of", "直播观看")): missing.append("正文清理")
    return missing

def processing_required(item):
    return item.get("stage") != "processed" or bool(missing_analysis_fields(item))
