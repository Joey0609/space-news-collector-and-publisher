"""新浪科技标题预筛；其他来源不使用本词库。"""
from __future__ import annotations

import re
import unicodedata

# 中文用子串，英文用词边界；统一大小写、全角字符和连接符。
KEYWORD_GROUPS = {
    "中文通用": "航天 宇航 太空 外太空 空间探索 商业航天 载人航天 航天器 航天员 宇航员 火箭 运载火箭 卫星 人造卫星 卫星互联网 卫星通信 卫星导航 卫星遥感 卫星星座 星间链路 空间站 空间望远镜 空间探测器 深空探测 探月 登月 探火 月球 月面 月背 火星 小行星 彗星 系外行星 空间碎片 太空垃圾 轨道碎片 太空旅游 太空经济".split(),
    "技术任务": "入轨 变轨 离轨 脱轨 再入 返回舱 返回器 着陆器 月球车 火星车 空间交会 交会对接 在轨服务 在轨制造 在轨加注 地月 近地轨道 低地球轨道 地球同步轨道 太阳同步轨道 液氧甲烷 液氧煤油 火箭发动机 可重复使用火箭 可回收火箭".split(),
    "中国机构企业": "国家航天局 中国航天 航天科技 航天科工 中国星网 中国卫通 中国卫星 银河航天 蓝箭航天 星际荣耀 星河动力 天兵科技 东方空间 中科宇航 深蓝航天 星际智航 箭元科技 星河探索 时空道宇 微纳星空 长光卫星 天仪研究院 航天驭星 国星宇航 垣信卫星 九天微星 零壹空间".split(),
    "中国型号项目": "长征 神舟 天舟 天宫 嫦娥 天问 北斗 鹊桥 玉兔 祝融 悟空号 墨子号 慧眼号 爱因斯坦探针 夸父一号 实践卫星 试验卫星 风云卫星 高分卫星 吉林一号 朱雀 双曲线 谷神星 智神星 天龙 引力一号 力箭 星云火箭 千帆星座 国网星座".split(),
    "国际机构企业": ["NASA", "ESA", "JAXA", "ISRO", "CNSA", "Roscosmos", "SpaceX", "Space X", "Blue Origin", "Rocket Lab", "ULA", "United Launch Alliance", "Arianespace", "ArianeGroup", "Firefly Aerospace", "Relativity Space", "Stoke Space", "Sierra Space", "Axiom Space", "Intuitive Machines", "Astrobotic", "ispace", "Planet Labs", "Maxar", "Iridium", "Eutelsat", "OneWeb", "Telesat", "Viasat", "AST SpaceMobile", "Satellogic", "Thales Alenia Space", "Airbus Defence and Space", "Northrop Grumman", "Lockheed Martin"],
    "国际中文别称": "美国宇航局 美国航天局 欧洲航天局 日本宇宙航空研究开发机构 印度空间研究组织 俄罗斯航天 蓝色起源 火箭实验室 联合发射联盟 阿丽亚娜 阿里安 萤火虫航天 相对论空间 公理空间 直觉机器 诺斯罗普 诺斯洛普 洛克希德 铱星 欧卫".split(),
    "国际型号项目": ["Starship", "星舰", "Falcon", "猎鹰", "Dragon", "龙飞船", "Crew Dragon", "载人龙", "Starlink", "星链", "Super Heavy", "超级重型", "New Glenn", "新格伦", "New Shepard", "新谢泼德", "Electron", "电子号火箭", "Neutron", "中子号火箭", "Vulcan", "火神火箭", "Atlas V", "宇宙神", "Ariane", "Vega", "织女星火箭", "Soyuz", "联盟号", "Progress", "进步号", "Starliner", "星际客机", "Dream Chaser", "追梦者", "Artemis", "阿尔忒弥斯", "Orion", "猎户座飞船", "Gateway", "月球门户", "ISS", "国际空间站", "Hubble", "哈勃", "JWST", "James Webb", "韦布", "Euclid", "欧几里得", "JUICE", "Europa Clipper", "欧罗巴快船", "BepiColombo", "OSIRIS-REx", "Perseverance", "毅力号", "Curiosity", "好奇号", "Voyager", "旅行者号", "Project Kuiper", "Amazon Leo", "柯伊伯计划"],
    "英文通用": ["spaceflight", "spacecraft", "spaceship", "space station", "space telescope", "space exploration", "space mission", "space debris", "space tourism", "space economy", "commercial space", "rocket", "satellite", "satcom", "cubesat", "smallsat", "nanosatellite", "microsatellite", "megaconstellation", "astronaut", "cosmonaut", "taikonaut", "lunar", "moon", "mars", "asteroid", "comet", "exoplanet", "interplanetary", "cislunar", "orbital", "suborbital", "launch vehicle", "launch pad", "launch site", "spaceport", "lander", "rover", "reentry", "re-entry", "spacewalk", "extravehicular", "microgravity", "zero gravity", "in-orbit", "on-orbit"],
}


def normalize_title(title):
    text = unicodedata.normalize("NFKC", str(title or "")).casefold()
    text = re.sub(r"[-‐‑‒–—―−_]", " ", text)
    return " ".join(text.split())


SPACE_KEYWORDS = tuple(dict.fromkeys(word for words in KEYWORD_GROUPS.values() for word in words))
_CHINESE = tuple(word for word in SPACE_KEYWORDS if re.search(r"[\u4e00-\u9fff]", word))
_ENGLISH = tuple(word for word in SPACE_KEYWORDS if word not in _CHINESE)
_PATTERNS = tuple(re.compile(r"(?<![a-z0-9])" + re.escape(normalize_title(word)) + r"(?:s|es)?(?![a-z0-9])")
                  for word in _ENGLISH)
_CONTEXT = re.compile(r"航天|宇航|太空|火箭|卫星|轨道|空间站|(?<![a-z0-9])(?:space|launch|orbit|satellite|rocket)(?:s)?(?![a-z0-9])")
_CONTEXT_NAMES = re.compile(r"亚马逊|波音|空客|华为|马斯克|(?<![a-z0-9])(?:amazon|boeing|airbus|huawei|musk|sls|leo|geo|sar)(?![a-z0-9])")


def matches_title(title):
    text = normalize_title(title)
    return (any(word in text for word in _CHINESE)
            or any(pattern.search(text) for pattern in _PATTERNS)
            or bool(_CONTEXT_NAMES.search(text) and _CONTEXT.search(text)))


def accepts_candidate(source, payload):
    """发现与旧候选回收共用规则，避免旧候选绕过新预筛。"""
    return source != "sina" or matches_title(payload.get("title"))
