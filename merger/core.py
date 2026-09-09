# -*- coding: utf-8 -*-
"""
核心逻辑：来源扫描、Steam/游戏目录检测、音频库合并分析、冲突建模、导出。
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zlib

from .vpk import VpkReader, write_vpk, write_addon_vpk
from .keyvalues import parse_kv, serialize, KVNode, find_waves

# 游戏根目录下自带的目录，扫描已部署库时排除
GAME_BUILTIN_DIRS = {
    'left4dead2', 'left4dead2_dlc1', 'left4dead2_dlc2', 'left4dead2_dlc3',
    'update', 'hl2', 'platform', 'bin', 'sdktools', 'sdkcontent',
    'steamapps', 'cfg', 'resource', 'maps', 'download', 'downloads',
    'addonconfig', 'gcfscape', 'docs', 'licenses',
}

# 原版 sound/ 下的顶级目录，用于缺失引用分类
VANILLA_SOUND_DIRS = {
    'weapons', 'player', 'physics', 'ambient', 'music', 'ui', 'vo', 'npc',
    'items', 'doors', 'buttons', 'common', 'world', 'vehicles', 'misc',
    'survivor', 'infected', 'hud', 'menu', 'level', 'intro', 'tech', 'voice',
    'zombie', 'tank', 'boomer', 'hunter', 'smoker', 'witch', 'spitter',
    'jockey', 'charger', 'foley', 'footsteps', 'events', 'game', 'announcer',
    'cards', 'card', 'melee', 'guns', 'reload',
}


def detect_custom_sound_dirs(source):
    """扫描来源脚本的 wave 路径，返回自定义音频目录名列表（排除原版目录）。
    如 wave ")oe/xxx.wav" 返回 'oe'。
    """
    import re
    found = set()
    wave_re = re.compile(rb'"wave"\s+"[^"]*"', re.IGNORECASE)
    for data in source.scripts.values():
        for m in wave_re.finditer(data):
            # 提取引号内路径，去掉前缀 ) 或 # 等
            s = m.group(0)
            # 找到第二个引号内的内容
            parts = s.split(b'"')
            if len(parts) < 3:
                continue
            path = parts[2].strip()
            if path:
                path = path.lstrip(b')#*')
                path_str = path.decode('utf-8', errors='replace')
                # 取顶级目录段
                parts2 = path_str.replace('\\', '/').split('/')
                if parts2:
                    top = parts2[0].lower()
                    if top and top not in VANILLA_SOUND_DIRS and not top.startswith('..'):
                        found.add(top)
    # 同时检查 loose 和 vpk_other 里的音频路径
    for p in source.loose:
        if p.startswith('sound/'):
            parts = p.split('/')
            if len(parts) >= 2:
                top = parts[1]
                if top not in VANILLA_SOUND_DIRS:
                    found.add(top)
    for p, _e in source.vpk_other:
        if p.startswith('sound/'):
            parts = p.split('/')
            if len(parts) >= 2:
                top = parts[1]
                if top not in VANILLA_SOUND_DIRS:
                    found.add(top)
    return sorted(found)


def rename_sound_dir(source, old_name, new_name, log=lambda m: None):
    """将来源脚本 wave 路径和音频文件路径里的 old_name 目录改为 new_name。
    old_name/new_name 不含路径分隔符，仅目录名。
    """
    import re
    old_lower = old_name.lower()
    new_lower = new_name.lower()

    # 改脚本内容里的 wave 路径
    new_scripts = {}
    new_orig = {}
    for k, data in source.scripts.items():
        # 匹配 "wave" "...old_name/..." 并替换
        def repl(m):
            parts = m.group(0).split(b'"')
            if len(parts) < 3:
                return m.group(0)
            path = parts[2]
            # 用正则替换路径中的目录名，保留大小写风格
            path = re.sub(
                rb'(?<![A-Za-z0-9_])' + re.escape(old_name.encode('utf-8')) + rb'(?=/)',
                new_name.encode('utf-8'),
                path,
                flags=re.IGNORECASE)
            parts[2] = path
            return b'"'.join(parts)
        new_data = re.sub(rb'"wave"\s+"[^"]*"', repl, data, flags=re.IGNORECASE)
        new_scripts[k] = new_data
        new_orig[k] = source.script_orig.get(k, k)
    source.scripts = new_scripts
    source.script_orig = new_orig

    # 改 loose 路径（文件路径和原始路径）
    new_loose = {}
    new_loose_orig = {}
    for k, v in source.loose.items():
        nk = k.replace(f'sound/{old_lower}/', f'sound/{new_lower}/')
        new_loose[nk] = v
        ok = source.loose_orig.get(k, k)
        new_loose_orig[nk] = ok.replace(f'sound/{old_lower}/', f'sound/{new_lower}/')
    source.loose = new_loose
    source.loose_orig = new_loose_orig

    # 改 vpk_other 路径（VPK 内音频文件）
    new_other = []
    for p, e in source.vpk_other:
        np = p.replace(f'sound/{old_lower}/', f'sound/{new_lower}/')
        new_other.append((np, e))
    source.vpk_other = new_other

    log(f'已将音频目录 "{old_name}" 重命名为 "{new_name}"')


# ---------------------------------------------------------------- Steam 检测
GAME_MARKERS = ('left4dead2.exe', os.path.join('left4dead2', 'gameinfo.txt'))


def is_valid_game_root(path):
    """判断 path 是否是 L4D2 游戏根目录；返回规范化后的根目录或 None。"""
    if not path or not os.path.isdir(path):
        return None
    path = os.path.normpath(path)
    # 用户可能直接选了 left4dead2 子目录，取其父级
    if os.path.basename(path).lower() == 'left4dead2':
        parent = os.path.dirname(path)
        if os.path.isfile(os.path.join(parent, 'left4dead2.exe')) or \
                os.path.isfile(os.path.join(parent, 'left4dead2', 'gameinfo.txt')):
            return parent
    for m in GAME_MARKERS:
        if os.path.isfile(os.path.join(path, m)):
            return path
    return None


def _settings_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'settings.json')


def load_saved_game_root():
    """上次手动指定的游戏目录（存在且仍有效才返回）。"""
    try:
        import json
        with open(_settings_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return is_valid_game_root(data.get('game_root'))
    except Exception:
        return None


def save_game_root(path):
    try:
        import json
        with open(_settings_path(), 'w', encoding='utf-8') as f:
            json.dump({'game_root': path}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _registry_steam_paths():
    """从注册表取 Steam 安装目录（HKCU + HKLM 32/64 位视图）。"""
    paths = []
    try:
        import winreg
    except ImportError:
        return paths
    keys = [
        (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 0),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\Valve\Steam', 0),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\Valve\Steam', winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\Valve\Steam', winreg.KEY_WOW64_64KEY),
    ]
    for hive, sub, view in keys:
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as k:
                p, _t = winreg.QueryValueEx(k, 'SteamPath')
                if p:
                    p = os.path.normpath(p)
                    if os.path.isdir(p) and p not in paths:
                        paths.append(p)
        except Exception:
            continue
    return paths


def _probe_dir(path, timeout=2.0):
    """带超时的 os.path.isdir。

    休眠中的机械盘、读卡器空槽、失联的网络盘等会让 isdir 卡住数十秒，
    这里放到临时线程里探测，超时一律视为不存在（线程事后自行废弃）。
    """
    box = []

    def work():
        try:
            box.append(os.path.isdir(path))
        except OSError:
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return bool(box)


def _existing_drives():
    """真实存在的本地盘符。用系统 API 位掩码直接获取——不要逐个探测盘符，
    在部分系统上探测不存在的盘符（网络/虚拟盘残留）每次可能卡数秒。
    光驱（CDROM/虚拟光驱）直接排除，探测它同样可能卡住。"""
    if sys.platform == 'win32':
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        out = []
        for i in range(26):
            if not mask >> i & 1:
                continue
            drive = f'{chr(ord("A") + i)}:\\'
            if k32.GetDriveTypeW(ctypes.c_wchar_p(drive)) == 5:  # DRIVE_CDROM
                continue
            out.append(drive)
        return out
    import string
    return [f'{l}:\\' for l in string.ascii_uppercase
            if os.path.exists(f'{l}:\\')]


def find_steam_roots():
    """返回所有能找到的 Steam 安装目录（可能多个，按大小写不敏感去重）。

    只查：注册表 -> C 盘默认位置 ->（都没有时）各存在盘符的根目录看一眼。
    不做任何深层扫描，找不到就快速返回。
    """
    roots, seen = [], set()

    def add(p):
        p = os.path.normpath(p)
        k = p.lower()
        if _probe_dir(os.path.join(p, 'steamapps')) and k not in seen:
            seen.add(k)
            roots.append(p)

    for p in _registry_steam_paths():
        add(p)
    for c in (r'C:\Program Files (x86)\Steam',
              r'C:\Program Files\Steam', r'C:\Steam'):
        add(c)
    if not roots:
        for d in _existing_drives():
            for sub in ('Steam', 'SteamLibrary'):
                add(os.path.join(d, sub))
    return roots


def _library_folders(steam_root):
    """解析 libraryfolders.vdf，返回所有 Steam 库路径（含 \\ 转义还原）。"""
    libs, seen = [], set()
    for vdf in (os.path.join(steam_root, 'steamapps', 'libraryfolders.vdf'),
                os.path.join(steam_root, 'config', 'libraryfolders.vdf')):
        if not os.path.isfile(vdf):
            continue
        try:
            text = open(vdf, 'r', encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = os.path.normpath(m.group(1).replace('\\\\', '\\'))
            # vdf 里可能列出已失联的库盘（休眠盘/读卡器），isdir 会卡死，用带超时探测
            if _probe_dir(p) and p.lower() not in seen:
                seen.add(p.lower())
                libs.append(p)
    return libs


def find_l4d2_dirs():
    """返回所有检测到的 L4D2 游戏根目录列表（含 left4dead2 子目录的那一级）。

    查找顺序：上次手动指定的目录 -> 注册表/C 盘默认位置找到的 Steam 及其
    libraryfolders.vdf 里的全部库 ->（都没找到游戏时）存在盘符根目录看一眼。
    全程浅层探测，找不到就快速返回，由用户手动指定。
    """
    roots = []
    saved = load_saved_game_root()
    if saved:
        roots.append(saved)

    def check(game):
        # 候选路径可能在问题盘上（读卡器/休眠盘），先带超时探一下目录存在性
        if not _probe_dir(game):
            return
        game = is_valid_game_root(game)
        if game and game.lower() not in (r.lower() for r in roots):
            roots.append(game)

    lib_roots = []
    for steam in find_steam_roots():
        lib_roots.append(steam)
        lib_roots.extend(_library_folders(steam))
    for lib in lib_roots:
        check(os.path.join(lib, 'steamapps', 'common', 'Left 4 Dead 2'))
    # Steam 本体都没找到、或库里没有游戏：盘符根目录看一眼常见安装形态
    found = any(r.lower() != (saved or '').lower() for r in roots)
    if not found:
        for lib in lib_roots:
            check(lib)
        if not any(r.lower() != (saved or '').lower() for r in roots):
            for d in _existing_drives():
                check(os.path.join(d, 'SteamLibrary', 'steamapps', 'common', 'Left 4 Dead 2'))
                check(os.path.join(d, 'Steam', 'steamapps', 'common', 'Left 4 Dead 2'))
                check(os.path.join(d, 'steamapps', 'common', 'Left 4 Dead 2'))
    return roots


def find_l4n(game_root):
    """检测 l4n(nekomimi) 平台的缓存生成工具，返回 source_nekomimi.exe 路径或 None。"""
    if not game_root:
        return None
    exe = os.path.join(game_root, 'bin', 'neko', 'source_nekomimi.exe')
    return exe if os.path.isfile(exe) else None


def build_sound_cache(sound_dir, neko_exe, log=lambda m: None, timeout=600):
    """调用 l4n 的 source_nekomimi.exe 为 sound 目录生成 sound.cache。

    返回 (ok: bool, cache_path)。output 逐行回调 log。"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        proc = subprocess.run([neko_exe, 'build_sound_cache', sound_dir],
                              cwd=os.path.dirname(neko_exe), capture_output=True,
                              timeout=timeout, creationflags=flags)
        out = ((proc.stdout or b'') + (proc.stderr or b'')).decode('utf-8', errors='replace')
        for line in out.splitlines():
            line = line.strip()
            if line:
                log(line)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        log('l4n 生成缓存超时')
        return False, None
    except OSError as e:
        log(f'无法运行 l4n 工具：{e}')
        return False, None
    cache = os.path.join(sound_dir, 'sound.cache')
    ok = rc == 0 and os.path.isfile(cache)
    # 大写 .WAV 后缀会导致 l4n 工具异常（其官方说明已注明）
    bad = []
    for dp, _dn, fns in os.walk(sound_dir):
        for fn in fns:
            if fn.endswith('.WAV'):
                bad.append(os.path.relpath(os.path.join(dp, fn), sound_dir))
    if bad:
        log(f'警告：{len(bad)} 个文件是大写 .WAV 后缀，l4n 工具可能无法处理它们，'
            f'例如：{bad[0]}')
    if ok:
        log(f'已生成 {cache}（{os.path.getsize(cache)} 字节）')
    else:
        log(f'缓存生成失败（退出码 {rc}）')
    return ok, cache if ok else None


def find_deployed_libs(game_root):
    """扫描游戏根目录下已部署的音频库文件夹（含 pak01_dir.vpk 的非内置目录）。"""
    libs = []
    try:
        names = os.listdir(game_root)
    except OSError:
        return libs
    for name in sorted(names):
        p = os.path.join(game_root, name)
        if not os.path.isdir(p) or name.lower() in GAME_BUILTIN_DIRS:
            continue
        if os.path.isfile(os.path.join(p, 'pak01_dir.vpk')):
            libs.append(p)
    return libs


def discover_lib_dirs(folder, max_depth=3):
    """
    在模组包文件夹内查找库根目录（含 pak01_dir.vpk 的目录）。
    跳过 left4dead2 目录（那是 gameinfo 参考）。
    """
    found = []
    folder = os.path.abspath(folder)

    def walk(d, depth):
        try:
            entries = os.listdir(d)
        except OSError:
            return
        if 'pak01_dir.vpk' in entries:
            found.append(d)
            return  # 库目录内部不再下钻
        if depth <= 0:
            return
        for name in entries:
            p = os.path.join(d, name)
            if os.path.isdir(p) and name.lower() != 'left4dead2':
                walk(p, depth - 1)

    walk(folder, max_depth)
    return found


# ---------------------------------------------------------------- 数据来源

class Source:
    """一个音频库来源（一个库文件夹）。"""

    def __init__(self, label, kind, lib_dir, pkg_root=None, vpk_file=None):
        self.label = label            # 显示名
        self.kind = kind              # 'pkg' 模组包 | 'gamelib' 游戏目录已部署
        self.lib_dir = os.path.abspath(lib_dir)
        self.pkg_root = pkg_root      # 模组包根目录（gamelib 为 None）
        self.vpk_file = vpk_file      # 显式指定的 VPK 文件（直接选 .vpk 文件时用）
        self.vpk = None
        self.scripts = {}             # scripts/xxx.txt(小写) -> bytes
        self.script_orig = {}         # 小写路径 -> 原始大小写路径（导出用）
        self.vpk_other = []           # [(小写路径, entry)] VPK 内非脚本文件
        self.loose = {}               # 小写 sound/... 路径 -> 绝对路径
        self.loose_orig = {}          # 小写路径 -> 原始大小写相对路径（导出用）
        self.script_entries = 0       # 脚本内音效条目数（粗略统计）
        self.audio_count = 0
        self.error = None

    @property
    def id(self):
        return self.vpk_file or self.lib_dir

    def load(self, log=lambda m: None):
        try:
            vpk_path = self.vpk_file or os.path.join(self.lib_dir, 'pak01_dir.vpk')
            if os.path.isfile(vpk_path):
                self.vpk = VpkReader(vpk_path)
                for low, entry in self.vpk.entries.items():
                    if low.endswith('/sound.cache') or low == 'sound.cache':
                        continue
                    if low.endswith('.txt'):
                        # addoninfo.txt 是 mod 元数据，不是声音脚本
                        if low == 'addoninfo.txt' or low.endswith('/addoninfo.txt'):
                            self.vpk_other.append((low, entry))
                        else:
                            # 音频库 VPK 里的其他 .txt 都按声音脚本处理
                            # （脚本可能在 scripts/ 下，也可能在自定义目录，如 MuisId-Mei/…）
                            self.scripts[low] = self.vpk.read(entry)
                            self.script_orig[low] = entry['path']
                    else:
                        self.vpk_other.append((low, entry))
            # 松散文件：收集 sound/ 下音频 + scripts/ 下松散脚本；
            # 跳过 pak01_dir/ 解包镜像（引擎不读它）和根目录零散文档
            # 独立 VPK 文件来源：只扫 vpk 同目录下的 sound/ 子目录
            # （有些音频库的音频不在 VPK 内，而是同目录的 sound/ 下）
            if self.vpk_file:
                sound_dir = os.path.join(self.lib_dir, 'sound')
                if os.path.isdir(sound_dir):
                    for dp, _dn, fns in os.walk(sound_dir):
                        for fn in fns:
                            ap = os.path.join(dp, fn)
                            rel = os.path.relpath(ap, self.lib_dir).replace('\\', '/')
                            low = rel.lower()
                            if fn.lower() == 'sound.cache':
                                continue
                            if low.startswith('sound/'):
                                self.loose[low] = ap
                                self.loose_orig[low] = rel
            else:
                for dp, _dn, fns in os.walk(self.lib_dir):
                    for fn in fns:
                        ap = os.path.join(dp, fn)
                        rel = os.path.relpath(ap, self.lib_dir).replace('\\', '/')
                        low = rel.lower()
                        if low.startswith('pak01') and low.endswith('.vpk'):
                            continue
                        if low.startswith('pak01_dir/'):
                            continue  # pak01_dir.vpk 的解包镜像，引擎不读取
                        if fn.lower() == 'sound.cache':
                            continue
                        if low.startswith('sound/'):
                            self.loose[low] = ap
                            self.loose_orig[low] = rel
                        elif low.startswith('scripts/') and low.endswith('.txt'):
                            # 松散脚本覆盖 VPK 内同路径脚本（引擎松散文件优先）
                            with open(ap, 'rb') as f:
                                self.scripts[low] = f.read()
                            self.script_orig[low] = rel
            self.script_entries = sum(
                max(1, data.count(b'"') // 8) for data in self.scripts.values()
            )
            self.audio_count = (
                sum(1 for p in self.loose if p.endswith('.wav'))
                + sum(1 for p, _e in self.vpk_other
                      if p.startswith('sound/') and p.endswith('.wav'))
            )
        except Exception as e:
            self.error = str(e)
            raise


def make_sources_from_package(folder, log=lambda m: None):
    """从模组包文件夹创建 Source 列表。"""
    out = []
    pkg_root = os.path.abspath(folder)
    for lib_dir in discover_lib_dirs(pkg_root):
        label = os.path.basename(lib_dir)
        # 若库目录不在包根下，标签带上相对路径信息
        rel = os.path.relpath(lib_dir, pkg_root)
        if rel != '.':
            label = f"{os.path.basename(pkg_root)}/{rel}".replace('\\', '/')
        s = Source(label, 'pkg', lib_dir, pkg_root=pkg_root)
        s.load(log=log)
        out.append(s)
    return out


def make_sources_from_game(game_root, log=lambda m: None):
    """从游戏目录已部署的库创建 Source 列表。"""
    out = []
    for lib_dir in find_deployed_libs(game_root):
        s = Source(os.path.basename(lib_dir), 'gamelib', lib_dir)
        s.load(log=log)
        out.append(s)
    return out


def make_sources_from_vpk(vpk_path, log=lambda m: None):
    """从独立 VPK 文件创建 Source（VPK 本身就是一个音频库）。"""
    vpk_path = os.path.abspath(vpk_path)
    label = os.path.basename(vpk_path)
    if label.lower().endswith('.vpk'):
        label = label[:-4]
    # lib_dir 用 vpk 所在目录；松散文件扫描基于此目录（通常没有松散文件，无碍）
    s = Source(label, 'pkg', os.path.dirname(vpk_path),
               pkg_root=os.path.dirname(vpk_path), vpk_file=vpk_path)
    s.load(log=log)
    return [s]


# ---------------------------------------------------------------- WAV 校验

def wav_info(data):
    """解析 RIFF/WAV 头，返回 (channels, sample_rate, bits, format_tag)。"""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    p = 12
    while p + 8 <= len(data):
        cid = data[p:p + 4]
        size = struct.unpack_from('<I', data, p + 4)[0]
        if cid == b'fmt ':
            fmt_tag, ch, rate = struct.unpack_from('<HHI', data, p + 8)
            bits = struct.unpack_from('<H', data, p + 22)[0]
            return ch, rate, bits, fmt_tag
        p += 8 + size + (size & 1)
    return None


# ---------------------------------------------------------------- 合并分析

class Candidate:
    """冲突候选项：某个来源提供的一份内容。"""
    __slots__ = ('source', 'crc', 'size', 'origin', 'detail')

    def __init__(self, source, crc, size, origin, detail=None):
        self.source = source       # Source
        self.crc = crc
        self.size = size
        self.origin = origin       # 'loose' | 'vpk'
        self.detail = detail       # loose: 绝对路径；vpk: (路径, entry)


class Conflict:
    """一处需要裁决的差异。"""
    def __init__(self, kind, key, title, candidates):
        self.kind = kind           # 'script' | 'file' | 'rawscript'
        self.key = key             # 唯一键
        self.title = title         # 显示名
        self.candidates = candidates  # 按来源优先级排序
        self.decision = None       # 选定的 Source；None=默认(最高优先级)

    def winner(self):
        if self.decision is not None:
            for c in self.candidates:
                if c.source is self.decision:
                    return c
        return self.candidates[0]


class Analysis:
    def __init__(self, sources):
        self.sources = sources     # 优先级从高到低
        self.script_entries = {}   # 文件(小写) -> 条目名(小写) -> [(Source, KVNode, canon)]
        self.script_order = {}     # 文件 -> [条目名小写] （按优先级来源顺序）
        self.script_name_orig = {} # (文件, 小写名) -> 原始名
        self.manifest_lines = {}   # manifest 文件 -> [(key, value, Source)] 已按优先级去重
        self.raw_scripts = {}      # 文件 -> [Candidate]（无法按 KV 解析的脚本）
        self.file_groups = {}      # 小写路径 -> [Candidate]
        self.file_orig = {}        # 小写路径 -> 原始大小写路径（导出用）
        self.conflicts = []
        self.missing = []          # [(引用路径, 级别)] 级别: 'missing' | 'vanilla'
        self.warnings = []         # [(路径, 说明)]
        self.stats = {}

    # ---- 收集 ----
    def run(self, game_root=None, log=lambda m: None):
        log('正在解析各来源脚本与音频…')
        for src in self.sources:
            self._collect_source(src, log)

        log('正在分析脚本条目冲突…')
        self._build_script_conflicts()
        log('正在分析音频文件冲突…')
        self._build_file_conflicts()
        log('正在校验音效引用…')
        self._check_refs(game_root, log)
        self._calc_stats()
        log('分析完成。')

    def _collect_source(self, src, log):
        # 脚本
        for fpath, data in src.scripts.items():
            text = data.decode('utf-8', errors='replace')
            is_manifest = fpath.endswith('game_sounds_manifest.txt')
            try:
                nodes = parse_kv(text)
            except Exception:
                crc = zlib.crc32(data) & 0xFFFFFFFF
                self.raw_scripts.setdefault(fpath, []).append(
                    Candidate(src, crc, len(data), 'vpk', (fpath, data)))
                continue
            if is_manifest:
                lines = self.manifest_lines.setdefault(fpath, [])
                seen = {(k.lower(), v.lower()) for k, v, _s in lines}
                for n in nodes:
                    if n.is_block():
                        for leaf in _iter_leaves(n):
                            pair = (leaf.key.lower(), leaf.value.lower())
                            if pair not in seen and leaf.value:
                                seen.add(pair)
                                lines.append((leaf.key, leaf.value, src))
                continue
            order = self.script_order.setdefault(fpath, [])
            entries = self.script_entries.setdefault(fpath, {})
            per_file_seen = set()
            for node in nodes:
                name = node.key.strip().lower()
                if not name:
                    continue
                canon = serialize([node])
                lst = entries.setdefault(name, [])
                # 同一来源内同名条目：后者覆盖前者
                lst[:] = [c for c in lst if c[0] is not src]
                lst.append((src, node, canon))
                self.script_name_orig[(fpath, name)] = node.key
                if name not in order and name not in per_file_seen:
                    order.append(name)
                    per_file_seen.add(name)

        # 音频 / VPK 内其他文件
        for low, ap in src.loose.items():
            try:
                size = os.path.getsize(ap)
                with open(ap, 'rb') as f:
                    crc = zlib.crc32(f.read()) & 0xFFFFFFFF
            except OSError:
                continue
            self.file_orig.setdefault(low, src.loose_orig.get(low, low))
            self.file_groups.setdefault(low, []).append(
                Candidate(src, crc, size, 'loose', ap))
        for low, entry in src.vpk_other:
            self.file_orig.setdefault(low, entry.get('path', low))
            self.file_groups.setdefault(low, []).append(
                Candidate(src, entry['crc'], entry['length'], 'vpk',
                          (low, entry)))

    # ---- 冲突 ----
    def _build_script_conflicts(self):
        for fpath, entries in sorted(self.script_entries.items()):
            for name, cands in sorted(entries.items()):
                canon_set = {c[2] for c in cands}
                if len(canon_set) > 1:
                    ordered = sorted(cands, key=lambda c: self._pri(c[0]))
                    conf = Conflict(
                        'script',
                        f'script:{fpath}:{name}',
                        f'{fpath}  →  {self.script_name_orig.get((fpath, name), name)}',
                        [Candidate(c[0], zlib.crc32(c[2].encode("utf-8")) & 0xFFFFFFFF,
                                   len(c[2]), 'script', c) for c in ordered])
                    self.conflicts.append(conf)
        for fpath, cands in sorted(self.raw_scripts.items()):
            crcs = {c.crc for c in cands}
            if len(crcs) > 1:
                ordered = sorted(cands, key=lambda c: self._pri(c.source))
                self.conflicts.append(
                    Conflict('rawscript', f'rawscript:{fpath}', fpath, ordered))

    def _build_file_conflicts(self):
        for path, cands in sorted(self.file_groups.items()):
            crcs = {c.crc for c in cands}
            if len(crcs) > 1:
                ordered = sorted(cands, key=lambda c: self._pri(c.source))
                self.conflicts.append(
                    Conflict('file', f'file:{path}', path, ordered))

    def _pri(self, src):
        try:
            return self.sources.index(src)
        except ValueError:
            return len(self.sources)

    # ---- 引用校验 ----
    def _check_refs(self, game_root, log):
        audio_set = {p for p in self.file_groups if p.startswith('sound/')}
        vanilla = self._vanilla_sound_paths(game_root, log)
        merged = self.merged_scripts()
        for fpath, text in merged.items():
            try:
                nodes = parse_kv(text)
            except Exception:
                continue
            for ref in find_waves(nodes):
                rel = _strip_wave_prefix(ref).replace('\\', '/').lower().lstrip('/')
                if not rel:
                    continue
                target = 'sound/' + rel
                if target in audio_set:
                    continue
                top = rel.split('/')[0]
                if target in vanilla or top in VANILLA_SOUND_DIRS:
                    self.missing.append((target, 'vanilla'))
                else:
                    self.missing.append((target, 'missing'))

    def _vanilla_sound_paths(self, game_root, log):
        paths = set()
        if not game_root:
            return paths
        for d in ('left4dead2', 'left4dead2_dlc1', 'left4dead2_dlc2',
                  'left4dead2_dlc3', 'update'):
            p = os.path.join(game_root, d, 'pak01_dir.vpk')
            if os.path.isfile(p):
                try:
                    r = VpkReader(p)
                    paths.update(k for k in r.entries if k.startswith('sound/'))
                    log(f'已读取原版目录 {d} 的音频清单')
                except Exception:
                    pass
        return paths

    # ---- 合并结果 ----
    def script_orig_path(self, low_path):
        """小写脚本路径 -> 原始大小写路径（跨来源取最先记录的）。"""
        for src in self.sources:
            orig = src.script_orig.get(low_path)
            if orig:
                return orig
        return low_path

    def merged_scripts(self):
        """返回 {脚本路径(原始大小写): 合并后文本}。"""
        out = {}
        # manifest
        for fpath, lines in self.manifest_lines.items():
            root = KVNode('game_sounds_manifest', None, [
                KVNode(k, v) for k, v, _s in lines
            ])
            out[self.script_orig_path(fpath)] = serialize([root])
        # 普通脚本
        for fpath, entries in self.script_entries.items():
            if fpath in self.manifest_lines:
                continue
            chosen = []
            for name in self.script_order.get(fpath, []):
                cands = entries[name]
                conf = self._find_conflict('script', f'script:{fpath}:{name}')
                if conf is not None:
                    win_src = conf.winner().source
                    node = next(c[1] for c in cands if c[0] is win_src)
                else:
                    node = cands[0][1]
                chosen.append(node)
            out[self.script_orig_path(fpath)] = serialize(chosen)
        # 无法解析的脚本
        for fpath, cands in self.raw_scripts.items():
            conf = self._find_conflict('rawscript', f'rawscript:{fpath}')
            if conf is not None:
                cand = conf.winner()
            else:
                cand = sorted(cands, key=lambda c: self._pri(c.source))[0]
            out[self.script_orig_path(fpath)] = cand.detail[1].decode('utf-8', errors='replace')
        return out

    def _find_conflict(self, kind, key):
        for c in self.conflicts:
            if c.kind == kind and c.key == key:
                return c
        return None

    def winner_for_file(self, path):
        conf = self._find_conflict('file', f'file:{path}')
        if conf is not None:
            return conf.winner()
        cands = self.file_groups.get(path, [])
        return sorted(cands, key=lambda c: self._pri(c.source))[0] if cands else None

    def _calc_stats(self):
        n_script_files = len(self.script_entries) + len(self.raw_scripts) \
            + len(self.manifest_lines)
        n_entries = sum(len(v) for v in self.script_entries.values())
        n_audio = sum(1 for p in self.file_groups if p.startswith('sound/'))
        n_conf_script = sum(1 for c in self.conflicts if c.kind == 'script')
        n_conf_file = sum(1 for c in self.conflicts if c.kind == 'file')
        n_missing = sum(1 for _p, lvl in self.missing if lvl == 'missing')
        self.stats = dict(
            sources=len(self.sources),
            script_files=n_script_files,
            entries=n_entries,
            audio=n_audio,
            conflict_script=n_conf_script,
            conflict_file=n_conf_file,
            missing=n_missing,
        )

    # ---- WAV 格式警告（导出前可调用）----
    def check_wav_formats(self, log=lambda m: None):
        warnings = []
        for path, cands in self.file_groups.items():
            if not path.endswith('.wav'):
                continue
            cand = self.winner_for_file(path)
            try:
                data = _read_candidate_bytes(cand, 44)
                info = wav_info(data)
                if info is None:
                    warnings.append((path, '不是标准 WAV 文件'))
                else:
                    ch, rate, bits, tag = info
                    if tag != 1 or bits != 16 or rate != 44100:
                        warnings.append(
                            (path, f'格式: {rate}Hz/{bits}bit/{"PCM" if tag == 1 else "压缩"}，'
                                   f'建议 44100Hz/16bit/PCM'))
            except Exception:
                pass
        self.warnings = warnings
        return warnings


def _iter_leaves(node):
    for ch in node.children:
        if ch.is_block():
            yield from _iter_leaves(ch)
        else:
            yield ch


def _strip_wave_prefix(ref):
    """剥掉 wave 路径前缀符号（ ) > # * ! 等），返回相对 sound/ 的路径。"""
    ref = ref.strip()
    while ref and not (ref[0].isalnum() or ref[0] in '/\\'):
        ref = ref[1:]
    return ref


def _read_candidate_bytes(cand, limit=None):
    if cand.origin == 'loose':
        with open(cand.detail, 'rb') as f:
            return f.read() if limit is None else f.read(limit)
    else:
        _low, entry = cand.detail
        data = cand.source.vpk.read(entry)
        return data if limit is None else data[:limit]


# ---------------------------------------------------------------- 导出

GAMEINFO_TEMPLATE = """\
"GameInfo"
{
\tgame\t"Left 4 Dead 2"\t// Window title
\ttype\tmultiplayer_only
\tnomodels 1
\tnohimodel 1
\tl4dcrosshair 1
\thidden_maps
\t{
\t\t"test_speakers"\t\t1
\t\t"test_hardware"\t\t1
\t}
\tnodegraph 0
\tperfwizard 0
\tSupportsXbox360 1
\tSupportsDX8\t0
\tGameData\t"left4dead2.fgd"

\tFileSystem
\t{
\t\tSteamAppId\t\t\t\t550
\t\tToolsAppId\t\t\t\t563

\t\tSearchPaths
\t\t{
\t\t\tGame\t\t\t\t__LIB__
\t\t\tGame\t\t\t\tupdate
\t\t\tGame\t\t\t\tleft4dead2_dlc3
\t\t\tGame\t\t\t\tleft4dead2_dlc2
\t\t\tGame\t\t\t\tleft4dead2_dlc1
\t\t\tGame\t\t\t\t|gameinfo_path|.
\t\t\tGame\t\t\t\thl2
\t\t}
\t}
}
"""


def patch_gameinfo(text, lib_name):
    """在 gameinfo.txt 的 SearchPaths 块顶部插入 Game <lib_name>；已存在则不重复。"""
    lines = text.splitlines()
    out = []
    in_search = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith('searchpaths'):
            in_search = True
        if in_search and stripped == '{' and not inserted:
            # 找到 SearchPaths 后的 { ，在下一行插入
            out.append(line)
            indent = line[:len(line) - len(line.lstrip())] + '\t'
            if not _gameinfo_has_lib(text, lib_name):
                out.append(f'{indent}Game\t\t\t\t{lib_name}')
                inserted = True
            in_search = False
            continue
        out.append(line)
    if not inserted and not _gameinfo_has_lib(text, lib_name):
        return None
    return '\n'.join(out) + '\n'


def _gameinfo_has_lib(text, lib_name):
    return re.search(r'Game\s+"?' + re.escape(lib_name) + r'"?\s*$',
                     text, re.MULTILINE | re.IGNORECASE) is not None


def export_merge(analysis, out_parent, lib_name, game_root=None,
                 log=lambda m: None, progress=lambda cur, total, msg: None):
    """
    导出合并音频库。
    out_parent 下生成「额外音频-<lib_name>」文件夹，内含 <lib_name>/ 库目录、
    left4dead2/gameinfo.txt、使用方法.txt。
    """
    if not re.match(r'^[A-Za-z0-9_\-+]+$', lib_name):
        raise ValueError('库名只能包含英文字母、数字、下划线、短横线和加号（不能有空格）')

    out_root = os.path.join(out_parent, f'额外音频-{lib_name}')
    lib_dir = os.path.join(out_root, lib_name)
    if os.path.exists(out_root):
        shutil.rmtree(out_root)
    os.makedirs(lib_dir)

    # 1. 合并脚本 -> pak01_dir.vpk
    log('写入合并脚本 pak01_dir.vpk …')
    files_for_vpk = {}
    for fpath, text in analysis.merged_scripts().items():
        files_for_vpk[fpath] = text.encode('utf-8')
    # VPK 内非音频、非脚本文件透传（按裁决）
    passthrough = [p for p in analysis.file_groups
                   if not p.startswith('sound/')]
    for i, path in enumerate(passthrough):
        cand = analysis.winner_for_file(path)
        if cand.origin == 'vpk' and cand.source.vpk is not None:
            files_for_vpk[path] = cand.source.vpk.read(cand.detail[1])
    write_vpk(os.path.join(lib_dir, 'pak01_dir.vpk'), files_for_vpk)
    progress(1, 4, '脚本已打包')

    # 2. 音频文件（松散 sound/）
    log('拷贝合并音频文件 …')
    sound_paths = [p for p in analysis.file_groups if p.startswith('sound/')]
    total = max(1, len(sound_paths))
    for i, path in enumerate(sound_paths):
        cand = analysis.winner_for_file(path)
        rel_orig = analysis.file_orig.get(path, path)
        dst = os.path.join(lib_dir, rel_orig.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if cand.origin == 'loose':
            shutil.copyfile(cand.detail, dst)
        else:
            _low, entry = cand.detail
            with open(dst, 'wb') as f:
                f.write(cand.source.vpk.read(entry))
        if i % 20 == 0:
            progress(i, total, f'音频 {i}/{total}')
    progress(total, total, f'音频 {total}/{total}')

    # 3. gameinfo.txt
    log('生成 gameinfo.txt …')
    gi_dir = os.path.join(out_root, 'left4dead2')
    os.makedirs(gi_dir, exist_ok=True)
    gi_text = None
    if game_root:
        real = os.path.join(game_root, 'left4dead2', 'gameinfo.txt')
        if os.path.isfile(real):
            try:
                base = open(real, 'r', encoding='utf-8', errors='replace').read()
                gi_text = patch_gameinfo(base, lib_name)
            except Exception:
                gi_text = None
    if gi_text is None:
        gi_text = GAMEINFO_TEMPLATE.replace('__LIB__', lib_name)
    with open(os.path.join(gi_dir, 'gameinfo.txt'), 'w', encoding='utf-8') as f:
        f.write(gi_text)

    # 4. 使用方法
    usage = (
        '【音频库合并工具 - 使用方法】\n\n'
        f'1. 把本文件夹里的「{lib_name}」和「left4dead2」两个文件夹，\n'
        '   复制到求生之路2游戏根目录（即 left4dead2 文件夹所在的那一级），\n'
        '   合并覆盖（left4dead2\\gameinfo.txt 会被替换，已在游戏目录部署过\n'
        '   其他音频库的话，gameinfo 里原有的库行会保留）。\n\n'
        '2. 进入游戏，按 ~ 打开控制台，输入以下命令并回车，生成声音缓存：\n\n'
        f'   snd_buildsoundcachefordirectory ../{lib_name}\n\n'
        '3. 以后更新音频库重新合并导出后，重复以上步骤；\n'
        f'   库名保持为 {lib_name} 时，gameinfo 无需重复替换。\n'
    )
    with open(os.path.join(out_root, '使用方法.txt'), 'w', encoding='utf-8') as f:
        f.write(usage)

    progress(4, 4, '导出完成')
    log(f'导出完成：{out_root}')
    return out_root


# ---------------------------------------------------------------------------
# l4n 平台打包导出（把音频库打进 addon VPK，需要客户端安装 l4n 才能加载）
# ---------------------------------------------------------------------------

def find_vpk_exe(game_root):
    """官方 vpk.exe（Add-on Support / l4d2tool 提供），用于最终封包。"""
    if not game_root:
        return None
    exe = os.path.join(game_root, 'bin', 'vpk.exe')
    return exe if os.path.isfile(exe) else None


def check_l4n_scripts_compat(analysis):
    """l4n 打包要求所有来源的脚本文件在 scripts/ 或 l4n/scripts/ 目录下。
    返回不兼容的 (来源标签, 脚本路径) 列表。"""
    bad = []
    for src in analysis.sources:
        for p in src.scripts:
            if not (p.startswith('scripts/') or p.startswith('l4n/scripts/')):
                bad.append((src.label, src.script_orig.get(p, p)))
    return bad


def _write_l4n_scripts(analysis, root, log):
    """合并脚本写入 root/l4n/scripts/sound/（去掉 scripts/ 前缀，原样照搬）。
    返回文件数。"""
    base = os.path.join(root, 'l4n', 'scripts', 'sound')
    n = 0
    for fpath, text in analysis.merged_scripts().items():
        if fpath.startswith('scripts/'):
            sub = fpath[len('scripts/'):]
        elif fpath.startswith('l4n/scripts/'):
            # l4n 格式 VPK 的脚本已在 l4n/scripts/ 下，取其相对路径
            sub = fpath[len('l4n/scripts/'):]
        else:  # 理论上已被 check_l4n_scripts_compat 拦下
            log(f'警告：脚本 {fpath} 不在 scripts/ 或 l4n/scripts/ 下，已跳过')
            continue
        dst = os.path.join(base, sub.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        n += 1
    return n


def _write_l4n_audio(analysis, root, log, progress):
    """合并音频写入 root/sound/（去掉 sound/ 前缀）。返回 (文件数, 覆盖列表)。"""
    sound_paths = [p for p in analysis.file_groups if p.startswith('sound/')]
    total = max(1, len(sound_paths))
    sound_root = os.path.join(root, 'sound')
    os.makedirs(sound_root, exist_ok=True)
    existed = set()
    for dp, _dn, fns in os.walk(sound_root):
        for fn in fns:
            existed.add(os.path.relpath(os.path.join(dp, fn),
                                        sound_root).replace('\\', '/').lower())
    overwrites = []
    for i, path in enumerate(sound_paths):
        cand = analysis.winner_for_file(path)
        rel_orig = analysis.file_orig.get(path, path)
        # 去掉 sound/ 前缀，以 root/sound 为基准
        low = rel_orig.replace('\\', '/').lower()
        rel_under = low[len('sound/'):] if low.startswith('sound/') else low
        dst = os.path.join(sound_root, rel_under.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if cand.origin == 'loose':
            shutil.copyfile(cand.detail, dst)
        else:
            _low, entry = cand.detail
            with open(dst, 'wb') as f:
                f.write(cand.source.vpk.read(entry))
        if rel_under in existed:
            overwrites.append(rel_under)
        if i % 20 == 0:
            progress(i, total, f'音频 {i}/{total}')
    progress(total, total, f'音频 {total}/{total}')
    return len(sound_paths), overwrites


def _write_vpk_other_passthrough(analysis, root):
    """VPK 内非脚本、非音频的其他文件按原路径透传到 root。"""
    n = 0
    for path in analysis.file_groups:
        if path.startswith('sound/'):
            continue
        cand = analysis.winner_for_file(path)
        if cand.origin != 'vpk' or cand.source.vpk is None:
            continue
        _low, entry = cand.detail
        rel_orig = analysis.file_orig.get(path, path)
        dst = os.path.join(root, rel_orig.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as f:
            f.write(cand.source.vpk.read(entry))
        n += 1
    return n


def _build_l4n_cache(root, l4n_exe, n_audio, log):
    """用 l4n 的 source_nekomimi.exe 对 root/sound 生成 sound.cache（打进包内）。"""
    sound_root = os.path.join(root, 'sound')

    def cache_log(m):
        if m.startswith('警告') or '失败' in m or '已生成' in m:
            log(m)

    build_sound_cache(sound_root, l4n_exe, cache_log)
    cache_path = os.path.join(sound_root, 'sound.cache')
    if os.path.isfile(cache_path):
        log(f'sound.cache 已生成（{n_audio} 个音频），将随包打包')
    else:
        log('警告：sound.cache 未生成（l4n 可能不可用），包内将没有缓存')


def _pack_l4n_vpk(root_dir, out_vpk, vpk_exe, log):
    """封包 root 为单文件 addon VPK。优先官方 vpk.exe，失败/缺失用内置封包器。"""
    if vpk_exe and os.path.isfile(vpk_exe):
        tmp_out = root_dir.rstrip('\\/') + '.vpk'
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        try:
            r = subprocess.run(
                [vpk_exe, root_dir], cwd=os.path.dirname(vpk_exe),
                capture_output=True, timeout=1800,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if r.returncode == 0 and os.path.isfile(tmp_out):
                shutil.move(tmp_out, out_vpk)
                log('使用官方 vpk.exe 封包完成')
                return
            log(f'vpk.exe 封包失败（rc={r.returncode}），改用内置封包器')
        except Exception as ex:
            log(f'vpk.exe 调用异常（{ex}），改用内置封包器')
    n, sz = write_addon_vpk(out_vpk, root_dir)
    log(f'内置封包器完成：{n} 个文件，数据区 {sz:,} 字节')


def _l4n_usage_txt(out_path, vpk_name, skin_note=''):
    usage = (
        '【l4n 合并音频包 - 使用方法】\n\n'
        f'1. 本包需要客户端已安装 l4n 平台才能加载音频。\n'
        f'2. 把「{vpk_name}」放入游戏 addons 文件夹：\n'
        '   …\\Left 4 Dead 2\\left4dead2\\addons\\\n\n'
        '3. 无需修改 gameinfo.txt，无需控制台命令（sound.cache 已打包在内）。\n'
        '4. 以后更新音频库后，重新用本工具打包，覆盖旧的 vpk 即可。\n'
        + skin_note
    )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(usage)


def export_l4n_standalone(analysis, out_dir, lib_name, l4n_exe, vpk_exe=None,
                          log=lambda m: None,
                          progress=lambda cur, total, msg: None):
    """方案 A：把合并音频库独立打包成 l4n addon VPK，返回 vpk 路径。"""
    if not re.match(r'^[A-Za-z0-9_\-+]+$', lib_name):
        raise ValueError('库名只能包含英文字母、数字、下划线、短横线和加号（不能有空格）')
    bad = check_l4n_scripts_compat(analysis)
    if bad:
        names = '；'.join(f'{lbl}: {p}' for lbl, p in bad[:5])
        more = f'（等共 {len(bad)} 处）' if len(bad) > 5 else ''
        raise ValueError('以下来源的脚本不在 scripts/ 目录下，无法用 l4n 打包：\n'
                         f'{names}{more}')

    out_vpk = os.path.join(out_dir, f'{lib_name}l4n.vpk')
    if os.path.exists(out_vpk):
        os.remove(out_vpk)
    temp = tempfile.mkdtemp(prefix='l4n_pack_')
    try:
        root = os.path.join(temp, 'output')
        os.makedirs(root)
        progress(1, 5, '组装 root')
        with open(os.path.join(root, 'addoninfo.txt'), 'w', encoding='utf-8') as f:
            f.write('"addoninfo"\n{\n'
                    f'\taddontitle\t"{lib_name}"\n'
                    '\taddonauthor\t"l4d2-audiomerge"\n'
                    f'\taddondescription\t"合并音频库 l4n 整合包"\n'
                    '}\n')
        n_sc = _write_l4n_scripts(analysis, root, log)
        log(f'脚本写入 l4n/scripts/sound/：{n_sc} 个')
        n_ot = _write_vpk_other_passthrough(analysis, root)
        if n_ot:
            log(f'其他文件透传：{n_ot} 个')
        progress(2, 5, '拷贝音频')
        n_au, ow = _write_l4n_audio(analysis, root, log, progress)
        log(f'音频写入 sound/：{n_au} 个' +
            (f'（覆盖同路径 {len(ow)} 个）' if ow else ''))
        progress(3, 5, '生成 sound.cache')
        _build_l4n_cache(root, l4n_exe, n_au, log)
        progress(4, 5, '封包 VPK')
        _pack_l4n_vpk(root, out_vpk, vpk_exe, log)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    _l4n_usage_txt(os.path.join(out_dir, f'使用方法-{lib_name}l4n.txt'),
                   f'{lib_name}l4n.vpk')
    progress(5, 5, '导出完成')
    log(f'导出完成：{out_vpk}')
    return out_vpk


def export_l4n_merge(skin_vpk_path, analysis, out_dir, l4n_exe, vpk_exe=None,
                     log=lambda m: None,
                     progress=lambda cur, total, msg: None):
    """方案 B：把音频库合并进皮肤 mod，封包为 <皮肤名>l4n.vpk，返回 vpk 路径。"""
    if not os.path.isfile(skin_vpk_path):
        raise ValueError(f'皮肤 mod 文件不存在：{skin_vpk_path}')
    bad = check_l4n_scripts_compat(analysis)
    if bad:
        names = '；'.join(f'{lbl}: {p}' for lbl, p in bad[:5])
        more = f'（等共 {len(bad)} 处）' if len(bad) > 5 else ''
        raise ValueError('以下来源的脚本不在 scripts/ 目录下，无法用 l4n 打包：\n'
                         f'{names}{more}')

    stem = os.path.splitext(os.path.basename(skin_vpk_path))[0]
    out_vpk = os.path.join(out_dir, f'{stem}l4n.vpk')
    if os.path.exists(out_vpk):
        os.remove(out_vpk)
    temp = tempfile.mkdtemp(prefix='l4n_pack_')
    try:
        root = os.path.join(temp, 'output')
        os.makedirs(root)
        progress(1, 6, '拆包皮肤 mod')
        log(f'拆包皮肤 mod：{os.path.basename(skin_vpk_path)}')
        reader = VpkReader(skin_vpk_path)
        n_skin = 0
        for low, entry in reader.entries.items():
            dst = os.path.join(root, entry['path'].replace('\\', '/').lstrip('/')
                               .replace('/', os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'wb') as f:
                f.write(reader.read(entry))
            n_skin += 1
        log(f'皮肤内容解出：{n_skin} 个文件')
        if os.path.isdir(os.path.join(root, 'l4n')):
            log('警告：该皮肤 mod 自带 l4n 目录（可能是二次打包产物），'
                '将强制合并，同路径文件以音频库为准')
        progress(2, 6, '写入合并脚本')
        n_sc = _write_l4n_scripts(analysis, root, log)
        log(f'脚本写入 l4n/scripts/sound/：{n_sc} 个')
        n_ot = _write_vpk_other_passthrough(analysis, root)
        if n_ot:
            log(f'其他文件透传：{n_ot} 个')
        progress(3, 6, '合并音频')
        n_au, ow = _write_l4n_audio(analysis, root, log, progress)
        log(f'音频写入 sound/：{n_au} 个' +
            (f'（覆盖皮肤同路径 {len(ow)} 个：' +
             '、'.join(ow[:3]) + ('…' if len(ow) > 3 else '') + '）' if ow else ''))
        progress(4, 6, '生成 sound.cache')
        _build_l4n_cache(root, l4n_exe, n_au, log)
        progress(5, 6, '封包 VPK')
        _pack_l4n_vpk(root, out_vpk, vpk_exe, log)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    _l4n_usage_txt(os.path.join(out_dir, f'使用方法-{stem}l4n.txt'),
                   f'{stem}l4n.vpk',
                   '5. 皮肤 mod 原有内容（材质/模型/脚本等）已完整保留。\n')
    progress(6, 6, '导出完成')
    log(f'导出完成：{out_vpk}')
    return out_vpk
