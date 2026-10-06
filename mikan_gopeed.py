#!/usr/bin/env python3
"""Mikan RSS to Gopeed automation with conservative duplicate protection."""

from __future__ import annotations

import argparse
import difflib
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m2ts", ".webm"}
__version__ = "1.2.1"
USER_AGENT = f"mikan-gopeed/{__version__}"

TRADITIONAL_MAP = str.maketrans(
    {
        "藍": "蓝",
        "與": "与",
        "於": "于",
        "轉": "转",
        "萊": "莱",
        "複": "复",
        "製": "制",
        "會": "会",
        "戀": "恋",
        "愛": "爱",
        "談": "谈",
        "龍": "龙",
        "廬": "庐",
        "彎": "弯",
        "對": "对",
        "個": "个",
        "這": "这",
        "檔": "档",
        "兒": "儿",
        "貓": "猫",
        "為": "为",
        "無": "无",
        "視": "视",
        "開": "开",
        "發": "发",
        "歲": "岁",
    }
)

CN_NUM = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}

CN_SEASON = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
    11: "十一",
    12: "十二",
}


def log(message: str, *, verbose: bool = True) -> None:
    if verbose:
        print(message, flush=True)


def curl_bytes(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    unix_socket: str = "",
) -> bytes:
    cmd = ["curl", "-fsSL", "-A", USER_AGENT, "--max-time", str(timeout)]
    if unix_socket:
        cmd.extend(["--unix-socket", unix_socket])
    cmd.extend(["-X", method])
    for key, value in (headers or {}).items():
        cmd.extend(["-H", f"{key}: {value}"])
    if payload is not None:
        # ensure_ascii=True：Gopeed 的 /forward 通道按字节解码 JSON，非 ASCII 会被读成乱码，
        # 所以负载必须全用 \uXXXX 转义，保持传输层纯 ASCII
        cmd.extend(
            [
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                json.dumps(payload),
            ]
        )
    cmd.append(url)
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"curl failed for {sanitize_token(url)}: {sanitize_token(detail)}")
    return result.stdout


def sanitize_token(text: str) -> str:
    # RSS URL 内嵌 Mikan token，避免随错误信息写进日志
    return re.sub(r"(token=)[^&\s]+", r"\1<redacted>", text, flags=re.IGNORECASE)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(item: ET.Element, name: str) -> str:
    for child in item.iter():
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_rss(raw: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(raw)
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        torrent_url = ""
        for child in item.iter():
            if local_name(child.tag) == "enclosure":
                torrent_url = child.attrib.get("url", "").strip()
                break
        items.append(
            {
                "title": child_text(item, "title"),
                "link": child_text(item, "link"),
                "guid": child_text(item, "guid"),
                "torrent_url": torrent_url,
            }
        )
    return items


def extract_magnet_links(text: str) -> list[str]:
    decoded = html.unescape(text)
    links = re.findall(r"magnet:\?[^\s\"'<>]+", decoded, flags=re.IGNORECASE)
    cleaned = [link.rstrip(".,;)]}") for link in links]
    return list(dict.fromkeys(cleaned))


def _bdecode(raw: bytes, index: int, info_span: dict[str, tuple[int, int]]) -> tuple[Any, int]:
    marker = raw[index : index + 1]
    if marker == b"i":
        end = raw.index(b"e", index)
        return int(raw[index + 1 : end]), end + 1
    if marker == b"l":
        values: list[Any] = []
        index += 1
        while raw[index : index + 1] != b"e":
            value, index = _bdecode(raw, index, info_span)
            values.append(value)
        return values, index + 1
    if marker == b"d":
        values: dict[Any, Any] = {}
        index += 1
        while raw[index : index + 1] != b"e":
            key, index = _bdecode(raw, index, info_span)
            value_start = index
            value, index = _bdecode(raw, index, info_span)
            if key == b"info" and "span" not in info_span:
                info_span["span"] = (value_start, index)
            values[key] = value
        return values, index + 1
    colon = raw.index(b":", index)
    length = int(raw[index:colon])
    start = colon + 1
    return raw[start : start + length], start + length


def torrent_info(raw: bytes) -> tuple[str, list[str]]:
    info_span: dict[str, tuple[int, int]] = {}
    decoded, _ = _bdecode(raw, 0, info_span)
    if "span" not in info_span:
        raise ValueError("torrent has no info dictionary")
    start, end = info_span["span"]
    infohash = hashlib.sha1(raw[start:end]).hexdigest()

    trackers: list[str] = []
    if isinstance(decoded, dict):
        announce = decoded.get(b"announce")
        if isinstance(announce, bytes):
            trackers.append(announce.decode("utf-8", "replace"))
        announce_list = decoded.get(b"announce-list")
        if isinstance(announce_list, list):
            for group in announce_list:
                if isinstance(group, list):
                    trackers.extend(v.decode("utf-8", "replace") for v in group if isinstance(v, bytes))
                elif isinstance(group, bytes):
                    trackers.append(group.decode("utf-8", "replace"))
    return infohash, list(dict.fromkeys(trackers))


def magnet_from_torrent(raw: bytes) -> tuple[str, str]:
    infohash, trackers = torrent_info(raw)
    parts = [f"magnet:?xt=urn:btih:{infohash}"]
    for tracker in trackers:
        parts.append("&tr=" + urllib.parse.quote(tracker, safe=""))
    return "".join(parts), infohash


def extract_episode(title: str) -> int | None:
    patterns = [
        r"\s[-–—]\s*(\d{1,4})(?:v\d+)?\s*\[",
        r"\s[-–—]\s*(\d{1,4})(?:v\d+)?\s*$",
        r"第\s*(\d{1,4})\s*(?:话|話|集)",
        r"(?i)\bep?\s*(\d{1,3})(?:v\d+)?\b",
        r"[\[\(](\d{1,3})(?:v\d+)?[\]\)]",
    ]
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            return int(match.group(1))
    return None


def strip_episode(title: str) -> str:
    cleaned = re.sub(r"\[[^\]]+\]", " ", title)
    cleaned = re.sub(r"\s[-–—]\s*\d{1,4}(?:v\d+)?\b.*$", "", cleaned)
    cleaned = re.sub(r"第\s*\d{1,4}\s*(?:话|話|集)", " ", cleaned)
    cleaned = re.sub(r"(?i)\bep?\s*\d{1,3}(?:v\d+)?\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -–—")


def chinese_density(text: str) -> int:
    return sum("\u4e00" <= char <= "\u9fff" for char in text)


def series_title(title: str) -> str:
    # 1) 移除全部括号组（半角 [] 与全角 【】，后者多为字幕组名）与集数尾缀 → 括号外文本
    outside = re.sub(r"【[^】]*】", " ", title)
    outside = re.sub(r"\[[^\]]*\]", " ", outside)
    outside = strip_episode(outside)
    # 2) 候选：括号外文本按 / 拆分的片段 + 含 / 的 [] 组内子段
    #    （兼容【字幕组】[番名A / 番名B / 罗马字] 双括号标题，取中日文密度最高者）
    candidates = []
    for part in outside.split("/"):
        part = part.strip()
        if chinese_density(part) > 0:
            candidates.append(part)
    for group in re.findall(r"\[([^\]]+)\]", title):
        if "/" not in group:
            continue
        for part in group.split("/"):
            part = part.strip()
            if chinese_density(part) >= 2:
                candidates.append(part)
    if not candidates:
        return re.sub(r"\s+", " ", outside).strip() or title.strip()
    selected = max(candidates, key=lambda part: (chinese_density(part), len(part)))
    if chinese_density(selected) > 0:
        selected = re.sub(r"^[A-Za-z0-9 ._-]+", "", selected).strip()
    return selected or title.strip()


def season_number(text: str) -> int | None:
    translated = text.translate(TRADITIONAL_MAP)
    match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*季", translated)
    if match:
        token = match.group(1)
        return int(token) if token.isdigit() else CN_NUM.get(token)
    match = re.search(r"(?:^|\s)([1-9]|1[0-2])(?:\s|$)", translated)
    return int(match.group(1)) if match else None


def normalize_text(text: str) -> str:
    translated = unicodedata.normalize("NFKC", text).translate(TRADITIONAL_MAP).casefold()

    def replace_cn_season(match: re.Match[str]) -> str:
        token = match.group(1)
        value = int(token) if token.isdigit() else CN_NUM.get(token)
        return f"s{value}" if value is not None else match.group(0)

    translated = re.sub(r"第\s*([一二三四五六七八九十\d]+)\s*季", replace_cn_season, translated)
    translated = re.sub(r"\s+([1-9]|1[0-2])\s*$", r"s\1", translated)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", translated)


def similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left and right and (left in right or right in left):
        return max(0.82, min(len(left), len(right)) / max(len(left), len(right)))
    return difflib.SequenceMatcher(None, left, right).ratio()


def classify_match(
    title: str,
    folders: list[str],
    aliases: dict[str, list[str]],
    *,
    threshold: float = 0.62,
    margin: float = 0.08,
) -> tuple[str, str, float]:
    """返回 (status, folder, score)，status 为 "matched"、"new" 或 "ambiguous"。"""
    target = normalize_text(series_title(title))
    target_season = season_number(series_title(title))
    scored: list[tuple[str, float]] = []
    for folder in folders:
        folder_season = season_number(folder)
        if target_season is not None and folder_season is not None and target_season != folder_season:
            continue
        names = [folder, *aliases.get(folder, [])]
        score = max(similarity(target, normalize_text(name)) for name in names)
        scored.append((folder, score))
    if not scored:
        return "new", "", 0.0
    scored.sort(key=lambda item: (-item[1], item[0]))
    best, best_score = scored[0]
    if best_score >= threshold:
        decisive = best_score >= 1.0 or len(scored) == 1 or best_score - scored[1][1] >= margin
        return ("matched" if decisive else "ambiguous"), best, best_score
    return "new", "", best_score


def new_folder_name(title: str) -> str:
    name = series_title(title)
    season = season_number(name)
    if season is not None:
        name = re.sub(r"第\s*[一二三四五六七八九十\d]+\s*季", "", name)
        name = re.sub(r"(?:^|\s)([1-9]|1[0-2])(?:\s|$)", " ", name)
        season_label = CN_SEASON.get(season, str(season))
        name = f"{name.strip()} 第{season_label}季"
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120] or "未命名番剧"


def episode_file(folder: Path, episode: int | None) -> Path | None:
    if episode is None:
        return None
    pattern = re.compile(rf"(?<!\d)0*{episode}(?!\d)")
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and pattern.search(path.name):
            return path
    return None


def item_hash(item: dict[str, str]) -> str | None:
    for value in item.values():
        match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])", value)
        if match:
            return match.group(1).lower()
    return None


def item_keys(item: dict[str, str]) -> list[str]:
    return [
        item.get("guid", ""),
        item_hash(item) or "",
        f"{series_title(item['title'])}|{extract_episode(item['title'])}",
    ]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        broken = path.with_suffix(".json.corrupt")
        try:
            path.replace(broken)
        except Exception:
            pass
        notify(f"去重状态文件损坏已重建：{path.name}（{exc}）")
        return {"seen": {}}
    value.setdefault("seen", {})
    return value


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def save_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def mark_seen(state: dict[str, Any], item: dict[str, str], action: str) -> None:
    for key in filter(None, item_keys(item)):
        state["seen"][key] = action


def is_seen(state: dict[str, Any], item: dict[str, str]) -> bool:
    return any(key in state["seen"] for key in filter(None, item_keys(item)))


RECORD_HEADER = """# 番剧更新记录

由「番剧自动下载」服务自动维护：RSS 有更新且下载完成后，新记录会加在最上面（时间 + 标题）。
本文件可以直接编辑。

"""


def load_pending(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        notify(f"下载跟踪文件损坏已重置：{path.name}（{exc}）")
        return {}


def save_pending(path: Path, pending: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(pending, ensure_ascii=False, indent=2))


def make_pending_entry(
    item: dict[str, str], target: Path, episode: int | None, files: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "title": item["title"],
        "series": target.name,
        "episode": episode,
        "target": str(target),
        "created_ts": time.time(),
        "expected_files": [str(f.get("name", "")) for f in files][:5],
        "keys": item_keys(item),
    }


def prepend_record(path: Path, lines: list[str], backup_path: Path | None = None) -> None:
    # 桌面文件必须由服务进程（launchd 身份）创建和读写：其他进程创建的文件带
    # com.apple.provenance 属性，服务进程访问会被 macOS 拒绝。读不到时退回备份。
    text = None
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else None
    except Exception:
        text = None
    if text is None and backup_path is not None and backup_path.exists():
        text = backup_path.read_text(encoding="utf-8")
    if text is None:
        text = RECORD_HEADER
    cut = text.find("\n## ")
    head = text if cut < 0 else text[: cut + 1]
    body = "" if cut < 0 else text[cut + 1 :]
    section = ("\n".join(lines).rstrip() + "\n\n") if lines else ""
    merged = head + section + body
    atomic_write_text(path, merged)
    if backup_path is not None:
        try:
            atomic_write_text(backup_path, merged)
        except Exception:
            pass


def new_video_files_since(target: Path, since_ts: float) -> list[str]:
    if not target.exists():
        return []
    names = [
        path.name
        for path in target.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and path.stat().st_mtime >= since_ts - 30
    ]
    return sorted(dict.fromkeys(names))


def record_done_entry(
    record_path: Path, entry: dict[str, Any], backup_path: Path | None = None
) -> None:
    when = time.strftime("%Y-%m-%d %H:%M")
    disk = new_video_files_since(Path(entry["target"]), entry["created_ts"])
    names = list(dict.fromkeys(disk + [n for n in entry.get("expected_files", []) if n]))
    episode = entry.get("episode")
    title_part = f"{entry['series']} 第 {episode} 集" if episode is not None else entry["series"]
    lines = [f"## {when}", f"- **{title_part}**"]
    if entry.get("title"):
        lines.append(f"  - RSS 标题：{entry['title']}")
    for name in names[:3]:
        lines.append(f"  - 文件：`{name}`")
    prepend_record(record_path, lines, backup_path)


def check_pending_downloads(
    client: "GopeedClient",
    pending_path: Path,
    record_path: Path,
    state: dict[str, Any],
    *,
    max_attempts: int = 3,
    verbose: bool = False,
    backup_path: Path | None = None,
) -> None:
    pending = load_pending(pending_path)
    if not pending:
        return
    try:
        tasks = client.call("GET", "/api/v1/tasks")
    except Exception as exc:
        log(f"pending download check skipped: {exc}", verbose=verbose)
        return
    index = {}
    for task in tasks if isinstance(tasks, list) else tasks.get("tasks", []):
        if isinstance(task, dict) and task.get("id"):
            index[task["id"]] = task
    for task_id, entry in list(pending.items()):
        status = (index.get(task_id) or {}).get("status")
        target = Path(entry["target"])
        if status == "done":
            try:
                record_done_entry(record_path, entry, backup_path)
            except Exception as exc:
                log(f"ERROR record write failed for {entry['title']!r}: {exc}", verbose=True)
                notify(f"番剧更新记录写入失败，下轮自动重试：{entry['title']}")
                continue
            del pending[task_id]
            log(f"RECORDED download of {entry['title']!r} into {record_path.name}", verbose=verbose)
        elif status == "error":
            # 失败任务必须从 Gopeed 删除，否则它的 btih 会一直占据哈希去重名单，
            # 下一轮的重新创建会被挡住，"重试"永远不会真正发生
            try:
                client.call("DELETE", f"/api/v1/tasks/{task_id}?force=true", timeout=30)
            except Exception as exc:
                log(f"ERROR deleting failed task {task_id}: {exc}", verbose=True)
            attempts = state.setdefault("download_attempts", {})
            series_key = (entry.get("keys") or ["", "", task_id])[-1]
            n = attempts.get(series_key, 0) + 1
            if n >= max_attempts:
                for key in filter(None, entry.get("keys", [])):
                    state["seen"][key] = "failed"
                attempts.pop(series_key, None)
                notify(f"番剧多次下载失败，已放弃本轮更新：{entry['title']}")
            else:
                attempts[series_key] = n
                for key in filter(None, entry.get("keys", [])):
                    state["seen"].pop(key, None)
                notify(f"番剧下载失败（第 {n}/{max_attempts} 次），下轮自动重试：{entry['title']}")
            log(f"TASK ERROR {entry['title']!r} attempts={n}", verbose=True)
            del pending[task_id]
        elif task_id not in index:
            # 任务在 Gopeed 里消失了：若文件已落地则照常记录，否则视为用户主动删除
            disk = new_video_files_since(target, entry["created_ts"])
            if disk:
                entry["expected_files"] = disk
                try:
                    record_done_entry(record_path, entry, backup_path)
                except Exception as exc:
                    log(f"ERROR record write failed for {entry['title']!r}: {exc}", verbose=True)
                else:
                    log(f"RECORDED vanished-task download of {entry['title']!r}", verbose=verbose)
            else:
                log(f"pending task vanished without files: {entry['title']!r}", verbose=True)
            del pending[task_id]
        save_pending(pending_path, pending)


class GopeedClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        unix_socket: str = "",
        *,
        start_app: bool = True,
        app_name: str = "Gopeed",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.unix_socket = str(Path(unix_socket).expanduser()) if unix_socket else ""
        self.start_app = start_app
        self.app_name = app_name

    def call(self, method: str, path: str, payload: Any = None, *, timeout: int = 30) -> Any:
        transports = ["socket", "http"] if self.unix_socket else ["http"]
        errors: list[str] = []
        raw = b""
        for transport in transports:
            if transport == "socket" and not Path(self.unix_socket).exists():
                errors.append("socket: not found")
                continue
            try:
                raw = self._transport(transport, method, path, payload, timeout=timeout)
                break
            except Exception as exc:
                errors.append(f"{transport}: {exc}")
        else:
            raise RuntimeError(" | ".join(errors))
        result = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(result, dict) and result.get("code", 0) != 0:
            raise RuntimeError(f"Gopeed API error: {result.get('msg') or result.get('code')}")
        return result.get("data", result) if isinstance(result, dict) else result

    def _transport(self, transport: str, method: str, path: str, payload: Any, *, timeout: int = 30) -> bytes:
        # 桌面版 Gopeed 默认不开 TCP API，只暴露本地 socket 的 /forward 转发通道
        if transport == "socket":
            return curl_bytes(
                "http://localhost/forward",
                method="POST",
                payload={"method": method, "path": path.lstrip("/"), "data": payload, "query": {}},
                timeout=timeout,
                unix_socket=self.unix_socket,
            )
        headers = {"X-Api-Token": self.token} if self.token else {}
        return curl_bytes(
            self.base_url + "/" + path.lstrip("/"),
            method=method,
            payload=payload,
            headers=headers,
            timeout=timeout,
        )

    def info(self, timeout: int = 10) -> Any:
        return self.call("GET", "/api/v1/info", timeout=timeout)

    def resolve(self, url: str, target: Path) -> dict[str, Any]:
        return self.call(
            "POST",
            "/api/v1/resolve",
            {"req": {"url": url}, "opts": {"path": str(target)}},
            timeout=150,
        )

    def create(self, url: str, target: Path, rid: str, select_files: list[int]) -> str:
        payload: dict[str, Any] = {"opts": {"path": str(target), "selectFiles": select_files}}
        if rid:
            payload["rid"] = rid
        else:
            payload["req"] = {"url": url, "labels": {"source": "mikan-rss"}}
        return self.call("POST", "/api/v1/tasks", payload, timeout=60)

    def task_hashes(self) -> set[str]:
        try:
            tasks = self.call("GET", "/api/v1/tasks")
        except Exception:
            return set()
        hashes: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)
            elif isinstance(value, str):
                for match in re.findall(r"btih:([0-9a-fA-F]{40})", value, flags=re.IGNORECASE):
                    hashes.add(match.lower())

        walk(tasks)
        return hashes


def find_resource_files(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        files = value.get("files")
        if isinstance(files, list) and all(isinstance(item, dict) and "name" in item for item in files):
            return files
        for nested in value.values():
            found = find_resource_files(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_resource_files(nested)
            if found:
                return found
    return []


def video_indexes(files: Iterable[dict[str, Any]]) -> list[int]:
    indexes: list[int] = []
    for index, item in enumerate(files):
        name = str(item.get("name", ""))
        if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
            indexes.append(index)
    return indexes


_notifications: list[str] = []


def notify(message: str) -> None:
    # 聚合到本轮结束时统一发送，避免一次循环弹多条通知
    _notifications.append(message)


def flush_notifications() -> None:
    if not _notifications:
        return
    combined = "\n".join(dict.fromkeys(_notifications))
    _notifications.clear()
    if len(combined) > 400:
        combined = combined[:400] + "…"
    # ensure_ascii=False：AppleScript 字符串不解析 \uXXXX，必须直接用中文
    script = f'display notification {json.dumps(combined, ensure_ascii=False)} with title "番剧自动下载"'
    subprocess.run(
        ["osascript", "-e", script],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def gopeed_ready(client: GopeedClient, *, allow_launch: bool, verbose: bool) -> bool:
    try:
        client.info()
        return True
    except Exception as exc:
        log(f"Gopeed API unavailable: {exc}", verbose=verbose)
    if not allow_launch or not client.start_app:
        return False
    log(f"launching {client.app_name} via open -a ...", verbose=verbose)
    subprocess.run(
        ["open", "-a", client.app_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        time.sleep(2)
        try:
            client.info()
            log("Gopeed API is ready after launch", verbose=verbose)
            return True
        except Exception:
            continue
    return False


def resolve_item_source(item: dict[str, str]) -> tuple[str, str, str | None]:
    fallback_hash = item_hash(item)
    page_error = ""
    if item["link"]:
        try:
            links = extract_magnet_links(curl_bytes(item["link"]).decode("utf-8", "replace"))
            if links:
                match = re.search(r"btih:([0-9a-fA-F]{40})", links[0], flags=re.IGNORECASE)
                return links[0], "magnet", match.group(1).lower() if match else fallback_hash
        except Exception as exc:
            page_error = str(exc)
    if item["torrent_url"]:
        try:
            magnet, infohash = magnet_from_torrent(curl_bytes(item["torrent_url"]))
            return magnet, "torrent-infohash", infohash
        except Exception as exc:
            page_error = f"{page_error}; torrent: {exc}"
    if item["torrent_url"]:
        return item["torrent_url"], "torrent-url", fallback_hash
    raise RuntimeError(f"no magnet or torrent source: {page_error}")


_run_lock = None


def acquire_run_lock(state_path: Path) -> bool:
    # launchd 不会并发拉起同一 label，但手动 --once 可能撞上定时轮次；
    # 拿不到锁就直接放弃本轮，避免两个进程互相覆盖 state/pending。
    global _run_lock
    handle = open(state_path.with_suffix(".lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _run_lock = handle
    return True


def release_run_lock() -> None:
    # 常驻进程（如网页面板）在每轮刷新结束后需要显式释放，否则同进程内后续刷新拿不到锁
    global _run_lock
    if _run_lock:
        try:
            _run_lock.close()
        except Exception:
            pass
        _run_lock = None


def wait_for_records(
    client: "GopeedClient",
    pending_path: Path,
    record_path: Path,
    state: dict[str, Any],
    backup_path: Path,
    *,
    max_wait: int = 900,
    poll_interval: int = 15,
    max_attempts: int = 3,
    verbose: bool = False,
) -> bool:
    """任务创建后在同一进程内盯梢：每 poll_interval 秒检查一次，
    全部下载完成并写入记录后返回 True；超时交给下一轮巡检兜底。"""
    deadline = time.time() + max(max_wait, 0)
    while True:
        try:
            check_pending_downloads(
                client,
                pending_path,
                record_path,
                state,
                max_attempts=max_attempts,
                verbose=verbose,
                backup_path=backup_path,
            )
        except Exception as exc:
            log(f"ERROR pending check failed (will retry): {exc}", verbose=True)
        if not load_pending(pending_path):
            return True
        if time.time() >= deadline:
            log(f"record wait timeout after {max_wait}s; falling back to next cycle", verbose=verbose)
            return False
        time.sleep(poll_interval)


def run_once(config: dict[str, Any], *, dry_run: bool, verbose: bool) -> int:
    rss_url = config["rss_url"]
    root = Path(os.path.expanduser(config["anime_root"]))
    aliases = {str(key): list(value) for key, value in config.get("aliases", {}).items()}
    state_path = Path(os.path.expanduser(
        config.get("state_path", Path(__file__).with_name("state.json"))))
    if not acquire_run_lock(state_path):
        log("another mikan-gopeed instance is running; skipping this cycle", verbose=True)
        return 0
    record_path = Path(os.path.expanduser(
        config.get("record_path", Path(__file__).with_name("番剧更新记录.md"))))
    backup_path = Path(os.path.expanduser(
        config.get("record_backup_path", Path(__file__).with_name("record-backup.md"))))
    pending_path = Path(os.path.expanduser(
        config.get("pending_path", Path(__file__).with_name("record-pending.json"))))
    state = load_state(state_path)
    completed_name = config.get("completed_dir_name", "已完結")
    # 护栏：根目录不存在说明番剧库可能被移动/删除。此时绝不能自动重建目录树，
    # 否则会悄悄生成一套"影子库"并把后续更新下进去。跳过下载并提醒。
    root_missing = not root.exists()
    if root_missing:
        notify(f"番剧根目录不存在，本轮跳过下载：{root}（移动过番剧库请更新 config.json 的 anime_root）")
        log(f"ERROR anime root does not exist: {root}", verbose=True)
        folders = []
    else:
        folders = [
            path.name
            for path in root.iterdir()
            if path.is_dir() and path.name != completed_name
        ]

    api_cfg = config.get("gopeed_api", {})
    client = GopeedClient(
        api_cfg.get("base_url", "http://127.0.0.1:9999"),
        api_cfg.get("token", ""),
        api_cfg.get("unix_socket", ""),
        start_app=api_cfg.get("start_app", True),
        app_name=api_cfg.get("app_name", "Gopeed"),
    )
    api_available = gopeed_ready(client, allow_launch=not dry_run, verbose=verbose)
    if not api_available:
        log("Gopeed API is not reachable; tasks can only be skipped this round", verbose=True)
    gopeed_hashes = client.task_hashes() if api_available else set()

    rss_error = ""
    try:
        items = parse_rss(curl_bytes(rss_url))
    except Exception as exc:
        rss_error = sanitize_token(str(exc))
        log(f"ERROR RSS fetch/parse failed (will retry next cycle): {rss_error}", verbose=True)
        if time.time() - state.get("last_rss_notify", 0) > 3600:
            notify("RSS 拉取失败，下轮自动重试（若刚更换 token 请检查 rss_url）")
            state["last_rss_notify"] = time.time()
        items = []
    summary = {
        "items": len(items),
        "skipped": 0,
        "created": 0,
        "would_create": 0,
        "errors": 0,
    }
    log(f"RSS items: {len(items)}", verbose=verbose)

    for item in items:
        if root_missing:
            summary["skipped"] += 1
            continue
        title = item["title"]
        episode = extract_episode(title)
        status, matched_name, match_score = classify_match(
            title,
            folders,
            aliases,
            threshold=float(config.get("match_threshold", 0.62)),
            margin=float(config.get("match_margin", 0.08)),
        )
        if status == "ambiguous":
            summary["errors"] += 1
            log(
                f"AMBIGUOUS {title!r}: closest candidates tie near {matched_name!r} "
                f"({match_score:.2f}); refusing to guess, skipping",
                verbose=True,
            )
            if not dry_run:
                notify(f"番剧目录匹配歧义，未下载：{title}")
            continue
        if status == "new":
            known = state.get("series_folders", {}).get(normalize_text(series_title(title)))
            if known and not dry_run:
                # 这个系列之前下载到过别的目录：多半是目录被改名/移动了。
                # 宁可停下让人确认，也不能自动建一个新文件夹把库裂成两半。
                summary["errors"] += 1
                log(
                    f"GUARD {title!r}: series previously downloaded to {known!r}, "
                    f"which no longer matches; refusing to auto-create a new folder",
                    verbose=True,
                )
                notify(f"番剧目录可能被改名或移动（原：{known}），本轮未下载：{series_title(title)}")
                continue
            target_name, score = new_folder_name(title), 0.0
        else:
            target_name, score = matched_name, match_score
        target = root / target_name
        existing = episode_file(target, episode) if target.exists() else None
        # 先做零成本去重（本地文件 / 状态文件），稳态下不再对 Mikan 重复抓取
        if existing:
            summary["skipped"] += 1
            log(f"SKIP existing file: {title!r} -> {existing}", verbose=verbose)
            if not dry_run:
                mark_seen(state, item, "existing-file")
                state.setdefault("series_folders", {})[normalize_text(series_title(title))] = target_name
                state.get("download_attempts", {}).pop(item_keys(item)[-1], None)
            continue
        if is_seen(state, item):
            summary["skipped"] += 1
            log(f"SKIP local state: {title!r} -> {target_name!r}", verbose=verbose)
            continue

        try:
            source, source_kind, infohash = resolve_item_source(item)
        except Exception as exc:
            summary["errors"] += 1
            log(f"ERROR {title}: {exc}", verbose=True)
            notify(f"番剧源解析失败：{title}")
            continue

        log(
            f"ITEM {title!r} -> {target_name!r} episode={episode} "
            f"match={score:.2f} source={source_kind} btih={infohash or '-'}",
            verbose=verbose,
        )

        if infohash and infohash in gopeed_hashes:
            summary["skipped"] += 1
            log("SKIP Gopeed task hash", verbose=verbose)
            if not dry_run:
                mark_seen(state, item, "gopeed-task")
            continue

        if dry_run:
            summary["would_create"] += 1
            log(f"DRY-RUN would create task in {target}", verbose=True)
            continue

        if not api_available:
            summary["errors"] += 1
            log(f"ERROR cannot create task; Gopeed API is unavailable: {title}", verbose=True)
            notify(f"Gopeed API 不可用，未下载：{title}")
            continue

        try:
            resolved = client.resolve(source, target)
            files = find_resource_files(resolved)
            if not files:
                raise RuntimeError("resource contains no downloadable files")
            selected = video_indexes(files)
            if not selected:
                selected = list(range(len(files)))
            rid = resolved.get("id", "") if isinstance(resolved, dict) else ""
            task_id = client.create(source, target, rid, selected)
            target.mkdir(parents=True, exist_ok=True)
            mark_seen(state, item, "created")
            state.setdefault("series_folders", {})[normalize_text(series_title(title))] = target_name
            pending = load_pending(pending_path)
            pending[str(task_id)] = make_pending_entry(item, target, episode, files)
            save_pending(pending_path, pending)
            summary["created"] += 1
            log(f"CREATED Gopeed task for {title!r} -> {target}", verbose=True)
        except Exception as exc:
            summary["errors"] += 1
            log(f"ERROR Gopeed task creation failed for {title!r}: {exc}", verbose=True)
            notify(f"Gopeed 任务创建失败：{title}")

    if not dry_run:
        if api_available:
            try:
                # 先处理上一轮遗留，再盯梢本轮新任务直到完成（或超时）
                check_pending_downloads(
                    client,
                    pending_path,
                    record_path,
                    state,
                    max_attempts=max(1, int(config.get("max_task_attempts", 3))),
                    verbose=verbose,
                    backup_path=backup_path,
                )
                wait_for_records(
                    client,
                    pending_path,
                    record_path,
                    state,
                    backup_path,
                    max_wait=max(0, int(config.get("record_wait_seconds", 900))),
                    max_attempts=max(1, int(config.get("max_task_attempts", 3))),
                    verbose=verbose,
                )
            except Exception as exc:
                log(f"ERROR pending check failed (will retry next cycle): {exc}", verbose=True)
                notify(f"下载记录更新失败（下轮自动重试）：{exc}")
        # check_pending 会改 state（失败重试/放弃），必须在它之后再持久化
        save_state(state_path, state)
    flush_notifications()
    log(f"SUMMARY {json.dumps(summary, ensure_ascii=False)}", verbose=True)
    if rss_error:
        return 1
    return 1 if summary["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download new Mikan RSS items with Gopeed")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--once", action="store_true", help="run one polling cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="report actions without creating tasks or changing state")
    parser.add_argument("--verbose", action="store_true", help="show per-item matching and dedupe details")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))

    if args.once:
        return run_once(config, dry_run=args.dry_run, verbose=args.verbose)

    interval = max(60, int(config.get("poll_interval_seconds", 600)))
    try:
        while True:
            run_once(config, dry_run=args.dry_run, verbose=args.verbose)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("stopped by user", verbose=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
