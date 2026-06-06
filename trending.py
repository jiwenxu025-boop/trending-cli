#!/usr/bin/env python3
"""
终端热榜聚合器
══════════════
一条命令看遍 GitHub / Reddit / 知乎日报 / HackerNews / V2EX / 微博
零 API Key，零注册，即开即用。

用法:
  python trending.py          默认显示全部 6 个源
  python trending.py --no-live 单次刷新，不自动更新
  python trending.py --version 查看版本

安装:
  pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

__version__ = "1.1.0"

import httpx
from bs4 import BeautifulSoup
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── 数据模型 ──────────────────────────────────────────────────────

@dataclass
class Item:
    source: str       # 来源名
    rank: int         # 排名
    title: str        # 标题
    url: str          # 链接
    desc: str = ""    # 描述 / 副信息
    heat: str = ""     # 热度数值字符串
    extra: str = ""   # 额外标签

    @property
    def colored_rank(self) -> Text:
        """排名着色：TOP3 红，4-5 黄，其余白"""
        t = Text(str(self.rank).rjust(2))
        if self.rank <= 3:
            t.stylize("bold red")
        elif self.rank <= 5:
            t.stylize("bold yellow")
        return t


# ── 抓取函数 ─────────────────────────────────────────────────────
# 每个源一个异步函数，失败返回空列表

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": UA}
TIMEOUT = httpx.Timeout(15.0)


async def fetch_github(n: int = 15) -> list[Item]:
    """GitHub Trending — HTML 解析"""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as c:
            resp = await c.get("https://github.com/trending")
            resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    articles = soup.select("article.Box-row")
    items = []
    for i, art in enumerate(articles[:n], 1):
        # repo 名
        h2 = art.select_one("h2 a")
        if not h2:
            continue
        repo = h2.get_text(strip=True).replace(" ", "")
        url = f"https://github.com/{repo}"

        # 描述
        desc_tag = art.select_one("p")
        desc = desc_tag.get_text(strip=True) if desc_tag else ""

        # 星数 / fork
        stars = ""
        star_tag = art.select_one("a[href$='/stargazers']")
        if star_tag:
            stars = star_tag.get_text(strip=True)

        lang_tag = art.select_one("[itemprop='programmingLanguage']")
        lang = lang_tag.get_text(strip=True) if lang_tag else ""

        extra = f"[cyan]{lang}[/cyan]" if lang else ""
        items.append(Item("GitHub", i, repo, url, desc, stars, extra))
    return items




async def fetch_reddit(n: int = 15) -> list[Item]:
    """Reddit r/all — JSON API（需带真实 UA + cookies）"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as c:
            resp = await c.get(
                "https://www.reddit.com/r/popular/.json",
                params={"limit": n},
            )
            if resp.status_code == 429:
                # 限流，再试一次 old.reddit.com
                resp = await c.get(
                    "https://old.reddit.com/r/popular/.json",
                    params={"limit": n},
                )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    items = []
    posts = data.get("data", {}).get("children", [])
    for i, child in enumerate(posts[:n], 1):
        post = child.get("data", {})
        title = post.get("title", "")
        url = post.get("url", "https://reddit.com")
        score = post.get("score", 0)
        sub = post.get("subreddit", "")
        items.append(
            Item(
                "Reddit", i, title, url,
                desc=f"r/{sub}",
                heat=f"👍 {score}",
                extra=f"[yellow]r/{sub}[/yellow]",
            )
        )
    return items


async def fetch_hackernews(n: int = 15) -> list[Item]:
    """Hacker News 热门 — Firebase 公开 API，无 Key"""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            # 获取 top 文章 ID
            resp = await c.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json"
            )
            resp.raise_for_status()
            ids = resp.json()[:n]

            # 并发获取每篇文章详情
            tasks = [
                c.get(f"https://hacker-news.firebaseio.com/v0/item/{iid}.json")
                for iid in ids
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        return []

    items = []
    for i, r in enumerate(results, 1):
        if isinstance(r, Exception) or r is None:
            continue
        try:
            story = r.json()
        except Exception:
            continue
        title = story.get("title", "")
        url = story.get("url", f"https://news.ycombinator.com/item?id={story.get('id', '')}")
        score = story.get("score", 0)
        by = story.get("by", "")
        items.append(
            Item(
                "HackerNews", i, title, url,
                desc=f"by {by}" if by else "",
                heat=f"⬆ {score}",
                extra="[cyan]news.ycombinator.com[/cyan]",
            )
        )
    return items


async def fetch_v2ex(n: int = 15) -> list[Item]:
    """V2EX 热门主题 — 公开 JSON API"""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            resp = await c.get("https://www.v2ex.com/api/topics/hot.json")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    items = []
    for i, topic in enumerate(data[:n], 1):
        title = topic.get("title", "")
        tid = topic.get("id", 0)
        url = f"https://www.v2ex.com/t/{tid}"
        node = topic.get("node", {}).get("title", "")
        replies = topic.get("replies", 0)
        member = topic.get("member", {}).get("username", "")
        items.append(
            Item(
                "V2EX", i, title, url,
                desc=f"{member} · {node}",
                heat=f"💬 {replies}",
                extra=f"[blue]{node}[/blue]" if node else "",
            )
        )
    return items


async def fetch_weibo(n: int = 15) -> list[Item]:
    """微博热搜 — 移动端 JSON API"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as c:
            resp = await c.get("https://weibo.com/ajax/side/hotSearch")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    items = []
    reals = data.get("data", {}).get("realtime", [])
    for i, entry in enumerate(reals[:n], 1):
        word = entry.get("word", "")
        url = f"https://s.weibo.com/weibo?q={word}"
        heat_val = entry.get("num", "")
        if heat_val:
            heat_val = f"🔥 {heat_val}"
        # 有些词条带话题标签，保留原始词条名
        items.append(
            Item(
                "微博", i, word, url,
                desc="" if not entry.get("note") else entry.get("note", ""),
                heat=heat_val,
                extra="[red]热搜[/red]",
            )
        )
    return items


async def fetch_zhihu(n: int = 15) -> list[Item]:
    """知乎日报热闻 — 公开 JSON API，无需 Key"""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            resp = await c.get("https://news-at.zhihu.com/api/4/news/latest")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    items = []
    stories = data.get("stories", [])
    for i, story in enumerate(stories[:n], 1):
        title = story.get("title", "")
        sid = story.get("id", 0)
        url = story.get("url", f"https://daily.zhihu.com/story/{sid}")
        hint = story.get("hint", "")
        # 去掉 hint 中的来源前缀（如 "作者 / 张三"）
        desc = hint[:40] if hint else ""
        # 有些带图片的 stories 是广告/推广，skip 掉
        if story.get("type", 0) == 2:
            continue
        items.append(
            Item(
                "知乎日报", i, title, url,
                desc=desc,
                heat="📰",
                extra="[cyan]daily.zhihu.com[/cyan]",
            )
        )
    return items


# ── 渲染 ──────────────────────────────────────────────────────────

def make_source_panel(source: str, items: list[Item], emoji: str, color: str) -> Panel:
    """为一个源生成 Panel 表格"""
    if not items:
        return Panel(
            Text("加载失败或暂无数据", style="dim"),
            title=f"{emoji} {source}",
            border_style=color,
            box=box.ROUNDED,
        )

    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold", width=3)    # 排名
    table.add_column()                           # 标题 + 描述
    table.add_column(style="dim", no_wrap=True)  # 热度

    for it in items[:10]:
        title_text = Text(it.title, no_wrap=True)
        if len(title_text.plain) > 60:
            title_text = Text(title_text.plain[:58] + "…")

        desc_text = ""
        if it.desc:
            d = it.desc if len(it.desc) < 50 else it.desc[:48] + "…"
            desc_text = Text(f"\n  {d}", style="dim")

        col = Text.assemble(title_text, desc_text)
        table.add_row(it.colored_rank, col, Text(it.heat or "", style="cyan"))

    return Panel(
        table,
        title=f"{emoji} {source}",
        border_style=color,
        box=box.SQUARE,
        padding=(0, 1),
    )


def build_display(
    github: list[Item],
    reddit: list[Item],
    hackernews: list[Item],
    v2ex: list[Item],
    weibo: list[Item],
    zhihu: list[Item],
    mode: str,
    width: int,
    ts: str,
) -> Panel:
    """组装全部面板，mode 控制显示哪些源"""
    # ── 分栏策略：宽终端两列，窄终端单列 ──
    two_col = width >= 110
    sources_map = {
        "g": ("GitHub", github, "⭐", "green"),
        "r": ("Reddit", reddit, "🤖", "magenta"),
        "z": ("知乎日报", zhihu, "💡", "cyan"),
        "h": ("HackerNews", hackernews, "🧠", "yellow"),
        "v": ("V2EX", v2ex, "💬", "blue"),
        "w": ("微博", weibo, "🔥", "red"),
    }
    keys = list(sources_map.keys()) if mode == "a" else [m for m in mode if m in sources_map]
    if not keys:
        keys = list(sources_map.keys())

    selected = [sources_map[k] for k in keys]
    panels = [make_source_panel(*s) for s in selected]

    # 排版
    if two_col and len(panels) >= 3:
        # 两行两列
        mid = (len(panels) + 1) // 2
        row1 = Group(*panels[:mid])
        row2 = Group(*panels[mid:])
        content = Group(row1, row2)
    elif two_col and len(panels) == 2:
        content = Group(*panels)
    else:
        content = Group(*panels)

    # 底部栏
    footer = Text(
        f"  [{ts}]  [g]itHub  [r]eddit  [z]hihu  [h]ackerNews  [v]2ex  [w]eibo  [a]ll  [1-9]打开链接  [q]uit",
        style="dim",
    )

    return Panel(
        Group(content, Text(""), footer),
        title="[bold]🌐 热榜聚合[/bold]",
        border_style="bright_white",
        subtitle=f" {len(github)+len(reddit)+len(hackernews)+len(v2ex)+len(weibo)+len(zhihu)} 条 · {ts} ",
        padding=(1, 2),
    )


# ── 异步主循环 ────────────────────────────────────────────────────

async def refresh_all(n: int) -> tuple:
    """并发抓取所有源"""
    g, r, z, h, v, w = await asyncio.gather(
        fetch_github(n),
        fetch_reddit(n),
        fetch_zhihu(n),
        fetch_hackernews(n),
        fetch_v2ex(n),
        fetch_weibo(n),
    )
    return g, r, z, h, v, w


async def main_async_once() -> None:
    """单次刷新模式 — 显示一次后退出"""
    console = Console()

    with console.status("[bold green]加载各大热榜…[/bold green]"):
        github, reddit, hackernews, v2ex, weibo, zhihu = await refresh_all(15)

    ts = datetime.now().strftime("%H:%M:%S")
    panel = build_display(
        github, reddit, hackernews, v2ex, weibo, zhihu, "a",
        console.width, ts,
    )
    console.print(panel)


# ── 实际入口（用选择器让用户可以按键切换）────────────────────────

def main() -> None:
    """实际入口 — 解析参数后自动选择平台交互方式"""
    import sys

    parser = argparse.ArgumentParser(
        prog="trending",
        description="终端热榜聚合器 — 一条命令看遍全站热点",
        epilog="按键: g=GitHub  r=Reddit  z=知乎日报  h=HackerNews  v=V2EX  w=微博  a=全部  q=退出",
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="单次刷新后退出，不进入自动刷新模式",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="显示版本号",
    )
    args = parser.parse_args()

    if args.version:
        print(f"trending v{__version__}")
        return

    if args.no_live:
        asyncio.run(main_async_once())
    elif sys.platform == "win32":
        asyncio.run(main_async_win())
    else:
        asyncio.run(main_async_unix())


# ── Unix 版本（真正的键盘交互）─────────────────────────────────────

async def main_async_unix() -> None:
    import fcntl
    import termios
    import tty

    console = Console()
    width = console.width
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        # 非阻塞
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        github, reddit, hackernews, v2ex, weibo, zhihu = await refresh_all(15)
        mode = "a"
        last_refresh = time.time()

        with Live(
            build_display(github, reddit, hackernews, v2ex, weibo, zhihu, mode, width,
                          datetime.now().strftime("%H:%M:%S")),
            console=console,
            refresh_per_second=4,
            screen=False,
        ) as live:
            while True:
                now = time.time()
                if now - last_refresh > 180:
                    github, reddit, hackernews, v2ex, weibo, zhihu = await refresh_all(15)
                    last_refresh = now

                # 读取键盘
                try:
                    ch = sys.stdin.read(1)
                    if ch:
                        ch = ch.lower()
                        if ch == "q":
                            break
                        elif ch in ("g", "r", "z", "h", "v", "w", "a"):
                            mode = ch
                        elif ch.isdigit():
                            idx = int(ch) - 1
                            # 收集当前可见项目
                            all_items = []
                            if mode == "a" or mode == "g":
                                all_items.extend(github[:10])
                            if mode == "a" or mode == "r":
                                all_items.extend(reddit[:10])
                            if mode == "a" or mode == "h":
                                all_items.extend(hackernews[:10])
                            if mode == "a" or mode == "v":
                                all_items.extend(v2ex[:10])
                            if mode == "a" or mode == "w":
                                all_items.extend(weibo[:10])
                            if mode == "a" or mode == "z":
                                all_items.extend(zhihu[:10])
                            if 0 <= idx < len(all_items):
                                webbrowser.open(all_items[idx].url)
                except (BlockingIOError, OSError):
                    pass

                ts = datetime.now().strftime("%H:%M:%S")
                live.update(
                    build_display(github, reddit, hackernews, v2ex, weibo, zhihu, mode, width, ts)
                )
                await asyncio.sleep(0.3)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # 恢复终端
        console.print("\n[green]👋 下次见！[/green]")


# ── Windows 版本（简化，用 input 轮询）────────────────────────────

async def main_async_win() -> None:
    import threading

    console = Console()
    width = console.width
    mode = "a"
    github = reddit = hackernews = v2ex = weibo = zhihu = []
    last_refresh = 0
    running = True

    # 后台刷新
    async def bg_refresh():
        nonlocal github, reddit, hackernews, v2ex, weibo, zhihu, last_refresh
        github, reddit, hackernews, v2ex, weibo, zhihu = await refresh_all(15)
        last_refresh = time.time()

    await bg_refresh()

    # 按键线程
    key_queue = []

    def reader():
        while running:
            try:
                ch = console.input()
                key_queue.append(ch.strip().lower())
            except:
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    with Live(
        build_display(github, reddit, hackernews, v2ex, weibo, zhihu, mode, width,
                      datetime.now().strftime("%H:%M:%S")),
        console=console,
        refresh_per_second=4,
        screen=False,
    ) as live:
        while running:
            now = time.time()
            if now - last_refresh > 180:
                await bg_refresh()

            # 处理队列
            while key_queue:
                ch = key_queue.pop(0)
                if ch == "q":
                    running = False
                elif ch in ("g", "r", "z", "h", "v", "w", "a"):
                    mode = ch
                elif ch.isdigit():
                    idx = int(ch) - 1
                    all_items = []
                    if mode in ("a", "g"):
                        all_items.extend(github[:10])
                    if mode in ("a", "r"):
                        all_items.extend(reddit[:10])
                    if mode in ("a", "h"):
                        all_items.extend(hackernews[:10])
                    if mode in ("a", "v"):
                        all_items.extend(v2ex[:10])
                    if mode in ("a", "w"):
                        all_items.extend(weibo[:10])
                    if mode in ("a", "z"):
                        all_items.extend(zhihu[:10])
                    if 0 <= idx < len(all_items):
                        webbrowser.open(all_items[idx].url)

            ts = datetime.now().strftime("%H:%M:%S")
            live.update(
                build_display(github, reddit, hackernews, v2ex, weibo, zhihu, mode, width, ts)
            )
            await asyncio.sleep(1)

    console.print("\n[green]👋 下次见！[/green]")


if __name__ == "__main__":
    main()
