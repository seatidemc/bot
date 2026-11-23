"""router 关键词快捷路由：命中词表时直接判定 knowledge，不调用 System One。

词表是独立文本文件，每行一个正则表达式，空行和 ``#`` 开头的行忽略：

- 前缀匹配：``^A``
- 后缀匹配：``A$``
- 任意位置包含：``.*A.*``（等价于直接写 ``A``）

匹配使用 ``re.search``，所以 ``^`` / ``$`` 分别锚定消息开头和结尾，普通词
默认按「包含」处理。关键词是整段消息级别的粗匹配，适合把明显属于服务器
知识库的问题（如「服务器怎么加入」）提前分流，不适合表达复杂的语义规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class KeywordFileError(Exception):
    """关键词文件读取或正则编译失败。"""


@dataclass(frozen=True)
class Rule:
    line: int
    pattern: str
    regex: re.Pattern[str]


def load(path: Path) -> list[Rule]:
    """读取并编译关键词文件；任何一行正则非法都会整体报错。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise KeywordFileError(f"读取失败: {path} ({e})") from e

    rules: list[Rule] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            regex = re.compile(line)
        except re.error as e:
            raise KeywordFileError(
                f"{path}:{lineno} 不是合法正则: {line!r}（{e}）"
            ) from e
        rules.append(Rule(line=lineno, pattern=line, regex=regex))
    return rules


def match(text: str, rules: Sequence[Rule]) -> Rule | None:
    """返回第一条命中的规则；未命中返回 None。"""
    for rule in rules:
        if rule.regex.search(text):
            return rule
    return None