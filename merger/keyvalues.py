# -*- coding: utf-8 -*-
"""
Source 引擎 KeyValues 文本解析 / 序列化。
用于 game_sounds_*.txt 声音脚本。注释在解析时丢弃，输出统一格式，
这样不同来源的同名条目只要语义相同就判为一致。
"""


class KVNode:
    __slots__ = ('key', 'value', 'children')

    def __init__(self, key, value=None, children=None):
        self.key = key
        self.value = value          # 叶子节点的字符串值；块节点为 None
        self.children = children or []

    def is_block(self):
        return self.value is None


def _tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':      # 行注释
            j = text.find('\n', i)
            i = n if j == -1 else j + 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':      # 块注释
            j = text.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == '"':                                           # 引号字符串
            i += 1
            buf = []
            while i < n and text[i] != '"':
                # game_sounds 脚本不做转义处理：\r \s 等原样保留
                # （实测引擎按字面解析 MuisId-Mei\reactive\... 这类路径）
                if text[i] == '\\' and i + 1 < n and text[i + 1] == '"':
                    buf.append('"')
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
            tokens.append(('str', ''.join(buf)))
        elif c == '{':
            tokens.append(('{', '{'))
            i += 1
        elif c == '}':
            tokens.append(('}', '}'))
            i += 1
        else:                                                  # 裸词
            buf = []
            while i < n and not text[i].isspace() and text[i] not in '{}"':
                buf.append(text[i])
                i += 1
            tokens.append(('str', ''.join(buf)))
    return tokens


def _parse_block(tokens, pos):
    nodes = []
    while pos < len(tokens):
        kind, val = tokens[pos]
        if kind == '}':
            return nodes, pos + 1
        if kind == 'str':
            key = val
            pos += 1
            if pos < len(tokens) and tokens[pos][0] == '{':
                pos += 1
                children, pos = _parse_block(tokens, pos)
                nodes.append(KVNode(key, None, children))
            else:
                value = ''
                if pos < len(tokens) and tokens[pos][0] == 'str':
                    value = tokens[pos][1]
                    pos += 1
                nodes.append(KVNode(key, value))
        else:
            pos += 1
    return nodes, pos


def parse_kv(text):
    """返回顶层节点列表。解析失败抛 ValueError。"""
    tokens = _tokenize(text)
    nodes, _ = _parse_block(tokens, 0)
    if not nodes:
        raise ValueError("空脚本或解析失败")
    return nodes


def _esc(s):
    # 值可能含反斜杠路径（引擎按原样解析），只转义引号
    return s.replace('"', '\\"')


def serialize(nodes):
    """把节点列表序列化为 Source 脚本文本（Tab 缩进）。"""
    lines = []

    def emit(node, depth):
        pad = '\t' * depth
        if node.value is not None:
            lines.append(f'{pad}"{_esc(node.key)}"\t\t"{_esc(node.value)}"')
        else:
            lines.append(f'{pad}"{_esc(node.key)}"')
            lines.append(f'{pad}{{')
            for ch in node.children:
                emit(ch, depth + 1)
            lines.append(f'{pad}}}')

    for node in nodes:
        emit(node, 0)
    return '\n'.join(lines) + '\n'


def iter_leaves(node):
    """深度优先遍历所有叶子。"""
    for ch in node.children:
        if ch.is_block():
            yield from iter_leaves(ch)
        else:
            yield ch


def find_waves(nodes):
    """返回节点列表中所有 wave 叶子的值（音效引用路径）。"""
    waves = []

    def walk(node):
        for ch in node.children:
            if ch.is_block():
                walk(ch)
            elif ch.key.lower() == 'wave' and ch.value:
                waves.append(ch.value)

    for n in nodes:
        if n.is_block():
            walk(n)
    return waves
