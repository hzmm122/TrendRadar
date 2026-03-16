# coding=utf-8
"""
报告辅助函数模块

提供报告生成相关的通用辅助函数
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Sequence, Tuple


def clean_title(title: str) -> str:
    """清理标题中的特殊字符

    清理规则：
    - 将换行符(\n, \r)替换为空格
    - 将多个连续空白字符合并为单个空格
    - 去除首尾空白

    Args:
        title: 原始标题字符串

    Returns:
        清理后的标题字符串
    """
    if not isinstance(title, str):
        title = str(title)
    cleaned_title = title.replace("\n", " ").replace("\r", " ")
    cleaned_title = re.sub(r"\s+", " ", cleaned_title)
    cleaned_title = cleaned_title.strip()
    return cleaned_title


def html_escape(text: str) -> str:
    """HTML特殊字符转义

    转义规则（按顺序）：
    - & → &amp;
    - < → &lt;
    - > → &gt;
    - " → &quot;
    - ' → &#x27;

    Args:
        text: 原始文本

    Returns:
        转义后的文本
    """
    if not isinstance(text, str):
        text = str(text)

    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def format_rank_display(ranks: List[int], rank_threshold: int, format_type: str) -> str:
    """格式化排名显示

    根据不同平台类型生成对应格式的排名字符串。
    当最小排名小于等于阈值时，使用高亮格式。

    Args:
        ranks: 排名列表（可能包含重复值）
        rank_threshold: 高亮阈值，小于等于此值的排名会高亮显示
        format_type: 平台类型，支持:
            - "html": HTML格式
            - "feishu": 飞书格式
            - "dingtalk": 钉钉格式
            - "wework": 企业微信格式
            - "telegram": Telegram格式
            - "slack": Slack格式
            - 其他: 默认markdown格式

    Returns:
        格式化后的排名字符串，如 "[1]" 或 "[1 - 5]"
        如果排名列表为空，返回空字符串
    """
    if not ranks:
        return ""

    unique_ranks = sorted(set(ranks))
    min_rank = unique_ranks[0]
    max_rank = unique_ranks[-1]

    # 根据平台类型选择高亮格式
    if format_type == "html":
        highlight_start = "<font color='red'><strong>"
        highlight_end = "</strong></font>"
    elif format_type == "feishu":
        highlight_start = "<font color='red'>**"
        highlight_end = "**</font>"
    elif format_type == "dingtalk":
        highlight_start = "**"
        highlight_end = "**"
    elif format_type == "wework":
        highlight_start = "**"
        highlight_end = "**"
    elif format_type == "telegram":
        highlight_start = "<b>"
        highlight_end = "</b>"
    elif format_type == "slack":
        highlight_start = "*"
        highlight_end = "*"
    else:
        # 默认 markdown 格式
        highlight_start = "**"
        highlight_end = "**"

    # 生成排名显示
    rank_str = ""
    if min_rank <= rank_threshold:
        if min_rank == max_rank:
            rank_str = f"{highlight_start}[{min_rank}]{highlight_end}"
        else:
            rank_str = f"{highlight_start}[{min_rank} - {max_rank}]{highlight_end}"
    else:
        if min_rank == max_rank:
            rank_str = f"[{min_rank}]"
        else:
            rank_str = f"[{min_rank} - {max_rank}]"

    # 计算热度趋势
    trend_arrow = ""
    if len(ranks) >= 2:
        prev_rank = ranks[-2]
        curr_rank = ranks[-1]
        if curr_rank < prev_rank:
            trend_arrow = "🔺"  # 排名上升（数值变小）
        elif curr_rank > prev_rank:
            trend_arrow = "🔻"  # 排名下降（数值变大）
        else:
            trend_arrow = "➖"  # 排名持平
    # len(ranks) == 1 时不显示趋势箭头（新上榜由 is_new 字段在 formatter.py 中处理）

    return f"{rank_str} {trend_arrow}" if trend_arrow else rank_str


def normalize_title_for_dedup(title: str) -> str:
    """用于去重的标题归一化。

    目标：尽量消除同一内容在不同平台/批次中出现的轻微差异（空白、标点、大小写等），
    但避免过度清洗导致不同内容被误合并。

    规则：
    - 先按 clean_title 清理空白与换行
    - 去除所有 Unicode 标点/符号/空白字符
    - ASCII 字母转小写

    Args:
        title: 原始标题

    Returns:
        归一化后的标题（可能为空字符串）
    """
    cleaned = clean_title(title)
    if not cleaned:
        return ""

    chars: List[str] = []
    for ch in cleaned:
        cat = unicodedata.category(ch)
        # L*: Letter, N*: Number。其余（P/S/Z/C 等）都视为噪声。
        if cat and (cat[0] == "L" or cat[0] == "N"):
            chars.append(ch.lower())

    return "".join(chars)


def _merge_title_data(into: dict, other: dict) -> None:
    """将 other 合并进 into（就地修改）。"""
    into["count"] = int(into.get("count", 1)) + int(other.get("count", 1))

    ranks_a = into.get("ranks") or []
    ranks_b = other.get("ranks") or []
    if ranks_a or ranks_b:
        into["ranks"] = list(ranks_a) + list(ranks_b)

    # 优先保留已有链接/时间；如缺失则补齐
    if not into.get("mobile_url") and other.get("mobile_url"):
        into["mobile_url"] = other.get("mobile_url")
    if not into.get("url") and other.get("url"):
        into["url"] = other.get("url")
    if not into.get("time_display") and other.get("time_display"):
        into["time_display"] = other.get("time_display")

    # 其他可选字段
    if other.get("is_new") and not into.get("is_new"):
        into["is_new"] = True
    if not into.get("matched_keyword") and other.get("matched_keyword"):
        into["matched_keyword"] = other.get("matched_keyword")


def _is_similar_enough(
    a: str,
    b: str,
    *,
    threshold: float,
    min_norm_len: int,
) -> bool:
    """判断两个归一化标题是否足够相似，用于模糊去重。"""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < max(1, int(min_norm_len)):
        return False
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio >= float(threshold)


def dedup_titles(
    titles: Sequence[dict],
    *,
    seen_keys: Optional[set] = None,
) -> Tuple[List[dict], set]:
    """对标题列表去重并合并计数。

    - 同一来源（source_name）下，归一化标题相同的条目会被合并
    - 若提供 seen_keys，则会在合并后进一步跨分组过滤：已出现过的 key 会被跳过

    Args:
        titles: 标题条目列表（保持输入顺序优先）
        seen_keys: 可选的全局已见 key 集合，用于跨分组去重

    Returns:
        (去重后的 titles, 更新后的 seen_keys)
    """
    if seen_keys is None:
        seen_keys = set()

    merged_by_key: dict = {}
    ordered_keys: List[Tuple[str, str]] = []

    for t in titles or []:
        source = str(t.get("source_name", "") or "")
        norm = normalize_title_for_dedup(t.get("title", ""))
        if not source or not norm:
            # 缺少关键字段时，不参与去重
            key = ("", "")
        else:
            key = (source, norm)

        if key != ("", ""):
            if key in merged_by_key:
                _merge_title_data(merged_by_key[key], t)
                continue
            merged_by_key[key] = dict(t)
            ordered_keys.append(key)
        else:
            # 对异常项（无 source 或 norm）保持原样
            ordered_keys.append(("__raw__", str(id(t))))
            merged_by_key[("__raw__", str(id(t)))] = dict(t)

    deduped: List[dict] = []
    for key in ordered_keys:
        item = merged_by_key.get(key)
        if not item:
            continue
        # 仅对标准 key 做跨分组去重；异常项不参与。
        if key[0] != "__raw__" and key in seen_keys:
            continue
        deduped.append(item)
        if key[0] != "__raw__" and key != ("", ""):
            seen_keys.add(key)

    return deduped, seen_keys


def dedup_titles_fuzzy(
    titles: Sequence[dict],
    *,
    similarity_threshold: float,
    min_norm_len: int = 6,
    seen_index: Optional[dict] = None,
) -> Tuple[List[dict], dict]:
    """对标题列表做“相似度”去重并合并计数（同一来源内）。"""
    if seen_index is None:
        seen_index = {}

    threshold = float(similarity_threshold)
    if threshold <= 0:
        # 兼容：阈值<=0 退化为精确去重
        deduped, seen_keys = dedup_titles(titles)
        for source, norm in seen_keys:
            if not source or not norm:
                continue
            seen_index.setdefault(source, []).append(norm)
        return deduped, seen_index

    clusters: List[dict] = []
    cluster_norms: List[Tuple[str, str]] = []

    for t in titles or []:
        source = str(t.get("source_name", "") or "")
        norm = normalize_title_for_dedup(t.get("title", ""))
        if not source or not norm:
            clusters.append(dict(t))
            cluster_norms.append(("__raw__", str(id(t))))
            continue

        # 跨分组去重：如果该来源下已经出现过相似标题，直接跳过
        existed_norms = seen_index.get(source, [])
        if existed_norms and any(
            _is_similar_enough(
                norm,
                existed,
                threshold=threshold,
                min_norm_len=min_norm_len,
            )
            for existed in existed_norms
        ):
            continue

        merged = False
        for idx, (c_source, c_norm) in enumerate(cluster_norms):
            if c_source != source:
                continue
            if _is_similar_enough(
                norm, c_norm, threshold=threshold, min_norm_len=min_norm_len
            ):
                _merge_title_data(clusters[idx], t)
                merged = True
                break

        if not merged:
            clusters.append(dict(t))
            cluster_norms.append((source, norm))

    for source, norm in cluster_norms:
        if source in ("__raw__", "") or not norm:
            continue
        seen_index.setdefault(source, []).append(norm)

    return clusters, seen_index
