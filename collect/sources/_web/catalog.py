"""来自 AutoSpaceNews 的订阅入口清单；每个订阅独立去重。"""

RSS_FEEDS = (
    ("spacenews", "SpaceNews", "https://spacenews.com/feed/"),
    ("nasa_breaking", "NASA Breaking", "https://www.nasa.gov/news-release/feed/"),
    ("space_com", "Space.com", "https://www.space.com/feeds.xml"),
    ("spaceflight_now", "Spaceflight Now", "https://spaceflightnow.com/feed/"),
    ("ars_space", "Ars Technica Space", "https://arstechnica.com/space/feed/"),
    ("nasaspaceflight", "NASASpaceFlight", "https://www.nasaspaceflight.com/feed/"),
    ("spaceq", "SpaceQ", "https://spaceq.ca/feed/"),
    ("satnews", "SatNews", "https://satnews.com/feed/"),
    ("space_policy_online", "Space Policy Online", "https://spacepolicyonline.com/feed/"),
    ("keeptrack", "KeepTrack.Space", "https://www.keeptrack.space/rss.xml"),
    ("defensescoop", "DefenseScoop", "https://defensescoop.com/feed/"),
    ("air_space_forces", "Air & Space Forces", "https://www.airandspaceforces.com/feed/"),
    ("bbc_science", "BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ("fox_science", "Fox News Science", "https://moxie.foxnews.com/google-publisher/science.xml"),
    ("guardian_space", "The Guardian Science", "https://www.theguardian.com/science/space/rss"),
)
SNAPI_URL = "https://api.spaceflightnewsapi.net/v4/articles/?limit=100&ordering=-published_at"
SINA_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=50&page=1"

# 优先站点文章容器，最后才用通用 article；禁止用整个 main/body 猜正文。
BODY_SELECTORS = {
    "defensescoop.com": (".single-article__content-inner",),
    "airandspaceforces.com": (".post-body",),
    "nasa.gov": (".entry-content", ".single-content", "article"),
    "space.com": ("#article-body", ".article-body"),
    "arstechnica.com": (".article-content", ".post-content", ".entry-content"),
    "bbc.com": ('[data-component="text-block"]',),
    "bbc.co.uk": ('[data-component="text-block"]',),
    "foxnews.com": (".article-body",),
    "theguardian.com": ("#maincontent .article-body-commercial-selector", ".article-body-commercial-selector"),
    "sina.com.cn": ("#artibody", "#article", ".article-content"),
}
