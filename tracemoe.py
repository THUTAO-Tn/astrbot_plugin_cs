"""tracemoe —— trace.moe（以图搜番）的 Python 封装。

只用标准库，无第三方依赖。Python 3.8+。

    import tracemoe
    hits = tracemoe.search_file("scene.jpg")
    print(hits[0].title, hits[0].timecode, hits[0].similarity)
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "BASE_URL",
    "TraceMoeError",
    "QuotaExceededError",
    "SearchFailedError",
    "InvalidInputError",
    "Match",
    "Quota",
    "search",
    "search_file",
    "search_url",
    "best",
    "quota",
    "set_user_agent",
    "set_rate_limit",
]

BASE_URL = "https://api.trace.moe"
DEFAULT_USER_AGENT = "tracemoe-py/1.0"

# 模块级可变配置（用 set_user_agent / set_rate_limit 修改）
_user_agent = DEFAULT_USER_AGENT
_min_interval = 0.0
_last_call = 0.0


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class TraceMoeError(Exception):
    """所有错误的基类。"""


class QuotaExceededError(TraceMoeError):
    """当天免费额度（100 次/IP）用完了，或服务端返回 429。"""


class SearchFailedError(TraceMoeError):
    """请求成功但 API 返回了 error 字段（例如图片格式不被接受）。"""


class InvalidInputError(TraceMoeError, ValueError):
    """本地参数错误，比如文件不存在。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def set_user_agent(ua: str) -> None:
    """修改 User-Agent。

    trace.moe 前方有 Cloudflare，默认的 ``Python-urllib/x.y`` 会被直接
    403 拒绝（error code: 1010），所以务必保证 UA 不像默认值。
    """
    global _user_agent
    if not ua or ua.startswith("Python-urllib"):
        raise InvalidInputError("User-Agent 不能为空，也不能用默认的 Python-urllib/*")
    _user_agent = ua


def set_rate_limit(seconds: float) -> None:
    """两次请求之间的最小间隔（秒）。免费额度并发是 1，建议 1.0~1.5。"""
    global _min_interval
    _min_interval = max(0.0, float(seconds))


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Quota:
    """/me 的返回：当前 IP 的配额情况。"""

    client_id: str
    priority: int
    concurrency: int
    quota: int
    used: int

    @property
    def remaining(self) -> int:
        return max(self.quota - self.used, 0)

    def __str__(self) -> str:
        return f"今日 {self.used}/{self.quota} 次（剩余 {self.remaining}，并发 {self.concurrency}）"


@dataclass(frozen=True)
class Match:
    """一条搜索结果。"""

    title: str
    filename: str
    similarity: float
    start: float                      # 匹配片段起点（秒）
    end: float                        # 匹配片段终点（秒）
    at: float                         # 最佳帧时间点（秒）
    episode: Optional[int] = None
    episode_start: Optional[int] = None
    episode_end: Optional[int] = None
    duration: Optional[float] = None
    video_url: Optional[str] = None
    image_url: Optional[str] = None
    anilist_id: Optional[int] = None
    anilist: Optional[Dict[str, Any]] = None
    is_adult: Optional[bool] = None
    raw: Optional[Dict[str, Any]] = None

    # ---- 派生信息 ----
    @property
    def episode_label(self) -> str:
        if self.episode is not None:
            return f"第{self.episode}集"
        if self.episode_start is not None:
            return f"{self.episode_start}-{self.episode_end}集"
        return "集数未知"

    @property
    def timecode(self) -> str:
        """匹配片段起点，形如 00:04:32。"""
        return _hms(self.start)

    @property
    def confident(self) -> bool:
        """相似度是否达到常用可信阈值（0.87）。"""
        return self.similarity >= 0.87

    def preview(self, at: Optional[float] = None, mute: bool = False) -> Optional[str]:
        """返回 10 秒预览片段的 URL（mp4）。"""
        if not self.video_url:
            return None
        params = {"t": round(self.at if at is None else at, 2)}
        if mute:
            params["mute"] = 1
        return f"{self.video_url}?{urllib.parse.urlencode(params)}"

    def to_dict(self) -> Dict[str, Any]:
        """转成可 JSON 序列化的普通 dict。"""
        d = {
            "title": self.title,
            "episode": self.episode,
            "episode_label": self.episode_label,
            "filename": self.filename,
            "similarity": round(self.similarity, 4),
            "start": self.start,
            "end": self.end,
            "at": self.at,
            "timecode": self.timecode,
            "duration": self.duration,
            "video_url": self.video_url,
            "image_url": self.image_url,
            "anilist_id": self.anilist_id,
            "is_adult": self.is_adult,
        }
        return d

    def __str__(self) -> str:
        return (f"{self.title} {self.episode_label} {self.timecode} "
                f"相似度 {self.similarity:.1%}")


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _hms(sec: float) -> str:
    total = int(sec or 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _quote_params(params: Dict[str, Any]) -> str:
    """拼查询串；值为 None 表示无值开关（如 anilistInfo）。"""
    parts = []
    for k, v in params.items():
        if v is None:
            parts.append(urllib.parse.quote(k))
        else:
            parts.append(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}")
    return "&".join(parts)


def _throttle() -> None:
    global _last_call
    if _min_interval > 0:
        wait = _min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
    _last_call = time.monotonic()


def _error_from_http(e: urllib.error.HTTPError) -> TraceMoeError:
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    snippet = " ".join(body.split())[:200]
    if e.code == 403 and "1010" in body:
        return TraceMoeError(
            "被 Cloudflare 拦截（error code: 1010）：请求的 User-Agent 不在允许列表。"
            "用 tracemoe.set_user_agent('你的程序名/版本') 改掉。")
    if e.code == 429:
        return QuotaExceededError("请求过于频繁或今日配额已用完（HTTP 429）。")
    if e.code >= 500:
        return TraceMoeError(f"服务端错误 HTTP {e.code}：{snippet}")
    return TraceMoeError(f"HTTP {e.code}：{snippet}")


def _open(req: urllib.request.Request, timeout: float, retries: int) -> Dict[str, Any]:
    """发请求 + 重试，返回解析后的 JSON dict。"""
    attempt = 0
    while True:
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = _error_from_http(e)
            # 只有 5xx / 429 值得重试
            if e.code >= 500 or e.code == 429:
                if attempt < retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 8))
                    continue
            raise err from None
        except urllib.error.URLError as e:
            if attempt < retries:
                attempt += 1
                time.sleep(min(2 ** attempt, 8))
                continue
            raise TraceMoeError(f"网络错误：{e.reason}") from None


def _to_matches(payload: Dict[str, Any], limit: Optional[int]) -> List[Match]:
    if payload.get("error"):
        raise SearchFailedError(str(payload["error"]))
    out: List[Match] = []
    for item in (payload.get("result") or [])[: limit if limit else None]:
        anilist = item.get("anilist")
        anilist_dict = anilist if isinstance(anilist, dict) else None
        anilist_id = anilist if isinstance(anilist, int) else (
            anilist_dict.get("id") if anilist_dict else None)

        titles: Dict[str, str] = {}
        if anilist_dict:
            titles = {k: v for k, v in (anilist_dict.get("title") or {}).items() if v}
        title = (titles.get("chinese") or titles.get("romaji")
                 or titles.get("english") or titles.get("native")
                 or item.get("filename") or "未知作品")

        out.append(Match(
            title=title,
            filename=item.get("filename") or "",
            similarity=float(item.get("similarity") or 0.0),
            start=float(item.get("from") or 0.0),
            end=float(item.get("to") or 0.0),
            at=float(item.get("at") or 0.0),
            episode=item.get("episode"),
            episode_start=item.get("episode_start"),
            episode_end=item.get("episode_end"),
            duration=item.get("duration"),
            video_url=item.get("video"),
            image_url=item.get("image"),
            anilist_id=anilist_id,
            anilist=anilist_dict,
            is_adult=(anilist_dict or {}).get("isAdult"),
            raw=item,
        ))
    return out


def _search_params(anilist_info: bool, cut_borders: bool,
                   filters: Optional[Sequence[str]], limit: Optional[int]) -> str:
    params: Dict[str, Any] = {}
    if anilist_info:
        params["anilistInfo"] = None      # 开关型参数
    if cut_borders:
        params["cutBorders"] = None
    if filters:
        params["filter"] = ",".join(filters)
    # 注：limit 在本地切片，不发给服务端（API 不接受该参数）
    return _quote_params(params)


def _multipart(field: str, filename: str, data: bytes, ctype: str) -> tuple:
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    body = head + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# 公开 API
# --------------------------------------------------------------------------
def search_url(image_url: str, *, anilist_info: bool = True,
               cut_borders: bool = False, filters: Optional[Sequence[str]] = None,
               limit: Optional[int] = None, timeout: float = 60,
               retries: int = 1) -> List[Match]:
    """用一张**公开可访问**的图片 URL 搜索。"""
    if not image_url or not str(image_url).lower().startswith(("http://", "https://")):
        raise InvalidInputError(f"不是合法的图片 URL：{image_url!r}")
    q = _quote_params({"url": image_url}) + "&" + \
        _search_params(anilist_info, cut_borders, filters, limit)
    req = urllib.request.Request(f"{BASE_URL}/search?{q}",
                                 headers={"User-Agent": _user_agent})
    return _to_matches(_open(req, timeout, retries), limit)


def search_file(path: str | os.PathLike, *, anilist_info: bool = True,
                cut_borders: bool = False, filters: Optional[Sequence[str]] = None,
                limit: Optional[int] = None, timeout: float = 120,
                retries: int = 1) -> List[Match]:
    """上传本地图片搜索（截图最常用）。支持 jpg/png/webp 等。"""
    p = Path(path)
    if not p.is_file():
        raise InvalidInputError(f"文件不存在：{p}")
    data = p.read_bytes()
    if not data:
        raise InvalidInputError(f"文件是空的：{p}")
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    body, content_type = _multipart("image", p.name, data, ctype)
    url = f"{BASE_URL}/search?{_search_params(anilist_info, cut_borders, filters, limit)}"
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": _user_agent,
        "Content-Type": content_type,
    })
    return _to_matches(_open(req, timeout, retries), limit)


def search(image: str | os.PathLike, **kwargs: Any) -> List[Match]:
    """自动判断：以 http/https 开头当 URL，否则当本地文件路径。"""
    s = str(image)
    if s.lower().startswith(("http://", "https://")):
        return search_url(s, **kwargs)
    return search_file(s, **kwargs)


def best(image: str | os.PathLike, *, min_similarity: float = 0.87,
         **kwargs: Any) -> Optional[Match]:
    """只取最可信的一条；没有达到阈值就返回 None。"""
    hits = search(image, **kwargs)
    if hits and hits[0].similarity >= min_similarity:
        return hits[0]
    return None


def quota(timeout: float = 20, retries: int = 1) -> Quota:
    """查询当前 IP 的配额。"""
    req = urllib.request.Request(f"{BASE_URL}/me",
                                 headers={"User-Agent": _user_agent})
    d = _open(req, timeout, retries)
    return Quota(
        client_id=d.get("id", ""),
        priority=int(d.get("priority") or 0),
        concurrency=int(d.get("concurrency") or 1),
        quota=int(d.get("quota") or 0),
        used=int(d.get("quotaUsed") or 0),
    )


# --------------------------------------------------------------------------
# 命令行：python tracemoe.py <图片URL或文件>
# --------------------------------------------------------------------------
def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="trace.moe 以图搜番")
    ap.add_argument("image", nargs="?", help="图片 URL 或本地文件")
    ap.add_argument("--no-anilist", action="store_true", help="不取 AniList 详情")
    ap.add_argument("--cut-borders", action="store_true", help="先裁黑边")
    ap.add_argument("--filter", help="逗号分隔: anime,movie,op,ed,ost,pv")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quota", action="store_true", help="只看配额")
    args = ap.parse_args(argv)

    try:
        if args.quota:
            print(quota())
            return 0
        if not args.image:
            ap.print_help()
            return 2
        hits = search(
            args.image,
            anilist_info=not args.no_anilist,
            cut_borders=args.cut_borders,
            filters=args.filter.split(",") if args.filter else None,
        )
        if args.json:
            print(json.dumps([h.to_dict() for h in hits[: args.top]],
                             ensure_ascii=False, indent=2))
            return 0
        for i, h in enumerate(hits[: args.top], 1):
            print(f"{i}. {h}")
            if h.preview():
                print(f"   预览 {h.preview()}")
        return 0
    except TraceMoeError as e:
        print(f"出错：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
