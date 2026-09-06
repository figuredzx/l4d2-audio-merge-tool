# -*- coding: utf-8 -*-
"""
VPK (Valve Pak) v1 / v2 读取，以及 v1 自包含格式写入。
纯标准库实现。路径一律用 / 分隔、小写作为内部键。
"""
import os
import struct
import zlib

SIGNATURE = 0x55AA1234
ARCHIVE_EMBED = 0x7FFF  # archive_index 为该值表示文件数据嵌在 _dir.vpk 内


class VpkReader:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self.data = f.read()
        sig, ver, tree_len = struct.unpack_from('<III', self.data, 0)
        if sig != SIGNATURE:
            raise ValueError(f"不是有效的 VPK 文件: {path}")
        self.version = ver
        off = 12
        if ver == 2:
            # file_data_section_size, archive_md5_size, other_md5_size, signature_size
            off += 16
        elif ver != 1:
            raise ValueError(f"不支持的 VPK 版本: {ver}")
        self.header_size = off
        self.tree_len = tree_len
        self.entries = {}   # 小写路径 -> entry dict
        self._parse_tree()
        self._data_start = off + tree_len  # 内嵌数据区起点（tree 之后）

    def _parse_tree(self):
        data = self.data
        p = self.header_size
        tree_end = self.header_size + self.tree_len

        def cstr(o):
            e = data.index(b'\x00', o)
            return data[o:e].decode('utf-8', 'replace'), e + 1

        while p < tree_end:
            ext, p = cstr(p)
            if ext == '':
                break
            while True:
                directory, p = cstr(p)
                if directory == '':
                    break
                while True:
                    name, p = cstr(p)
                    if name == '':
                        break
                    crc, preload, arc, eoff, elen = struct.unpack_from('<IHHII', data, p)
                    p += 18
                    preload_bytes = data[p:p + preload]
                    p += preload
                    if directory == ' ':
                        full = name if ext == ' ' else f"{name}.{ext}"
                    else:
                        full = f"{directory}/{name}" if ext == ' ' else f"{directory}/{name}.{ext}"
                    self.entries[full.lower().replace('\\', '/')] = {
                        'path': full,
                        'crc': crc,
                        'preload': preload_bytes,
                        'archive': arc,
                        'offset': eoff,
                        'length': elen,
                    }

    def read(self, entry):
        """entry 可以是 entries 里的 dict，或小写路径字符串。"""
        if isinstance(entry, str):
            entry = self.entries[entry.lower().replace('\\', '/')]
        pre = entry['preload']
        if entry['archive'] == ARCHIVE_EMBED:
            start = self._data_start + entry['offset']
            return pre + self.data[start:start + entry['length']]
        # 外部分卷 pak01_XXX.vpk
        base = self.path
        idx = base.lower().rfind('_dir.vpk')
        if idx != -1:
            base = base[:idx]
        arc_path = f"{base}_{entry['archive']:03d}.vpk"
        with open(arc_path, 'rb') as f:
            f.seek(entry['offset'])
            return pre + f.read(entry['length'])


def _build_tree_groups(files):
    """把 {路径: bytes} 组织成 ext -> dir -> name -> bytes 三级结构。
    无扩展名的文件 ext 为 ' '，根目录 dir 为 ' '
    （VPK 树的约定：'' 是各级列表的结束符，故用空格占位）。"""
    groups = {}
    for fpath, content in files.items():
        if isinstance(content, str):
            content = content.encode('utf-8')
        fpath = fpath.replace('\\', '/').strip('/')
        directory, fn = os.path.split(fpath)
        if '.' in fn:
            name, ext = fn.rsplit('.', 1)
        else:
            name, ext = fn, ' '
        if not directory:
            directory = ' '
        groups.setdefault(ext, {}).setdefault(directory, {})[name] = content
    return groups


def write_vpk(path, files):
    """
    写成 v1 自包含 VPK（单文件，数据全部内嵌）。
    files: {相对路径(str, / 分隔): bytes}
    """
    groups = _build_tree_groups(files)

    tree = bytearray()
    data_section = bytearray()

    for ext in sorted(groups):
        tree += ext.encode('utf-8') + b'\x00'
        for directory in sorted(groups[ext]):
            tree += directory.encode('utf-8') + b'\x00'
            for name in sorted(groups[ext][directory]):
                content = groups[ext][directory][name]
                tree += name.encode('utf-8') + b'\x00'
                crc = zlib.crc32(content) & 0xFFFFFFFF
                offset = len(data_section)
                data_section += content
                # 目录条目：CRC(4) preload(2) 分卷号(2) 偏移(4) 长度(4) 终止符0xFFFF(2) = 18 字节
                tree += struct.pack('<IHHIIH', crc, 0, ARCHIVE_EMBED, offset,
                                    len(content), 0xFFFF)
            tree += b'\x00'  # 名字列表结束
        tree += b'\x00'      # 目录列表结束
    tree += b'\x00'          # 扩展名列表结束

    with open(path, 'wb') as f:
        f.write(struct.pack('<III', SIGNATURE, 1, len(tree)))
        f.write(tree)
        f.write(data_section)


def write_addon_vpk(path, root_dir):
    """
    把一个真实的 root 目录流式封包成单文件 v1 addon VPK
    （布局与官方 vpk.exe 一致：头 + 树 + 数据区；条目 prelen=0、
    archive=0x7FFF、offset 为相对数据区起点）。
    返回 (条目数, 数据区字节数)。
    """
    # 收集相对路径（保留原始大小写；内部键小写）
    rels = []           # [(小写相对路径, 原始相对路径, 文件大小)]
    for dp, _dn, fns in os.walk(root_dir):
        for fn in fns:
            ap = os.path.join(dp, fn)
            rel = os.path.relpath(ap, root_dir).replace('\\', '/')
            rels.append((rel.lower(), rel, os.path.getsize(ap)))
    rels.sort()

    # 与 write_vpk 相同的三级分组，但只记录路径和大小
    groups = {}
    for low, orig, size in rels:
        directory, fn = os.path.split(low)
        if '.' in fn:
            name, ext = fn.rsplit('.', 1)
        else:
            name, ext = fn, ' '
        if not directory:
            directory = ' '
        groups.setdefault(ext, {}).setdefault(directory, {})[name] = (low, orig, size)

    # 第一遍：流式计算每个文件的 CRC
    crcs = {}
    for low, orig, size in rels:
        crc = 0
        with open(os.path.join(root_dir, orig), 'rb') as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
        crcs[low] = crc & 0xFFFFFFFF

    # 构建树（offset 按数据区内的顺序分配）
    tree = bytearray()
    ordered = []        # [(原始相对路径, 大小)]，顺序即数据区顺序
    offset = 0
    for ext in sorted(groups):
        tree += ext.encode('utf-8') + b'\x00'
        for directory in sorted(groups[ext]):
            tree += directory.encode('utf-8') + b'\x00'
            for name in sorted(groups[ext][directory]):
                low, orig, size = groups[ext][directory][name]
                tree += name.encode('utf-8') + b'\x00'
                tree += struct.pack('<IHHIIH', crcs[low], 0, ARCHIVE_EMBED,
                                    offset, size, 0xFFFF)
                ordered.append((orig, size))
                offset += size
            tree += b'\x00'
        tree += b'\x00'
    tree += b'\x00'

    # 第二遍：写 头 + 树 + 流式数据
    with open(path, 'wb') as out:
        out.write(struct.pack('<III', SIGNATURE, 1, len(tree)))
        out.write(tree)
        written = 0
        for orig, size in ordered:
            with open(os.path.join(root_dir, orig), 'rb') as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
    return len(ordered), written
