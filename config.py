import pytz
from typing import Dict, List, Tuple

LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

CLIENT_GEO_TTL = 300.0
LOCAL_TIMEZONE = pytz.timezone("America/Chicago")

BOTS = { "googlebot":"Googlebot","bingbot":"Bingbot","twitterbot":"Twitterbot","facebookexternalhit":"FacebookBot",
         "duckduckbot":"DuckDuckBot","baiduspider":"Baiduspider","yandexbot":"YandexBot",
         "ia_archiver":"Alexa/Archive.org","gptbot":"ChatGPT-Bot","perplexitybot":"PerplexityAI"}

social_platforms = { "facebook.com": "Facebook", "fb.com": "Facebook", "twitter.com": "Twitter/X", "t.co": "Twitter/X","snapchat.com": "Snapchat",
                         "x.com": "Twitter/X", "instagram.com": "Instagram", "linkedin.com": "LinkedIn", "reddit.com": "Reddit","telegram.org": "Telegram",
                         "pinterest.com": "Pinterest",  "tiktok.com": "TikTok", "youtube.com": "YouTube", "discord.com": "Discord", "whatsapp.com": "WhatsApp" }

search_engines = { "google.com": "Google Search","bing.com": "Bing Search", "yahoo.com": "Yahoo Search", 
                    "baidu.com": "Baidu", "yandex.com": "Yandex", "ask.com": "Ask.com", "duckduckgo.com": "DuckDuckGo"}
