# -*- coding: utf-8 -*-
"""
L4D2 音频库合并工具 - Tkinter 桌面界面
"""
import os
import queue
import sys
import tempfile
import threading

# 允许直接 python merger\gui.py 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:
    print('需要 Python 自带的 tkinter 模块（标准 Python 安装默认包含）')
    raise

from merger.core import (
    Analysis, Source, export_merge,
    export_l4n_standalone, export_l4n_merge,
    find_l4d2_dirs, find_deployed_libs, make_sources_from_package,
    make_sources_from_game, make_sources_from_vpk, _read_candidate_bytes,
    is_valid_game_root, save_game_root, find_l4n, find_vpk_exe,
    build_sound_cache,
)
from merger.keyvalues import serialize
import re as _re

try:
    import winsound
except Exception:
    winsound = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('L4D2 音频库合并工具')
        self.geometry('1180x820')
        self.minsize(960, 680)

        self.sources = []          # Source，顺序即优先级（前高后低）
        self.analysis = None
        self.game_root = None
        self.l4n_exe = None
        self.vpk_exe = None
        self._dirty = False
        self._tempdir = tempfile.mkdtemp(prefix='l4d2audiomerge_')
        self._temp_cache = {}
        self.q = queue.Queue()

        self._build_ui()
        self._autodetect_game()
        self.after(100, self._poll)
        if sys.version_info < (3, 8):
            self.log(f'警告：当前 Python {sys.version.split()[0]} 版本过旧，'
                     '本工具要求 Python 3.8+，部分功能可能异常。')
            self.after(
                200, lambda: messagebox.showwarning(
                    'Python 版本过旧',
                    f'当前 Python 为 {sys.version.split()[0]}，本工具要求 3.8+。\n'
                    '请到 python.org 安装新版 Python（默认安装即可）。'))

    # ---------------------------------------------------------- 界面
    def _build_ui(self):
        # ===== 环境要求常显标注 =====
        ver = sys.version.split()[0]
        self.lbl_env = ttk.Label(
            self, foreground='#666',
            text=f'运行环境：Python {ver}｜要求 Python 3.8+（需含 tkinter，'
                 'python.org 官网安装包默认包含）｜无需安装任何第三方库，下载即用')
        self.lbl_env.pack(fill='x', padx=12, pady=(6, 0))

        # ===== 来源区 =====
        frm_src = ttk.LabelFrame(self, text='来源库（列表越靠上优先级越高，同名内容以高优先级为准）')
        frm_src.pack(fill='x', padx=8, pady=(8, 4))

        btns = ttk.Frame(frm_src)
        btns.pack(fill='x', padx=6, pady=4)
        ttk.Button(btns, text='添加音频库文件夹…', command=self.add_package).pack(side='left')
        ttk.Button(btns, text='添加 VPK 文件…', command=self.add_vpk_file).pack(side='left', padx=4)
        ttk.Button(btns, text='扫描游戏目录已部署的库', command=self.scan_game_libs).pack(side='left', padx=4)
        ttk.Button(btns, text='设置游戏位置…', command=self.choose_game_root).pack(side='left', padx=4)
        ttk.Button(btns, text='移除选中', command=self.remove_selected).pack(side='left', padx=4)
        ttk.Button(btns, text='上移', width=6, command=lambda: self.move_selected(-1)).pack(side='left')
        ttk.Button(btns, text='下移', width=6, command=lambda: self.move_selected(1)).pack(side='left', padx=4)
        ttk.Button(btns, text='开始分析', command=self.start_analysis).pack(side='right')
        self.lbl_game = ttk.Label(btns, text='游戏目录：检测中…', foreground='#666')
        self.lbl_game.pack(side='right', padx=10)

        cols = ('label', 'kind', 'entries', 'audio', 'path')
        self.tree_src = ttk.Treeview(frm_src, columns=cols, show='headings', height=7)
        for c, t, w in [('label', '名称', 220), ('kind', '类型', 110),
                        ('entries', '音效条目', 80), ('audio', '音频文件', 80),
                        ('path', '库目录路径', 560)]:
            self.tree_src.heading(c, text=t)
            self.tree_src.column(c, width=w, anchor='w')
        vs = ttk.Scrollbar(frm_src, orient='vertical', command=self.tree_src.yview)
        self.tree_src.configure(yscrollcommand=vs.set)
        self.tree_src.pack(side='left', fill='x', expand=True, padx=(6, 0), pady=(0, 6))
        vs.pack(side='right', fill='y', pady=(0, 6), padx=(0, 6))

        # ===== 中部标签页 =====
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=4)

        # --- 校验报告 ---
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text='校验报告')
        self.lbl_stats = ttk.Label(tab1, text='尚未分析。添加来源后点「开始分析」。', justify='left')
        self.lbl_stats.pack(anchor='w', padx=8, pady=6)
        cols1 = ('level', 'path', 'desc')
        self.tree_report = ttk.Treeview(tab1, columns=cols1, show='headings')
        self.tree_report.heading('level', text='类型')
        self.tree_report.heading('path', text='路径 / 内容')
        self.tree_report.heading('desc', text='说明')
        self.tree_report.column('level', width=110, anchor='w')
        self.tree_report.column('path', width=520, anchor='w')
        self.tree_report.column('desc', width=380, anchor='w')
        r1 = ttk.Scrollbar(tab1, orient='vertical', command=self.tree_report.yview)
        self.tree_report.configure(yscrollcommand=r1.set)
        self.tree_report.pack(side='left', fill='both', expand=True, padx=(8, 0), pady=(0, 8))
        r1.pack(side='right', fill='y', pady=(0, 8), padx=(0, 8))

        # --- 脚本条目冲突 ---
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text='脚本条目冲突')
        paned = ttk.Panedwindow(tab2, orient='vertical')
        paned.pack(fill='both', expand=True, padx=8, pady=8)
        top2 = ttk.Frame(paned)
        cols2 = ('title', 'cands', 'winner')
        self.tree_sc = ttk.Treeview(top2, columns=cols2, show='headings', height=8)
        self.tree_sc.heading('title', text='冲突条目（脚本文件 → 音效条目名）')
        self.tree_sc.heading('cands', text='候选数')
        self.tree_sc.heading('winner', text='当前采用')
        self.tree_sc.column('title', width=620, anchor='w')
        self.tree_sc.column('cands', width=70, anchor='center')
        self.tree_sc.column('winner', width=260, anchor='w')
        s2 = ttk.Scrollbar(top2, orient='vertical', command=self.tree_sc.yview)
        self.tree_sc.configure(yscrollcommand=s2.set)
        self.tree_sc.pack(side='left', fill='both', expand=True)
        s2.pack(side='right', fill='y')
        paned.add(top2, weight=2)

        bot2 = ttk.LabelFrame(paned, text='候选内容（单选决定采用谁）')
        self.frm_sc_cands = ttk.Frame(bot2)
        self.frm_sc_cands.pack(fill='x', padx=6, pady=4)
        txt_wrap = ttk.Frame(bot2)
        txt_wrap.pack(fill='both', expand=True, padx=6, pady=(0, 6))
        self.txt_sc = tk.Text(txt_wrap, height=10, wrap='none', state='disabled',
                              font=('Consolas', 9))
        s2b = ttk.Scrollbar(txt_wrap, orient='vertical', command=self.txt_sc.yview)
        self.txt_sc.configure(yscrollcommand=s2b.set)
        self.txt_sc.pack(side='left', fill='both', expand=True)
        s2b.pack(side='right', fill='y')
        paned.add(bot2, weight=3)
        self.tree_sc.bind('<<TreeviewSelect>>', self._on_sc_select)

        # --- 音频文件冲突 ---
        tab3 = ttk.Frame(nb)
        nb.add(tab3, text='音频/文件冲突')
        paned3 = ttk.Panedwindow(tab3, orient='vertical')
        paned3.pack(fill='both', expand=True, padx=8, pady=8)
        top3 = ttk.Frame(paned3)
        cols3 = ('title', 'cands', 'winner')
        self.tree_af = ttk.Treeview(top3, columns=cols3, show='headings', height=8)
        self.tree_af.heading('title', text='冲突文件路径')
        self.tree_af.heading('cands', text='候选数')
        self.tree_af.heading('winner', text='当前采用')
        self.tree_af.column('title', width=620, anchor='w')
        self.tree_af.column('cands', width=70, anchor='center')
        self.tree_af.column('winner', width=260, anchor='w')
        s3 = ttk.Scrollbar(top3, orient='vertical', command=self.tree_af.yview)
        self.tree_af.configure(yscrollcommand=s3.set)
        self.tree_af.pack(side='left', fill='both', expand=True)
        s3.pack(side='right', fill='y')
        paned3.add(top3, weight=2)

        bot3 = ttk.LabelFrame(paned3, text='候选文件（可试听）')
        line3 = ttk.Frame(bot3)
        line3.pack(fill='x', padx=6, pady=4)
        ttk.Button(line3, text='▶ 试听选中候选', command=self.play_selected).pack(side='left')
        ttk.Button(line3, text='■ 停止', command=self.stop_play).pack(side='left', padx=4)
        self.lbl_af_info = ttk.Label(line3, text='', foreground='#444')
        self.lbl_af_info.pack(side='left', padx=10)
        self.frm_af_cands = ttk.Frame(bot3)
        self.frm_af_cands.pack(fill='x', padx=6, pady=4)
        paned3.add(bot3, weight=1)
        self.tree_af.bind('<<TreeviewSelect>>', self._on_af_select)

        # ===== 导出区 =====
        frm_out = ttk.LabelFrame(self, text='导出合并库')
        frm_out.pack(fill='x', padx=8, pady=4)
        ttk.Label(frm_out, text='库文件夹名：').grid(row=0, column=0, padx=(8, 2), pady=6, sticky='w')
        self.var_libname = tk.StringVar(value='myaudiolib')
        ttk.Entry(frm_out, textvariable=self.var_libname, width=20).grid(row=0, column=1, padx=2)
        ttk.Label(frm_out, text='导出到：').grid(row=0, column=2, padx=(16, 2))
        self.var_outdir = tk.StringVar(value=os.path.join(os.path.expanduser('~'), 'Desktop'))
        ttk.Entry(frm_out, textvariable=self.var_outdir, width=60).grid(row=0, column=3, padx=2)
        ttk.Button(frm_out, text='浏览…', command=self.pick_outdir).grid(row=0, column=4, padx=4)
        ttk.Button(frm_out, text='开始合并导出', command=self.start_export).grid(row=0, column=5, padx=8)
        self.progress = ttk.Progressbar(frm_out, mode='determinate', length=200)
        self.progress.grid(row=1, column=0, columnspan=6, sticky='we', padx=8, pady=(0, 6))
        self.lbl_l4n = ttk.Label(frm_out, text='l4n：检测中…', foreground='#666')
        self.lbl_l4n.grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 6), sticky='w')
        self.var_cache = tk.BooleanVar(value=True)
        self.chk_cache = ttk.Checkbutton(
            frm_out, text='导出后用 l4n 生成 sound.cache（部署后无需再敲控制台命令）',
            variable=self.var_cache)
        self._cache_shown = False   # 仅检测到 l4n 时显示开关
        # l4n 导出形态（仅检测到 l4n 时显示）
        self.var_exp_mode = tk.StringVar(value='folder')
        self.var_skin = tk.StringVar(value='')
        self._mode_shown = False
        self.frm_mode = ttk.Frame(frm_out)
        ttk.Label(self.frm_mode, text='导出形态：').pack(side='left')
        for val, text in (('folder', '文件夹（传统）'),
                          ('l4n_a', 'l4n 独立包（VPK）'),
                          ('l4n_b', 'l4n 皮肤合体包（VPK）')):
            ttk.Radiobutton(self.frm_mode, text=text, value=val,
                            variable=self.var_exp_mode,
                            command=self._on_mode_change).pack(side='left', padx=(8, 0))
        ttk.Button(self.frm_mode, text='选择皮肤mod…',
                   command=self.pick_skin).pack(side='left', padx=(12, 4))
        self.lbl_skin = ttk.Label(self.frm_mode, text='（未选择皮肤mod）',
                                  foreground='#666')
        self.lbl_skin.pack(side='left')

        # ===== 日志 =====
        frm_log = ttk.LabelFrame(self, text='日志')
        frm_log.pack(fill='x', padx=8, pady=(4, 8))
        self.txt_log = tk.Text(frm_log, height=6, state='disabled', font=('Consolas', 9))
        sl = ttk.Scrollbar(frm_log, orient='vertical', command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sl.set)
        self.txt_log.pack(side='left', fill='x', expand=True, padx=(6, 0), pady=6)
        sl.pack(side='right', fill='y', pady=6, padx=(0, 6))

        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ---------------------------------------------------------- 工具
    def log(self, msg):
        self.txt_log.configure(state='normal')
        self.txt_log.insert('end', msg + '\n')
        self.txt_log.see('end')
        self.txt_log.configure(state='disabled')

    def _poll(self):
        try:
            while True:
                fn = self.q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def run_bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _autodetect_game(self):
        def work():
            roots = find_l4d2_dirs()
            def done():
                if roots:
                    self.game_root = roots[0]
                    tag = '（上次手动指定）' if len(roots) > 1 and self.game_root != roots[-1] else ''
                    self.lbl_game.configure(text=f'游戏目录：{self.game_root}')
                    self.log(f'检测到游戏目录：{self.game_root}{tag}')
                else:
                    self.lbl_game.configure(text='游戏目录：未检测到，请点「设置游戏位置…」')
                    self.log('未自动检测到 L4D2 游戏目录。'
                             '可点「设置游戏位置…」手动指定，或只使用「添加音频库文件夹」方式。')
                self._update_l4n()
            self.q.put(done)
        self.run_bg(work)

    def _update_l4n(self):
        """根据游戏目录检测 l4n 平台，更新状态显示与相关开关可见性。"""
        self.l4n_exe = find_l4n(self.game_root)
        self.vpk_exe = find_vpk_exe(self.game_root)
        if self.l4n_exe:
            self.lbl_l4n.configure(text='l4n：已检测到', foreground='#2a7f5f')
            if not self._cache_shown:
                self.chk_cache.grid(row=2, column=2, padx=(16, 2), pady=(0, 6), sticky='w')
                self._cache_shown = True
            if not self._mode_shown:
                self.frm_mode.grid(row=3, column=0, columnspan=6,
                                   padx=8, pady=(0, 6), sticky='w')
                self._mode_shown = True
            self.log('检测到 l4n 平台（source_nekomimi），导出时可自动生成 sound.cache。')
            self.log('检测到官方 vpk.exe，封包将使用它。' if self.vpk_exe
                     else '未检测到官方 vpk.exe，封包将使用内置封包器（效果相同）。')
        else:
            self.lbl_l4n.configure(
                text='l4n：未检测到（安装 l4n 平台后可在此预生成 sound.cache）',
                foreground='#666')
            if self._cache_shown:
                self.chk_cache.grid_forget()
                self._cache_shown = False
            if self._mode_shown:
                self.frm_mode.grid_forget()
                self._mode_shown = False
                self.var_exp_mode.set('folder')

    def _on_mode_change(self):
        if self.var_exp_mode.get() == 'l4n_b' and not self.var_skin.get():
            self.pick_skin()

    def pick_skin(self):
        p = filedialog.askopenfilename(
            title='选择皮肤 mod VPK（音频库将合并进去）',
            filetypes=[('VPK 文件', '*.vpk'), ('所有文件', '*.*')])
        if p:
            self.var_skin.set(p)
            self.lbl_skin.configure(text=os.path.basename(p), foreground='#000')
        elif not self.var_skin.get():
            self.lbl_skin.configure(text='（未选择皮肤mod）', foreground='#666')

    def choose_game_root(self, silent_cancel=False):
        """手动选择游戏根目录；校验通过后记住（settings.json）。"""
        d = filedialog.askdirectory(
            title='选择 L4D2 游戏根目录（含 left4dead2.exe 的那一级；选 left4dead2 子目录也可以）')
        if not d:
            return False
        root = is_valid_game_root(d)
        if not root:
            messagebox.showerror(
                '无效目录',
                '所选目录不是求生之路2游戏根目录。\n\n'
                '应选择包含 left4dead2.exe 和 left4dead2 文件夹的那一级，例如：\n'
                r'  D:\SteamLibrary\steamapps\common\Left 4 Dead 2')
            return False
        self.game_root = root
        save_game_root(root)
        self.lbl_game.configure(text=f'游戏目录：{root}')
        self.log(f'已设置游戏目录：{root}（已记住，下次启动自动使用）')
        self._update_l4n()
        return True

    # ---------------------------------------------------------- 来源管理
    def add_package(self):
        d = filedialog.askdirectory(title='选择音频库文件夹（含 pak01_dir.vpk；也可选包含它的模组包，会自动识别）')
        if not d:
            return
        self.log(f'扫描音频库：{d}')

        def work():
            try:
                srcs = make_sources_from_package(d, log=lambda m: self.q.put(lambda: self.log(m)))
                def done():
                    self._add_sources(srcs)
                    if not srcs:
                        messagebox.showinfo('提示', '该文件夹内没有找到 pak01_dir.vpk（音频库结构）。')
                self.q.put(done)
            except Exception as e:
                self.q.put(lambda: messagebox.showerror('错误', f'读取失败：{e}'))
        self.run_bg(work)

    def add_vpk_file(self):
        paths = filedialog.askopenfilenames(
            title='选择音频库 VPK 文件（一个或多个）',
            filetypes=[('VPK 文件', '*.vpk'), ('所有文件', '*.*')])
        if not paths:
            return
        def work():
            all_srcs = []
            for p in paths:
                self.log(f'读取 VPK：{p}')
                try:
                    srcs = make_sources_from_vpk(p, log=lambda m: self.q.put(lambda: self.log(m)))
                    all_srcs.extend(srcs)
                except Exception as e:
                    self.q.put(lambda m=p, e=e: self.log(f'  [错误] {m}: {e}'))
            self.q.put(lambda: self._add_sources(all_srcs))
        self.run_bg(work)

    def scan_game_libs(self):
        if not self.game_root:
            if not self.choose_game_root():
                return
        self.log(f'扫描游戏目录已部署的库：{self.game_root}')

        def work():
            try:
                srcs = make_sources_from_game(self.game_root,
                                              log=lambda m: self.q.put(lambda: self.log(m)))
                def done():
                    self._add_sources(srcs)
                    if not srcs:
                        messagebox.showinfo('提示', '游戏目录下没有找到已部署的音频库文件夹。')
                self.q.put(done)
            except Exception as e:
                self.q.put(lambda: messagebox.showerror('错误', f'扫描失败：{e}'))
        self.run_bg(work)

    def _add_sources(self, srcs):
        added = 0
        for s in srcs:
            if s.error:
                self.log(f'  [跳过] {s.label}：{s.error}')
                continue
            if any(x.id == s.id for x in self.sources):
                self.log(f'  [已存在] {s.label}')
                continue
            self.sources.append(s)
            kind = '模组包' if s.kind == 'pkg' else '游戏目录库'
            self.tree_src.insert('', 'end', iid=s.id,
                                 values=(s.label, kind, s.script_entries, s.audio_count, s.lib_dir))
            added += 1
            self.log(f'  [添加] {s.label}（条目 {s.script_entries}，音频 {s.audio_count}）')
        if added:
            self._dirty = True

    def remove_selected(self):
        sel = self.tree_src.selection()
        for iid in sel:
            self.sources = [s for s in self.sources if s.id != iid]
            self.tree_src.delete(iid)
        if sel:
            self._dirty = True

    def move_selected(self, delta):
        sel = self.tree_src.selection()
        if len(sel) != 1:
            return
        iid = sel[0]
        # iid 即 source id
        order = [self.tree_src.item(c, 'iid') for c in self.tree_src.get_children('')]
        i = order.index(iid)
        j = i + delta
        if j < 0 or j >= len(order):
            return
        order[i], order[j] = order[j], order[i]
        for idx, x in enumerate(order):
            self.tree_src.move(x, '', idx)
        self.sources.sort(key=lambda s: order.index(s.lib_dir))
        self._dirty = True

    # ---------------------------------------------------------- 分析
    def start_analysis(self):
        if not self.sources:
            messagebox.showwarning('提示', '请先添加来源库。')
            return
        self.log('开始分析…')

        def work():
            try:
                an = Analysis(self.sources)
                an.run(game_root=self.game_root,
                       log=lambda m: self.q.put(lambda: self.log(m)))
                an.check_wav_formats(log=lambda m: self.q.put(lambda: self.log(m)))
                self.q.put(lambda: self._analysis_done(an))
            except Exception as e:
                self.q.put(lambda: messagebox.showerror('分析失败', str(e)))
        self.run_bg(work)

    def _analysis_done(self, an):
        self.analysis = an
        self._dirty = False
        st = an.stats
        self.lbl_stats.configure(
            text=(f'来源库 {st["sources"]} 个 ｜ 脚本文件 {st["script_files"]} 个 ｜ '
                  f'音效条目 {st["entries"]} 条 ｜ 音频文件 {st["audio"]} 个\n'
                  f'脚本条目冲突 {st["conflict_script"]} 处 ｜ '
                  f'音频/文件冲突 {st["conflict_file"]} 处 ｜ '
                  f'自定义缺失引用 {st["missing"]} 个'))
        self._refresh_report(an)
        self._refresh_conflicts(an)
        self.log(f'分析完成：脚本冲突 {st["conflict_script"]}，文件冲突 {st["conflict_file"]}，'
                 f'缺失引用 {st["missing"]}')
        if st['conflict_script'] or st['conflict_file']:
            self.log('请在「脚本条目冲突」「音频/文件冲突」标签页检查裁决结果（默认高优先级获胜）。')

    def _refresh_report(self, an):
        for iid in self.tree_report.get_children(''):
            self.tree_report.delete(iid)
        for path, level in an.missing:
            if level == 'missing':
                self.tree_report.insert('', 'end', values=('缺失引用', path,
                    '脚本引用了该音频，但所有来源库里都找不到；你可能还缺一个音频库包'))
            else:
                self.tree_report.insert('', 'end', values=('原版引用', path,
                    '原版游戏音效，库中不存在属正常'))
        for path, desc in an.warnings:
            self.tree_report.insert('', 'end', values=('格式警告', path, desc))

    def _refresh_conflicts(self, an):
        for tv in (self.tree_sc, self.tree_af):
            for iid in tv.get_children(''):
                tv.delete(iid)
        for c in an.conflicts:
            tv = self.tree_sc if c.kind in ('script', 'rawscript') else self.tree_af
            tv.insert('', 'end', iid=c.key,
                      values=(c.title, len(c.candidates), c.winner().source.label))

    def _winner_label(self, conf):
        return conf.winner().source.label

    # ---------------------------------------------------------- 脚本冲突裁决
    def _on_sc_select(self, _evt=None):
        key = self.tree_sc.focus()
        conf = self._find_conf(key)
        if not conf:
            return
        for w in self.frm_sc_cands.winfo_children():
            w.destroy()
        var = tk.IntVar(value=0)
        for i, cand in enumerate(conf.candidates):
            rb = ttk.Radiobutton(
                self.frm_sc_cands, variable=var, value=i,
                text=f'来源：{cand.source.label}',
                command=lambda c=conf, v=var, idx=i: self._decide_script(c, v, idx))
            rb.grid(row=i, column=0, sticky='w', padx=4, pady=1)
            if conf.decision is cand.source:
                var.set(i)
        # 默认展示第一个候选内容
        self._show_sc_candidate(conf, 0)
        var.trace_add('write', lambda *_: self._show_sc_candidate(conf, var.get()))

    def _decide_script(self, conf, var, idx):
        conf.decision = conf.candidates[idx].source
        self.tree_sc.set(conf.key, 'winner', conf.winner().source.label)

    def _show_sc_candidate(self, conf, idx):
        cand = conf.candidates[idx]
        if conf.kind == 'rawscript':
            text = cand.detail[1].decode('utf-8', errors='replace')  # 原始 bytes
        else:
            text = cand.detail[2]  # canon 文本
        self.txt_sc.configure(state='normal')
        self.txt_sc.delete('1.0', 'end')
        self.txt_sc.insert('1.0', text)
        self.txt_sc.configure(state='disabled')

    # ---------------------------------------------------------- 音频冲突裁决/试听
    def _on_af_select(self, _evt=None):
        key = self.tree_af.focus()
        conf = self._find_conf(key)
        if not conf:
            return
        for w in self.frm_af_cands.winfo_children():
            w.destroy()
        var = tk.IntVar(value=0)
        for i, cand in enumerate(conf.candidates):
            origin = '松散文件' if cand.origin == 'loose' else 'VPK 内'
            rb = ttk.Radiobutton(
                self.frm_af_cands, variable=var, value=i,
                text=f'来源：{cand.source.label} ｜ {origin} ｜ {cand.size // 1024} KB',
                command=lambda c=conf, idx=i: self._decide_file(c, idx))
            rb.grid(row=i, column=0, sticky='w', padx=4, pady=1)
            if conf.decision is cand.source:
                var.set(i)
        self._current_af = (conf, var)
        self.lbl_af_info.configure(text=conf.title)

    def _decide_file(self, conf, idx):
        conf.decision = conf.candidates[idx].source
        self.tree_af.set(conf.key, 'winner', conf.winner().source.label)

    def play_selected(self):
        if winsound is None:
            messagebox.showwarning('提示', '当前系统不支持 winsound 播放。')
            return
        if not getattr(self, '_current_af', None):
            return
        conf, var = self._current_af
        cand = conf.candidates[var.get()]
        try:
            path = self._candidate_wav_path(cand)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self.log(f'试听：{conf.title} ← {cand.source.label}')
        except Exception as e:
            messagebox.showerror('播放失败', str(e))

    def stop_play(self):
        if winsound is not None:
            winsound.PlaySound(None, 0)

    def _candidate_wav_path(self, cand):
        key = (cand.source.lib_dir, cand.detail if cand.origin == 'loose' else cand.detail[0])
        if key in self._temp_cache:
            return self._temp_cache[key]
        if cand.origin == 'loose':
            p = cand.detail
        else:
            data = _read_candidate_bytes(cand)
            name = cand.detail[0].replace('/', '_')
            p = os.path.join(self._tempdir, name)
            with open(p, 'wb') as f:
                f.write(data)
        self._temp_cache[key] = p
        return p

    def _find_conf(self, key):
        if not self.analysis:
            return None
        for c in self.analysis.conflicts:
            if c.key == key:
                return c
        return None

    # ---------------------------------------------------------- 导出
    def pick_outdir(self):
        d = filedialog.askdirectory(title='选择导出位置')
        if d:
            self.var_outdir.set(d)

    def start_export(self):
        if not self.analysis or self._dirty:
            if not messagebox.askyesno('确认', '来源有变动，是否先按当前来源重新分析再导出？'):
                return
            self.start_analysis()
            return
        lib = self.var_libname.get().strip()
        out = self.var_outdir.get().strip()
        mode = self.var_exp_mode.get() if self.l4n_exe else 'folder'
        if mode == 'l4n_b' and not self.var_skin.get():
            messagebox.showwarning('提示', '皮肤合体包需要先点「选择皮肤mod…」选择一个 VPK。')
            return
        if mode != 'folder' and not self.l4n_exe:
            messagebox.showwarning('提示', 'l4n 打包模式需要检测到 l4n 平台（bin/neko）。')
            return
        if not self.game_root:
            if messagebox.askyesno('提示',
                                   '未设置游戏目录，gameinfo.txt 将使用内置模板生成。\n'
                                   '要现在手动指定游戏目录吗？（推荐，可基于真实 gameinfo 修改）'):
                if not self.choose_game_root():
                    return
        if not lib or not out or not os.path.isdir(out):
            messagebox.showwarning('提示', '请填写有效的库名和导出目录。')
            return
        if not _re.match(r'^[A-Za-z0-9_\-+]+$', lib):
            messagebox.showwarning(
                '库名无效',
                '库名只能包含英文字母、数字、下划线、短横线和加号。\n\n'
                '中文库名在 VPK 封包或游戏加载时可能出错（引擎对非 ASCII 路径兼容性差），\n'
                '请改用纯英文名，如 my_audio_lib。')
            return
        if mode != 'folder':
            self.log(f'开始导出 l4n 包（{"皮肤合体" if mode == "l4n_b" else "独立"}）'
                     f'：{lib} -> {out}')
        else:
            self.log(f'开始导出合并库：{lib} -> {out}')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)

        def work():
            try:
                if mode == 'l4n_a':
                    result = export_l4n_standalone(
                        self.analysis, out, lib, self.l4n_exe,
                        vpk_exe=getattr(self, 'vpk_exe', None),
                        log=lambda m: self.q.put(lambda: self.log(m)),
                        progress=lambda cur, total, msg: self.q.put(
                            lambda: self._progress(cur, total, msg)))
                elif mode == 'l4n_b':
                    result = export_l4n_merge(
                        self.var_skin.get(), self.analysis, out, self.l4n_exe,
                        vpk_exe=getattr(self, 'vpk_exe', None),
                        log=lambda m: self.q.put(lambda: self.log(m)),
                        progress=lambda cur, total, msg: self.q.put(
                            lambda: self._progress(cur, total, msg)))
                else:
                    result = export_merge(
                        self.analysis, out, lib, game_root=self.game_root,
                        log=lambda m: self.q.put(lambda: self.log(m)),
                        progress=lambda cur, total, msg: self.q.put(
                            lambda: self._progress(cur, total, msg)))
                if mode == 'folder' and self.l4n_exe and self.var_cache.get():
                    self.q.put(lambda: self.log('正在用 l4n 生成 sound.cache …'))
                    ok, _cache = build_sound_cache(
                        os.path.join(result, lib, 'sound'), self.l4n_exe,
                        log=lambda m: self.q.put(lambda: self.log('  ' + m)))
                    if ok:
                        note_path = os.path.join(result, '使用方法.txt')
                        try:
                            with open(note_path, 'a', encoding='utf-8') as f:
                                f.write('\n[缓存] 本次导出已使用 l4n 预生成 sound.cache，'
                                        '部署后一般无需再执行上述控制台命令；\n'
                                        '       若之后手动增删过音频文件，重跑一次命令即可重建缓存。\n')
                        except OSError:
                            pass
                self.q.put(lambda: self._export_done(result))
            except Exception as e:
                self.q.put(lambda: self._export_fail(e))
        self.run_bg(work)

    def _progress(self, cur, total, msg):
        self.progress.configure(mode='determinate', maximum=total, value=cur)
        self.log(f'  {msg}')

    def _export_done(self, path):
        self.progress.stop()
        self.progress.configure(mode='determinate', value=0)
        self.log(f'导出完成：{path}')
        if str(path).lower().endswith('.vpk'):
            messagebox.showinfo(
                '完成', f'l4n 包已导出：\n{path}\n\n'
                        '放入游戏 addons 文件夹即可（需已安装 l4n）；\n'
                        '详细说明见同目录的「使用方法-*.txt」。')
        else:
            messagebox.showinfo('完成', f'合并库已导出到：\n{path}\n\n请查看其中的「使用方法.txt」完成部署。')

    def _export_fail(self, e):
        self.progress.stop()
        self.progress.configure(mode='determinate', value=0)
        messagebox.showerror('导出失败', str(e))

    def _on_close(self):
        self.stop_play()
        try:
            import shutil
            shutil.rmtree(self._tempdir, ignore_errors=True)
        except Exception:
            pass
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
