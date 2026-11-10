# ┌────────────────────────────────────────────────────────────────────────┐
# │                         automation/sites.py                            │
# │      The website registry: every service a person names out loud       │
# └────────────────────────────────────────────────────────────────────────┘
"""
One line per website. `targets.WEBSITE_REGISTRY` is a thin view over `SITES` (Windows and the
resolver read it); `web_macros` reads the extra fields (category, search template).

Fields:
    url        where "open X" goes
    aliases    other names people SAY ("google sheet", "sheets", "spreadsheets")
    titles     fragments of the browser window title when the site is on screen ("close X")
    category   a coarse group: google, microsoft, work, ai, dev, social, video, music, shop,
               food, travel, money, learn, news, list
    search     a search URL with {q}, where the site has one ("search Amazon for earbuds")

Rules:
  * This is an alias table, not a fuzzy matcher. A near-miss between two sites is a wrong
    destination, so `targets.canonical_website` only normalises GRAMMAR (singular/plural,
    "the … website", "my …"), never spelling.
  * "spotify" is deliberately NOT a name here: the desktop app owns it; the web player is
    "spotify web". Same for "whatsapp" (an app on Windows, an Omarchy web app on Linux) —
    the web client is "whatsapp web". Pinned by the test suites.
  * A name that is also one of the user's FOLDERS ("documents", "photos", "music") is never
    an alias here — "open my photos" is ~/Pictures, not Google Photos.
  * Every URL is https (pinned by tests). Browser pages (chrome://…) are handled by the
    extension's own commands, not by this table.
  * Category "list" entries are the user's OWN lists ("my sheets", "watch later", "my orders").
  * Adding a site is one line. No network, no model, no import-time work beyond the dict.
"""

_G = "https://www.google.com/search?q={q}"


def _s(url, aliases=(), titles=(), category="web", search=None):
    return {"url": url, "aliases": tuple(aliases), "titles": tuple(titles),
            "category": category, "search": search}


SITES = {
    # ── the original 23 (keys and URLs unchanged; pinned by tests) ──
    "youtube": _s("https://www.youtube.com/", ("yt", "you tube"), ("youtube", "youtu.be"),
                  "video", "https://www.youtube.com/results?search_query={q}"),
    "youtube music": _s("https://music.youtube.com/", ("yt music",), ("youtube music",),
                        "music", "https://music.youtube.com/search?q={q}"),
    "google": _s("https://www.google.com/", ("google search", "google.co.in"),
                 ("google search",), "google", _G),
    "gmail": _s("https://mail.google.com/", ("google mail", "my mail", "my email", "email",
                                              "mail", "my inbox", "inbox", "e mail"),
                ("gmail", "google mail", "inbox"), "google",
                "https://mail.google.com/mail/u/0/#search/{q}"),
    "github": _s("https://github.com/", ("git hub",), ("github",), "dev",
                 "https://github.com/search?q={q}"),
    "chatgpt": _s("https://chatgpt.com/", ("chat gpt", "openai", "open ai", "chatgpt ai"),
                  ("chatgpt", "chat.openai"), "ai"),
    "claude": _s("https://claude.ai/", ("claude ai", "anthropic claude"), ("claude",), "ai"),
    "linkedin": _s("https://www.linkedin.com/", ("linked in",), ("linkedin",), "social",
                   "https://www.linkedin.com/search/results/all/?keywords={q}"),
    "twitter": _s("https://x.com/", ("tweeter", "x", "x.com", "twitter x"), ("twitter", "x.com"),
                  "social", "https://x.com/search?q={q}"),
    "reddit": _s("https://www.reddit.com/", (), ("reddit",), "social",
                 "https://www.reddit.com/search/?q={q}"),
    "netflix": _s("https://www.netflix.com/", (), ("netflix",), "video",
                  "https://www.netflix.com/search?q={q}"),
    "instagram": _s("https://www.instagram.com/", ("insta", "ig"), ("instagram",), "social"),
    "facebook": _s("https://www.facebook.com/", ("fb",), ("facebook",), "social",
                   "https://www.facebook.com/search/top?q={q}"),
    "amazon": _s("https://www.amazon.in/", ("amazon india", "amazon.in", "amazon shopping"),
                 ("amazon",), "shop", "https://www.amazon.in/s?k={q}"),
    "wikipedia": _s("https://www.wikipedia.org/", ("wiki",), ("wikipedia",), "learn",
                    "https://en.wikipedia.org/w/index.php?search={q}"),
    "stackoverflow": _s("https://stackoverflow.com/", ("stack overflow",),
                        ("stack overflow", "stackoverflow"), "dev",
                        "https://stackoverflow.com/search?q={q}"),
    "twitch": _s("https://www.twitch.tv/", (), ("twitch",), "video",
                 "https://www.twitch.tv/search?term={q}"),
    "maps": _s("https://www.google.com/maps", ("google maps", "google map", "map"),
               ("google maps",), "google", "https://www.google.com/maps/search/{q}"),
    "translate": _s("https://translate.google.com/", ("google translate", "translator"),
                    ("google translate",), "google"),
    "drive": _s("https://drive.google.com/", ("google drive", "my drive", "gdrive"),
                ("google drive", "drive.google"), "google",
                "https://drive.google.com/drive/search?q={q}"),
    "docs": _s("https://docs.google.com/document/", ("google docs", "google doc",
                                                     "google documents"),
               ("google docs", "docs.google"), "google",
               "https://docs.google.com/document/u/0/?q={q}"),
    "whatsapp web": _s("https://web.whatsapp.com/", ("web whatsapp", "whatsapp on the web"),
                       ("whatsapp",), "social"),
    "spotify web": _s("https://open.spotify.com/", ("web spotify", "spotify web player",
                                                    "spotify in the browser"),
                      ("spotify",), "music", "https://open.spotify.com/search/{q}"),

    # ── Google ──
    "sheets": _s("https://docs.google.com/spreadsheets/", ("google sheets", "google sheet",
                                                           "spreadsheets", "spreadsheet",
                                                           "google spreadsheet", "my spreadsheets"),
                 ("google sheets",), "google", "https://docs.google.com/spreadsheets/u/0/?q={q}"),
    "slides": _s("https://docs.google.com/presentation/", ("google slides", "google slide",
                                                           "presentations", "google presentation"),
                 ("google slides",), "google", "https://docs.google.com/presentation/u/0/?q={q}"),
    "forms": _s("https://docs.google.com/forms/", ("google forms", "google form"),
                ("google forms",), "google"),
    "keep": _s("https://keep.google.com/", ("google keep", "keep notes"),
               ("google keep",), "google"),
    "google photos": _s("https://photos.google.com/", ("my google photos",),
                 ("google photos",), "google", "https://photos.google.com/search/{q}"),
    "meet": _s("https://meet.google.com/", ("google meet",), ("meet",), "google"),
    "calendar": _s("https://calendar.google.com/", ("google calendar", "my calendar"),
                   ("google calendar",), "google"),
    "classroom": _s("https://classroom.google.com/", ("google classroom",),
                    ("google classroom", "classroom"), "learn"),
    "gemini": _s("https://gemini.google.com/", ("google gemini", "bard"), ("gemini",), "ai"),
    "contacts": _s("https://contacts.google.com/", ("google contacts", "my contacts"),
                   ("google contacts",), "google"),
    "google news": _s("https://news.google.com/", ("news",), ("google news",), "news",
                      "https://news.google.com/search?q={q}"),
    "play store": _s("https://play.google.com/store", ("google play", "play"),
                     ("google play",), "google", "https://play.google.com/store/search?q={q}"),
    "google scholar": _s("https://scholar.google.com/", ("scholar",), ("google scholar",),
                         "learn", "https://scholar.google.com/scholar?q={q}"),
    "google sites": _s("https://sites.google.com/", (), ("google sites",), "google"),
    "google account": _s("https://myaccount.google.com/", ("my google account",),
                         ("google account",), "google"),
    "google finance": _s("https://www.google.com/finance/", (), ("google finance",), "money"),
    "google flights": _s("https://www.google.com/travel/flights", ("flights",),
                         ("google flights",), "travel"),
    "google earth": _s("https://earth.google.com/web/", (), ("google earth",), "google"),
    "google colab": _s("https://colab.research.google.com/", ("colab", "colaboratory"),
                       ("colab",), "dev"),
    "google cloud": _s("https://console.cloud.google.com/", ("gcp", "google cloud console"),
                       ("google cloud",), "dev"),
    "google analytics": _s("https://analytics.google.com/", (), ("analytics",), "work"),
    "google pay": _s("https://pay.google.com/", ("gpay",), ("google pay",), "money"),
    "notebooklm": _s("https://notebooklm.google.com/", ("notebook lm",), ("notebooklm",), "ai"),
    "google ai studio": _s("https://aistudio.google.com/", ("ai studio",), ("ai studio",), "ai"),
    "youtube studio": _s("https://studio.youtube.com/", ("yt studio",), ("youtube studio",),
                         "video"),

    # ── Microsoft ──
    "outlook": _s("https://outlook.live.com/", ("outlook mail", "hotmail", "outlook web"),
                  ("outlook",), "microsoft"),
    "word online": _s("https://www.office.com/launch/word", ("microsoft word online",
                                                             "word on the web"),
                      ("word",), "microsoft"),
    "excel online": _s("https://www.office.com/launch/excel", ("microsoft excel online",
                                                               "excel on the web"),
                       ("excel",), "microsoft"),
    "powerpoint online": _s("https://www.office.com/launch/powerpoint",
                            ("powerpoint on the web",), ("powerpoint",), "microsoft"),
    "office": _s("https://www.office.com/", ("microsoft 365", "office 365", "office online"),
                 ("microsoft 365", "office"), "microsoft"),
    "onedrive": _s("https://onedrive.live.com/", ("one drive",), ("onedrive",), "microsoft"),
    "teams web": _s("https://teams.microsoft.com/", ("microsoft teams web", "teams online"),
                    ("teams",), "microsoft"),
    "bing": _s("https://www.bing.com/", (), ("bing",), "web", "https://www.bing.com/search?q={q}"),
    "copilot": _s("https://copilot.microsoft.com/", ("microsoft copilot", "bing chat"),
                  ("copilot",), "ai"),

    # ── work & productivity ──
    "notion": _s("https://www.notion.so/", ("notion so",), ("notion",), "work"),
    "canva": _s("https://www.canva.com/", (), ("canva",), "work"),
    "figma": _s("https://www.figma.com/", (), ("figma",), "work"),
    "miro": _s("https://miro.com/app/", (), ("miro",), "work"),
    "trello": _s("https://trello.com/", (), ("trello",), "work"),
    "asana": _s("https://app.asana.com/", (), ("asana",), "work"),
    "jira": _s("https://www.atlassian.com/software/jira", (), ("jira",), "work"),
    "slack web": _s("https://app.slack.com/", ("slack in the browser",), ("slack",), "work"),
    "discord web": _s("https://discord.com/app", ("discord in the browser",), ("discord",),
                      "social"),
    "zoom web": _s("https://app.zoom.us/wc", ("zoom in the browser",), ("zoom",), "work"),
    "dropbox": _s("https://www.dropbox.com/home", (), ("dropbox",), "work"),
    "overleaf": _s("https://www.overleaf.com/project", (), ("overleaf",), "learn"),
    "grammarly": _s("https://app.grammarly.com/", (), ("grammarly",), "work"),
    "calendly": _s("https://calendly.com/app", (), ("calendly",), "work"),
    "airtable": _s("https://airtable.com/", (), ("airtable",), "work"),
    "todoist": _s("https://app.todoist.com/", (), ("todoist",), "work"),
    "excalidraw": _s("https://excalidraw.com/", (), ("excalidraw",), "work"),
    "draw.io": _s("https://app.diagrams.net/", ("drawio", "draw io", "diagrams.net"),
                  ("draw.io", "diagrams.net"), "work"),
    "telegram web": _s("https://web.telegram.org/", ("telegram in the browser",),
                       ("telegram",), "social"),
    "messenger": _s("https://www.messenger.com/", ("facebook messenger",), ("messenger",),
                    "social"),
    "proton mail": _s("https://mail.proton.me/", ("protonmail",), ("proton mail",), "work"),

    # ── AI ──
    "perplexity": _s("https://www.perplexity.ai/", ("perplexity ai",), ("perplexity",), "ai",
                     "https://www.perplexity.ai/search?q={q}"),
    "huggingface": _s("https://huggingface.co/", ("hugging face", "hf"), ("hugging face",),
                      "ai", "https://huggingface.co/search/full-text?q={q}"),
    "deepseek": _s("https://chat.deepseek.com/", ("deep seek",), ("deepseek",), "ai"),
    "grok": _s("https://grok.com/", (), ("grok",), "ai"),
    "mistral": _s("https://chat.mistral.ai/", ("le chat",), ("mistral",), "ai"),
    "groq": _s("https://console.groq.com/", ("groq console",), ("groq",), "ai"),

    # ── developer ──
    "gitlab": _s("https://gitlab.com/", ("git lab",), ("gitlab",), "dev",
                 "https://gitlab.com/search?search={q}"),
    "leetcode": _s("https://leetcode.com/", ("leet code",), ("leetcode",), "dev",
                   "https://leetcode.com/problemset/?search={q}"),
    "kaggle": _s("https://www.kaggle.com/", (), ("kaggle",), "dev",
                 "https://www.kaggle.com/search?q={q}"),
    "codeforces": _s("https://codeforces.com/", ("code forces",), ("codeforces",), "dev"),
    "hackerrank": _s("https://www.hackerrank.com/", ("hacker rank",), ("hackerrank",), "dev"),
    "codechef": _s("https://www.codechef.com/", ("code chef",), ("codechef",), "dev"),
    "geeksforgeeks": _s("https://www.geeksforgeeks.org/", ("geeks for geeks", "gfg"),
                        ("geeksforgeeks",), "dev",
                        "https://www.geeksforgeeks.org/search/?gq={q}"),
    "npm": _s("https://www.npmjs.com/", ("npmjs",), ("npm",), "dev",
              "https://www.npmjs.com/search?q={q}"),
    "pypi": _s("https://pypi.org/", ("py pi", "python package index"), ("pypi",), "dev",
               "https://pypi.org/search/?q={q}"),
    "mdn": _s("https://developer.mozilla.org/", ("mdn docs", "mozilla docs"), ("mdn",), "dev",
              "https://developer.mozilla.org/en-US/search?q={q}"),
    "vercel": _s("https://vercel.com/dashboard", (), ("vercel",), "dev"),
    "netlify": _s("https://app.netlify.com/", (), ("netlify",), "dev"),
    "replit": _s("https://replit.com/", ("repl it",), ("replit",), "dev"),
    "codepen": _s("https://codepen.io/", ("code pen",), ("codepen",), "dev"),
    "docker hub": _s("https://hub.docker.com/", (), ("docker hub",), "dev",
                     "https://hub.docker.com/search?q={q}"),
    "aws console": _s("https://console.aws.amazon.com/", ("aws", "amazon web services"),
                      ("aws",), "dev"),
    "arch wiki": _s("https://wiki.archlinux.org/", ("archwiki", "arch linux wiki"),
                    ("archwiki",), "dev", "https://wiki.archlinux.org/index.php?search={q}"),
    "aur": _s("https://aur.archlinux.org/", ("arch user repository",), ("aur",), "dev",
              "https://aur.archlinux.org/packages?K={q}"),
    "omarchy manual": _s("https://learn.omacom.io/2/the-omarchy-manual", ("omarchy docs",),
                         ("omarchy",), "dev"),

    # ── social & communication ──
    "quora": _s("https://www.quora.com/", (), ("quora",), "social",
                "https://www.quora.com/search?q={q}"),
    "medium": _s("https://medium.com/", (), ("medium",), "learn",
                 "https://medium.com/search?q={q}"),
    "pinterest": _s("https://www.pinterest.com/", (), ("pinterest",), "social",
                    "https://www.pinterest.com/search/pins/?q={q}"),
    "threads": _s("https://www.threads.net/", (), ("threads",), "social"),
    "snapchat": _s("https://web.snapchat.com/", ("snap chat",), ("snapchat",), "social"),
    "tumblr": _s("https://www.tumblr.com/", (), ("tumblr",), "social"),
    "bluesky": _s("https://bsky.app/", ("blue sky",), ("bluesky",), "social"),

    # ── video & music ──
    "prime video": _s("https://www.primevideo.com/", ("amazon prime video", "amazon prime"),
                      ("prime video",), "video"),
    "hotstar": _s("https://www.hotstar.com/", ("disney hotstar", "jiohotstar", "jio hotstar"),
                  ("hotstar",), "video"),
    "jiocinema": _s("https://www.jiocinema.com/", ("jio cinema",), ("jiocinema",), "video"),
    "sonyliv": _s("https://www.sonyliv.com/", ("sony liv",), ("sonyliv",), "video"),
    "zee5": _s("https://www.zee5.com/", ("zee five",), ("zee5",), "video"),
    "crunchyroll": _s("https://www.crunchyroll.com/", (), ("crunchyroll",), "video"),
    "vimeo": _s("https://vimeo.com/", (), ("vimeo",), "video"),
    "imdb": _s("https://www.imdb.com/", (), ("imdb",), "video", "https://www.imdb.com/find/?q={q}"),
    "jiosaavn": _s("https://www.jiosaavn.com/", ("jio saavn", "saavn"), ("jiosaavn",), "music"),
    "gaana": _s("https://gaana.com/", (), ("gaana",), "music"),
    "soundcloud": _s("https://soundcloud.com/", ("sound cloud",), ("soundcloud",), "music",
                     "https://soundcloud.com/search?q={q}"),
    "apple music": _s("https://music.apple.com/", (), ("apple music",), "music"),

    # ── shopping & food (India first) ──
    "flipkart": _s("https://www.flipkart.com/", ("flip kart",), ("flipkart",), "shop",
                   "https://www.flipkart.com/search?q={q}"),
    "myntra": _s("https://www.myntra.com/", (), ("myntra",), "shop", "https://www.myntra.com/{q}"),
    "ajio": _s("https://www.ajio.com/", (), ("ajio",), "shop",
               "https://www.ajio.com/search/?text={q}"),
    "meesho": _s("https://www.meesho.com/", (), ("meesho",), "shop"),
    "nykaa": _s("https://www.nykaa.com/", (), ("nykaa",), "shop",
                "https://www.nykaa.com/search/result/?q={q}"),
    "croma": _s("https://www.croma.com/", (), ("croma",), "shop"),
    "ebay": _s("https://www.ebay.com/", (), ("ebay",), "shop", "https://www.ebay.com/sch/i.html?_nkw={q}"),
    "swiggy": _s("https://www.swiggy.com/", (), ("swiggy",), "food"),
    "zomato": _s("https://www.zomato.com/", (), ("zomato",), "food"),
    "blinkit": _s("https://blinkit.com/", ("grofers",), ("blinkit",), "food"),
    "zepto": _s("https://www.zeptonow.com/", (), ("zepto",), "food"),
    "bigbasket": _s("https://www.bigbasket.com/", ("big basket",), ("bigbasket",), "food"),
    "instamart": _s("https://www.swiggy.com/instamart", ("swiggy instamart",), ("instamart",),
                    "food"),

    # ── travel, tickets, money ──
    "bookmyshow": _s("https://in.bookmyshow.com/", ("book my show", "bms"), ("bookmyshow",),
                     "travel"),
    "makemytrip": _s("https://www.makemytrip.com/", ("make my trip", "mmt"), ("makemytrip",),
                     "travel"),
    "irctc": _s("https://www.irctc.co.in/nget/train-search", ("indian railways",), ("irctc",),
                "travel"),
    "uber": _s("https://m.uber.com/", (), ("uber",), "travel"),
    "ola": _s("https://book.olacabs.com/", ("ola cabs",), ("ola",), "travel"),
    "airbnb": _s("https://www.airbnb.co.in/", ("air bnb",), ("airbnb",), "travel"),
    "booking.com": _s("https://www.booking.com/", ("booking dot com",), ("booking.com",),
                      "travel"),
    "redbus": _s("https://www.redbus.in/", ("red bus",), ("redbus",), "travel"),
    "cleartrip": _s("https://www.cleartrip.com/", ("clear trip",), ("cleartrip",), "travel"),
    "paytm": _s("https://paytm.com/", (), ("paytm",), "money"),
    "phonepe": _s("https://www.phonepe.com/", ("phone pe",), ("phonepe",), "money"),
    "zerodha": _s("https://kite.zerodha.com/", ("kite", "zerodha kite"), ("kite",), "money"),
    "groww": _s("https://groww.in/", ("grow",), ("groww",), "money"),
    "sbi": _s("https://www.onlinesbi.sbi/", ("sbi online", "state bank", "onlinesbi"),
              ("sbi",), "money"),
    "hdfc bank": _s("https://netbanking.hdfcbank.com/", ("hdfc", "hdfc netbanking"),
                    ("hdfc",), "money"),
    "icici bank": _s("https://www.icicibank.com/", ("icici",), ("icici",), "money"),
    "axis bank": _s("https://www.axisbank.com/", ("axis",), ("axis bank",), "money"),
    "paypal": _s("https://www.paypal.com/", ("pay pal",), ("paypal",), "money"),

    # ── learning & reference ──
    "arxiv": _s("https://arxiv.org/", ("archive org papers", "arxiv papers"), ("arxiv",),
                "learn", "https://arxiv.org/a/{q}"),
    "coursera": _s("https://www.coursera.org/", (), ("coursera",), "learn",
                   "https://www.coursera.org/search?query={q}"),
    "udemy": _s("https://www.udemy.com/", (), ("udemy",), "learn",
                "https://www.udemy.com/courses/search/?q={q}"),
    "khan academy": _s("https://www.khanacademy.org/", ("khan",), ("khan academy",), "learn"),
    "nptel": _s("https://nptel.ac.in/", (), ("nptel",), "learn"),
    "swayam": _s("https://swayam.gov.in/", (), ("swayam",), "learn"),
    "duolingo": _s("https://www.duolingo.com/", (), ("duolingo",), "learn"),
    "wolfram alpha": _s("https://www.wolframalpha.com/", ("wolfram",), ("wolfram",), "learn",
                        "https://www.wolframalpha.com/input?i={q}"),
    "desmos": _s("https://www.desmos.com/calculator", ("desmos calculator", "graphing calculator"),
                 ("desmos",), "learn"),
    "chegg": _s("https://www.chegg.com/", (), ("chegg",), "learn"),
    "dictionary": _s("https://www.merriam-webster.com/", ("merriam webster",),
                     ("merriam-webster",), "learn",
                     "https://www.merriam-webster.com/dictionary/{q}"),
    "internet archive": _s("https://archive.org/", ("archive.org", "wayback machine"),
                           ("internet archive",), "learn",
                           "https://archive.org/search?query={q}"),
    "iitm portal": _s("https://www.iitm.ac.in/", ("iit madras", "iitm"), ("iit madras",),
                      "learn"),
    "iitm bs portal": _s("https://ds.study.iitm.ac.in/", ("iitm bs", "iitm online degree",
                                                          "study iitm"),
                         ("iitm",), "learn"),

    # ── news, weather, misc ──
    "weather": _s("https://weather.com/", ("weather forecast", "weather.com"), ("weather",),
                  "news"),
    "the hindu": _s("https://www.thehindu.com/", ("hindu",), ("the hindu",), "news"),
    "times of india": _s("https://timesofindia.indiatimes.com/", ("toi",), ("times of india",),
                         "news"),
    "ndtv": _s("https://www.ndtv.com/", (), ("ndtv",), "news"),
    "indian express": _s("https://indianexpress.com/", (), ("indian express",), "news"),
    "bbc news": _s("https://www.bbc.com/news", ("bbc",), ("bbc",), "news"),
    "cnn": _s("https://edition.cnn.com/", (), ("cnn",), "news"),
    "hacker news": _s("https://news.ycombinator.com/", ("hn", "ycombinator"), ("hacker news",),
                      "news"),
    "the verge": _s("https://www.theverge.com/", ("verge",), ("the verge",), "news"),
    "cricbuzz": _s("https://www.cricbuzz.com/", ("cricket score", "live cricket"),
                   ("cricbuzz",), "news"),
    "espncricinfo": _s("https://www.espncricinfo.com/", ("cricinfo",), ("cricinfo",), "news"),
    "duckduckgo": _s("https://duckduckgo.com/", ("duck duck go", "ddg"), ("duckduckgo",),
                     "web", "https://duckduckgo.com/?q={q}"),
    "speedtest": _s("https://www.speedtest.net/", ("speed test", "internet speed test"),
                    ("speedtest",), "web"),
    "chess.com": _s("https://www.chess.com/play", ("chess",), ("chess.com",), "web"),
    "lichess": _s("https://lichess.org/", (), ("lichess",), "web"),

    # ── the user's own lists (A6) ──
    "youtube history": _s("https://www.youtube.com/feed/history", ("my youtube history",
                                                                   "watch history"),
                          ("history - youtube",), "list"),
    "watch later": _s("https://www.youtube.com/playlist?list=WL", ("youtube watch later",
                                                                   "my watch later"),
                      ("watch later",), "list"),
    "youtube subscriptions": _s("https://www.youtube.com/feed/subscriptions",
                                ("my subscriptions", "my youtube subscriptions"),
                                ("subscriptions - youtube",), "list"),
    "liked videos": _s("https://www.youtube.com/playlist?list=LL", ("my liked videos",),
                       ("liked videos",), "list"),
    "amazon orders": _s("https://www.amazon.in/gp/css/order-history", ("my orders",
                                                                       "my amazon orders"),
                        ("your orders",), "list"),
    "flipkart orders": _s("https://www.flipkart.com/account/orders", ("my flipkart orders",),
                          ("my orders",), "list"),
    "github notifications": _s("https://github.com/notifications", (), ("notifications",),
                               "list"),
    "github pull requests": _s("https://github.com/pulls", ("my pull requests", "my prs"),
                               ("pull requests",), "list"),
}


_DISPLAY = {"youtube": "YouTube", "youtube music": "YouTube Music", "github": "GitHub",
            "chatgpt": "ChatGPT", "linkedin": "LinkedIn", "whatsapp web": "WhatsApp Web",
            "twitter": "X", "stackoverflow": "Stack Overflow", "gitlab": "GitLab",
            "leetcode": "LeetCode", "huggingface": "Hugging Face", "pypi": "PyPI",
            "npm": "npm", "mdn": "MDN", "aur": "the AUR", "imdb": "IMDb", "irctc": "IRCTC",
            "sbi": "SBI", "ndtv": "NDTV", "cnn": "CNN", "onedrive": "OneDrive",
            "bookmyshow": "BookMyShow", "makemytrip": "MakeMyTrip", "deepseek": "DeepSeek",
            "notebooklm": "NotebookLM", "phonepe": "PhonePe", "sonyliv": "SonyLIV",
            "jiocinema": "JioCinema", "jiosaavn": "JioSaavn", "geeksforgeeks": "GeeksforGeeks",
            "espncricinfo": "ESPNcricinfo", "duckduckgo": "DuckDuckGo", "paypal": "PayPal",
            "bigbasket": "BigBasket", "hackerrank": "HackerRank", "codechef": "CodeChef",
            "iitm portal": "IIT Madras", "iitm bs portal": "the IITM BS portal"}


def display_name(name):
    """The name Kayra SAYS: 'sheets' -> 'Google Sheets', 'youtube' -> 'YouTube'."""
    if not name:
        return ""
    if name in _DISPLAY:
        return _DISPLAY[name]
    spec = SITES.get(name) or {}
    for alias in spec.get("aliases", ()):
        if alias.startswith(("google ", "microsoft ")) and name in alias:
            return alias.title()
    return name.title() if name.islower() else name


def search_url(name, query):
    """The site's own search page for `query`, or None when it has none. Pure string work."""
    from urllib.parse import quote_plus
    spec = SITES.get(name)
    template = spec and spec.get("search")
    return template.replace("{q}", quote_plus(query or "")) if template else None
