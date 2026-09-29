# -*- coding: utf-8 -*-
"""
视频拼接工具（本地 GUI 版）
- 一次导入多段 MP4，每段可设多条「开始-结束」裁剪区间，不设则整段拼接
- 支持图片素材（可多张、可与视频混排）：每张图片按「图片间隔」（默认 1 秒）连续显示，
  不同宽高比的图片以黑边补齐到基准分辨率
- 兼容海康威视私有格式（IMKH 头 / MPEG-PS 流，扩展名常为 .mp4）
- 海康 PS 流先无损转封装为 MP4：裁剪时可精确定位，不必从头解码整段录像
- 所有素材参数一致且不裁剪时走流复制直通：不重新编码，秒级完成
- 输出统一 H.264 + AAC 的 MP4，按列表顺序拼接
依赖：同目录或内嵌的 ffmpeg.exe
"""
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import queue

# ---------------- ffmpeg 定位 ----------------
def find_ffmpeg():
    here = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, 'ffmpeg.exe'),
        os.path.join(getattr(sys, '_MEIPASS', here), 'ffmpeg.exe'),
        shutil.which('ffmpeg') or '',
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise RuntimeError('找不到 ffmpeg.exe，请把它放到本程序同目录下')

FFMPEG = None  # 延迟初始化

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# ---------------- 工具函数 ----------------
def parse_time(s):
    """支持 秒 / 分:秒 / 时:分:秒，返回秒(float)；非法返回 None"""
    s = (s or '').strip()
    if not s:
        return None
    parts = s.split(':')
    if len(parts) > 3:
        return None
    sec = 0.0
    for p in parts:
        if not re.match(r'^\d+(\.\d+)?$', p.strip()):
            return None
        sec = sec * 60 + float(p)
    return sec

def fmt_time(sec):
    if sec is None:
        return '--:--'
    sec = max(0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return ('%02d:' % h if h else '') + '%02d:%04.1f' % (m, s)

def parse_filename_duration(name):
    """海康导出文件名常带起止时间戳：xxx_20260907100800-20260907101000_1.mp4"""
    m = re.search(r'(\d{14})\D+(\d{14})', name)
    if not m:
        return None
    def p(s):
        return datetime.datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                                 int(s[8:10]), int(s[10:12]), int(s[12:14]))
    d = (p(m.group(2)) - p(m.group(1))).total_seconds()
    return d if 0 < d < 86400 * 7 else None

def sniff_container(path):
    """识别真实容器：'ftyp' → mp4；'IMKH' 或 PS 包起始码 → ps"""
    with open(path, 'rb') as f:
        buf = f.read(4096)
    if len(buf) > 8 and buf[4:8] == b'ftyp':
        return 'mp4'
    if buf[:4] == b'IMKH':
        return 'ps'
    if b'\x00\x00\x01\xba' in buf:
        return 'ps'
    return 'unknown'

def strip_ps_header(src, dst):
    """去掉海康私有头，从第一个 PS 包起始码开始写入 dst"""
    with open(src, 'rb') as f:
        head = f.read(1024 * 1024)
        off = head.find(b'\x00\x00\x01\xba')
        if off < 0:
            off = 0
        f.seek(0)
        with open(dst, 'wb') as out:
            if off > 0:
                f.seek(off)
            shutil.copyfileobj(f, out, 1024 * 1024)

# ---------------- ffmpeg 调用 ----------------
def run_ffmpeg(args, log, progress_cb=None):
    """运行 ffmpeg，逐行输出 stderr；progress_cb(ratio) 可选。返回退出码。"""
    cmd = [FFMPEG, '-hide_banner'] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            universal_newlines=True, errors='replace',
                            creationflags=CREATE_NO_WINDOW)
    for line in proc.stderr:
        line = line.rstrip()
        if not line:
            continue
        log(line)
        if progress_cb:
            m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
            if m:
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                progress_cb(t)
    proc.wait()
    return proc.returncode

def probe(path, is_ps, log, skip_bytes=0):
    """返回 dict(duration, width, height, fps, has_audio, ...)
    skip_bytes: 跳过文件头部 N 字节（用于越过海康 IMKH 私有头）"""
    args = []
    if is_ps:
        args += ['-analyzeduration', '20000000', '-probesize', '50000000']
        if skip_bytes > 0:
            args += ['-skip_initial_bytes', str(skip_bytes)]
        args += ['-f', 'mpeg']
    args += ['-i', path]
    lines = []
    run_ffmpeg(args, lines.append)
    text = '\n'.join(lines)
    info = {'duration': None, 'width': 0, 'height': 0, 'fps': 0, 'has_audio': False,
            'vcodec': None, 'pix_fmt': None, 'sar': None,
            'acodec': None, 'sample_rate': 0, 'channels': None}
    m = re.search(r'Duration: (\d+):(\d+):([\d.]+)', text)
    if m:
        info['duration'] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r'Video: (\w+)', text)
    if m:
        info['vcodec'] = m.group(1)
    m = re.search(r'Video: [^,]+, ([a-z0-9_]+)', text)
    if m:
        info['pix_fmt'] = m.group(1)
    m = re.search(r'Video:.*? (\d{2,5})x(\d{2,5})[ ,]', text)
    if m:
        info['width'], info['height'] = int(m.group(1)), int(m.group(2))
    m = re.search(r'\[SAR (\d+:\d+)', text)
    if m:
        info['sar'] = m.group(1)
    m = re.search(r'Video:.*? (\d+(?:\.\d+)?) fps', text)
    if m:
        info['fps'] = float(m.group(1))
    # "0 channels" 的音轨是不可解码的坏轨（海康 PS 常见），按无音轨处理
    for line in lines:
        if 'Audio:' in line and '0 channels' not in line:
            info['has_audio'] = True
            if not info['acodec']:
                m = re.search(r'Audio: (\w+)', line)
                if m:
                    info['acodec'] = m.group(1)
                m = re.search(r'(\d+) Hz', line)
                if m:
                    info['sample_rate'] = int(m.group(1))
                m = re.search(r'\d+ Hz, ([^,]+)', line)
                if m:
                    info['channels'] = m.group(1).strip()
    return info

def probe_image(path, log):
    """探测静态图片尺寸，返回 {'width','height'}；失败返回 {}"""
    lines = []
    run_ffmpeg(['-i', path], lines.append)
    m = re.search(r'Video:.*? (\d{2,5})x(\d{2,5})', '\n'.join(lines))
    return {'width': int(m.group(1)), 'height': int(m.group(2))} if m else {}

def probe_import(it):
    """导入时的快速参数识别（仅供显示和一致性判断，正式处理时会重新精确识别）。
    返回 None 表示尚未识别；识别失败返回空 dict（界面据此显示"识别失败"而非一直转圈）。"""
    try:
        if it.get('kind') == 'image':
            return probe_image(it['path'], lambda s: None)
        if it['container'] == 'ps':
            # IMKH 私有头含有干扰字节，直接探测会失败：找到第一个 PS 包起始码的位置，
            # 用 -skip_initial_bytes 越过头部探测完整文件——参数和时长都是真实值，
            # 与处理时裁剪校验所用的时长一致（移动侦测录像的实际内容可能远短于文件名时间跨度）
            with open(it['path'], 'rb') as f:
                off = f.read(1024 * 1024).find(b'\x00\x00\x01\xba')
            info = probe(it['path'], True, lambda s: None, skip_bytes=off if off > 0 else 0)
            if not info.get('width'):
                return {}
            if not info.get('duration'):
                info['duration'] = parse_filename_duration(it['name'])
            return info
        info = probe(it['path'], False, lambda s: None)
        return info if (info.get('width') or info.get('duration')) else {}
    except Exception:
        return {}

def fmt_params(info):
    """卡片上显示的参数摘要"""
    parts = ['时长 ' + (fmt_time(info.get('duration')) if info.get('duration') else '未知')]
    if info.get('width'):
        parts.append('%d×%d' % (info['width'], info['height']))
    if info.get('fps'):
        parts.append('%gfps' % info['fps'])
    if info.get('vcodec'):
        parts.append(info['vcodec'].upper())
    if info.get('has_audio'):
        a = '有音轨'
        if info.get('acodec'):
            a += '(%s' % info['acodec'].upper()
            if info.get('sample_rate'):
                a += ' %gkHz' % (info['sample_rate'] / 1000.0)
            a += ')'
        parts.append(a)
    else:
        parts.append('无音轨')
    return ' ｜ '.join(parts)

def remux_ps_to_mp4(src, dst, log):
    """把海康 PS 流无损转封装为 MP4（流复制，不重编码）。
    转封装后时间轴重建、带完整索引，-ss 可以精确定位。成功返回 True。"""
    # 注意不要用 +genpts：它会按包间隔重建 PTS，把正常时间轴撑变形（帧率/时长漂移）
    in_args = ['-analyzeduration', '20000000', '-probesize', '50000000',
               '-f', 'mpeg', '-i', src]
    # 音轨可能是 MP4 不支持的编码（如 G.726）：先尝试连音轨一起转，失败则只转视频
    for maps in (['-map', '0:v:0', '-map', '0:a:0?'], ['-map', '0:v:0']):
        args = in_args + maps + ['-c', 'copy', '-avoid_negative_ts', 'make_zero', '-y', dst]
        if run_ffmpeg(args, log) == 0 and os.path.exists(dst) and os.path.getsize(dst) > 10000:
            return True
    return False

def can_stream_copy(items):
    """全部素材都是参数一致的原始 MP4 且没有任何裁剪区间时，可直接流复制拼接。
    海康 PS 转封装产物除外：其码流常带私有填充包、DTS 重复，直接拷贝拼接
    会导致部分播放器无法播放，必须重编码。"""
    if any(it.get('kind') == 'image' for it in items):
        return False
    if any(it['ranges'] for it in items):
        return False
    if any(it['container'] != 'mp4' or it.get('remuxed') for it in items):
        return False
    sig = []
    for it in items:
        if not it.get('vcodec') or not it.get('width'):
            return False
        audio = None
        if it.get('has_audio'):
            if not it.get('acodec'):
                return False
            audio = (it['acodec'], it.get('sample_rate') or 0, it.get('channels') or '')
        sig.append((it['vcodec'], it['width'], it['height'], it.get('pix_fmt') or '',
                    it.get('sar') or '1:1', round((it.get('fps') or 0) * 2) / 2, audio))
    return len(set(sig)) == 1

def concat_copy(files, out_path, tmpdir, log, progress_cb=None):
    """concat 分离器 + 流复制拼接：不解码不重编码。成功返回 True。"""
    lst = os.path.join(tmpdir, 'concat_list.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for p in files:
            p = os.path.abspath(p).replace('\\', '/').replace("'", "'\\''")
            f.write("file '%s'\n" % p)
    args = ['-f', 'concat', '-safe', '0', '-i', lst,
            '-c', 'copy', '-movflags', '+faststart', '-y', out_path]
    rc = run_ffmpeg(args, log, progress_cb)
    return rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000

# ---------------- 核心处理流程 ----------------
def process(items, out_path, log, progress, baseline=0, image_interval=1.0):
    """items: [{path,name,container,ranges:[(s,e|None)],kind:'image'?}]；log(str)；progress(ratio, text)
    baseline: 对齐基准视频的下标，输出以其分辨率和帧率为准
    image_interval: 每张图片的持续秒数"""
    tmpdir = tempfile.mkdtemp(prefix='videomerge_')
    try:
        # 1. 探测全部素材
        progress(0.01, '正在分析素材…')
        for i, it in enumerate(items):
            if it.get('kind') == 'image':
                it['work'] = it['path']
                info = probe_image(it['path'], log)
                it.update({k: v for k, v in info.items() if v})
                it['duration'] = image_interval
                log('图片：%s %s，显示 %g 秒' % (
                    it['name'],
                    ('%dx%d' % (it['width'], it['height'])) if it.get('width') else '分辨率未知',
                    image_interval))
                continue
            if it['container'] == 'ps':
                stripped = os.path.join(tmpdir, 'in%d.ps' % i)
                log('剥离海康私有头：' + it['name'])
                strip_ps_header(it['path'], stripped)
                it['work'] = stripped
                # 无损转封装为 MP4：流复制很快，且产出带索引、时间轴重建的文件，
                # 之后 -ss 能精确定位，不必为取靠后的片段从头解码整段录像
                remuxed = os.path.join(tmpdir, 'in%d.mp4' % i)
                log('无损转封装为 MP4（流复制）…')
                if remux_ps_to_mp4(stripped, remuxed, log):
                    it['work'] = remuxed
                    it['container'] = 'mp4'
                    it['remuxed'] = True
                else:
                    log('转封装失败，回退为从头解码模式（较慢）')
            else:
                it['work'] = it['path']
            info = probe(it['work'], it['container'] == 'ps', log)
            if it['container'] == 'mp4' and it['work'] != it['path'] and not info.get('width'):
                # 转封装产物无法识别，回退为直接处理 PS
                log('转封装产物异常，回退为从头解码模式（较慢）')
                it['work'] = stripped
                it['container'] = 'ps'
                info = probe(it['work'], True, log)
            it.update({k: v for k, v in info.items() if v})
            if not it.get('duration'):
                it['duration'] = parse_filename_duration(it['name'])
            log('识别结果：%s → 时长 %s，%s，%s' % (
                it['name'],
                fmt_time(it.get('duration')) if it.get('duration') else '未知',
                ('%dx%d' % (it['width'], it['height'])) if it.get('width') else '分辨率未知',
                '有音轨' if it.get('has_audio') else '无音轨'))

        # 2. 汇总片段
        jobs = []  # {item, start, end|None}
        for it in items:
            if not it['ranges']:
                jobs.append({'item': it, 'start': 0.0, 'end': None})
            else:
                for (s, e) in it['ranges']:
                    jobs.append({'item': it, 'start': s, 'end': e})
        for j in jobs:
            dur = j['item'].get('duration')
            if dur:
                if j['end'] is None:
                    j['end'] = dur
                j['end'] = min(j['end'], dur)
                if j['end'] <= j['start'] or j['start'] >= dur:
                    raise RuntimeError('「%s」的片段（%s → %s）超出实际时长 %s' % (
                        j['item']['name'], fmt_time(j['start']), fmt_time(j['end']), fmt_time(dur)))

        # 3. 统一目标参数：以基准视频的分辨率（取偶数）和帧率为准
        base = items[baseline] if 0 <= baseline < len(items) else items[0]
        W = int((base.get('width') or 1920) // 2 * 2)
        H = int((base.get('height') or 1080) // 2 * 2)
        FPS = round(base.get('fps') or 0, 2) or 30
        log('对齐基准：「%s」%dx%d@%gfps' % (base['name'], W, H, FPS))

        # 4. 单条命令完成全部裁剪+拼接：时间戳连续、一次编码成型，兼容性最好
        total_span = sum((j['end'] - j['start']) for j in jobs if j['end'] is not None) or 0

        # 快速通道：参数一致且无需裁剪时流复制直通，不重编码，速度取决于磁盘读写
        if can_stream_copy(items):
            log('所有素材参数一致且无需裁剪：流复制直通（不重新编码）')

            def copy_prog(t):
                if total_span > 0:
                    r = min(0.99, t / total_span)
                    progress(r, '正在直通拼接…（%d%%）' % int(r * 100))

            if concat_copy([it['work'] for it in items], out_path, tmpdir, log, copy_prog):
                expect = sum(it.get('duration') or 0 for it in items)
                got = probe(out_path, False, log).get('duration')
                if not expect or (got and abs(got - expect) <= max(2.0, expect * 0.02)):
                    progress(1.0, '完成')
                    return
                log('直通产物时长异常（%s，预期约 %s），改用重新编码…' % (
                    fmt_time(got) if got else '未知', fmt_time(expect)))
            else:
                log('流复制直通失败，改用重新编码…')
            if os.path.exists(out_path):
                os.remove(out_path)

        def on_prog(t):
            if total_span > 0:
                r = min(0.99, t / total_span)
                progress(r, '正在裁剪拼接…（%d%%）' % int(r * 100))

        def build_args(keep_audio):
            args = []
            unique = []
            for j in jobs:
                if j['item'] not in unique:
                    unique.append(j['item'])
            in_seek = {}
            for u, it in enumerate(unique):
                if it.get('kind') == 'image':
                    # 静态图片：循环解码 t 秒作为一段输入
                    in_seek[u] = 0.0
                    args += ['-loop', '1', '-framerate', '25', '-t', '%.3f' % it['duration']]
                elif it['container'] == 'ps':
                    # PS 流时间戳存在回绕/跳变，-ss 不可靠：从头解码，滤镜内按帧号重建时间轴
                    args += ['-analyzeduration', '20000000', '-probesize', '50000000', '-f', 'mpeg']
                    in_seek[u] = 0.0
                else:
                    # MP4 时间戳可靠：输入端精确 seek 到该文件最早片段起点
                    s0 = min(j['start'] for j in jobs if j['item'] is it)
                    in_seek[u] = s0
                    if s0 > 0.001:
                        args += ['-ss', '%.3f' % s0]
                args += ['-i', it['work']]
            parts = []
            for k, j in enumerate(jobs):
                it = j['item']
                u = unique.index(it)
                s = j['start'] - in_seek[u]
                e = None if j['end'] is None else j['end'] - in_seek[u]
                tr = 'start=%.3f' % s + ('' if e is None else ':end=%.3f' % e)
                if it.get('kind') == 'image':
                    # 图片：等比缩放到目标分辨率内，不足部分黑边居中补齐，不变形
                    vchain = ('scale=%d:%d:force_original_aspect_ratio=decrease,'
                              'pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
                              'fps=%g,format=yuv420p,settb=1/90000' % (W, H, W, H, FPS))
                elif it['container'] == 'ps':
                    fps = it.get('fps') or 25
                    vchain = ('setpts=N/(%g*TB),trim=%s,setpts=PTS-STARTPTS,scale=%d:%d,'
                              'setsar=1,fps=%g,format=yuv420p,settb=1/90000' % (fps, tr, W, H, FPS))
                else:
                    vchain = ('trim=%s,setpts=PTS-STARTPTS,scale=%d:%d,'
                              'setsar=1,fps=%g,format=yuv420p,settb=1/90000' % (tr, W, H, FPS))
                parts.append('[%d:v]%s[v%d]' % (u, vchain, k))
                if keep_audio and it.get('has_audio'):
                    if it['container'] == 'ps':
                        achain = ('asetpts=N/SR/TB,atrim=%s,asetpts=PTS-STARTPTS,'
                                  'aformat=sample_rates=44100:channel_layouts=stereo' % tr)
                    else:
                        achain = ('atrim=%s,asetpts=PTS-STARTPTS,'
                                  'aformat=sample_rates=44100:channel_layouts=stereo' % tr)
                    parts.append('[%d:a]%s[a%d]' % (u, achain, k))
                else:
                    d = (j['end'] - j['start']) if j['end'] is not None else (it.get('duration') or 0) - j['start']
                    if d <= 0:
                        raise RuntimeError('「%s」无音轨且时长未知，请为该视频填写具体的结束时间' % it['name'])
                    parts.append('anullsrc=channel_layout=stereo:sample_rate=44100,'
                                 'atrim=end=%.3f,asetpts=PTS-STARTPTS[a%d]' % (d, k))
            parts.append(''.join('[v%d][a%d]' % (k, k) for k in range(len(jobs))) +
                         'concat=n=%d:v=1:a=1[v][a]' % len(jobs))
            args += ['-filter_complex', ';'.join(parts), '-map', '[v]', '-map', '[a]',
                     '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                     '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', '-y', out_path]
            return args

        # PS 音轨可能损坏导致失败：先尝试保留音轨，失败则全部降级为静音轨重试
        attempts = [True, False] if any(it.get('has_audio') for it in items) else [False]
        last_err = None
        for keep_audio in attempts:
            args = build_args(keep_audio)
            log('开始处理：%d 个片段%s' % (len(jobs), '' if keep_audio else '（全部使用静音轨）'))
            rc = run_ffmpeg(args, log, on_prog)
            if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                last_err = None
                break
            last_err = 'ffmpeg 退出码 %d' % rc
            if keep_audio:
                log('保留音轨失败，全部改用静音轨重试…')
        if last_err:
            raise RuntimeError('处理失败：' + last_err)
        progress(1.0, '完成')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------------- GUI ----------------
def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    app = tk.Tk()
    app.title('视频拼接工具 v4.4')
    app.geometry('920x700')
    app.minsize(720, 480)

    q = queue.Queue()

    top = tk.Frame(app)
    top.pack(fill='x', padx=10, pady=8)
    tk.Label(top, text='视频拼接工具', font=('Microsoft YaHei', 16, 'bold')).pack(side='left')
    tk.Label(top, text='支持 MP4 / 海康PS流 / 图片；视频可设裁剪区间，图片按「图片间隔」转视频；同参数整段拼接走无损直通',
             fg='#888').pack(side='left', padx=12)

    # 操作区独立成一行，窗口变窄时按钮和输入框不会被标题挤掉
    toolbar = tk.Frame(app)
    toolbar.pack(fill='x', padx=10)

    # 参数一致性提示横幅
    banner = tk.Frame(app)
    banner.pack(fill='x', padx=10)
    banner_label = tk.Label(banner, text='', anchor='w', justify='left',
                            wraplength=860, font=('Microsoft YaHei', 9))
    banner_label.pack(fill='x')
    banner.bind('<Configure>', lambda e: banner_label.config(wraplength=max(200, e.width - 10)))

    # 滚动区域
    canvas = tk.Canvas(app, highlightthickness=0)
    vsb = ttk.Scrollbar(app, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side='right', fill='y')
    canvas.pack(fill='both', expand=True, padx=10)
    inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor='nw')
    inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-e.delta / 120), 'units'))

    bottom = tk.Frame(app)
    bottom.pack(fill='x', padx=10, pady=8)
    prog = ttk.Progressbar(bottom, length=200, mode='determinate')
    prog.pack(fill='x')
    prog_text = tk.Label(bottom, text='', anchor='w', fg='#666')
    prog_text.pack(fill='x')
    log_box = tk.Text(bottom, height=10, bg='#1e1e1e', fg='#9fd39f', font=('Consolas', 9))
    log_box.pack(fill='x', pady=4)

    items = []  # [{path,name,container,info,ranges:[[StringVar,StringVar]]}]
    baseline_var = tk.IntVar(value=0)  # 对齐基准视频的下标
    interval_var = tk.StringVar(value='1')  # 图片间隔（每张显示秒数）

    def log(msg):
        q.put(('log', msg))

    def progress(ratio, text):
        q.put(('prog', (ratio, text)))

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == 'log':
                    log_box.insert('end', val + '\n')
                    log_box.see('end')
                elif kind == 'prog':
                    prog['value'] = val[0] * 100
                    prog_text.config(text=val[1])
                elif kind == 'probe':
                    it, info = val
                    it['info'] = info
                elif kind == 'probe_done':
                    refresh()
                elif kind == 'done':
                    start_btn.config(state='normal')
                    if val[0]:
                        messagebox.showinfo('完成', '拼接完成：\n' + val[1])
                    else:
                        messagebox.showerror('失败', val[1])
        except queue.Empty:
            pass
        app.after(100, poll)

    def consistency_state():
        """素材参数一致性：'empty'|'probing'|'same'|'audio_diff'|'video_diff'|'images'|'unknown'"""
        if not items:
            return 'empty'
        if any(it.get('kind') == 'image' for it in items):
            return 'images'
        if len(items) < 2:
            return 'empty'
        infos = [it.get('info') for it in items]
        if any(i is None for i in infos):
            return 'probing'

        def vsig(i):
            if not i.get('width'):
                return None
            return (i.get('vcodec'), i['width'], i['height'], i.get('pix_fmt'),
                    i.get('sar') or '1:1', round((i.get('fps') or 0) * 2) / 2)

        def asig(i):
            if not i.get('has_audio'):
                return None
            return (i.get('acodec'), i.get('sample_rate'), i.get('channels'))

        vs = [vsig(i) for i in infos]
        if any(v is None for v in vs):
            return 'unknown'
        if len(set(vs)) > 1:
            return 'video_diff'
        if len(set(asig(i) for i in infos)) > 1:
            return 'audio_diff'
        return 'same'

    def update_banner(state):
        msgs = {
            'same': ('✔ 所有素材参数一致：不设置裁剪区间时将走无损直通，秒级完成', '#389e0d', '#f6ffed'),
            'images': ('🖼 列表包含图片素材：每张图片按「图片间隔」（默认 1 秒）连续显示后按顺序拼接，'
                      '输出统一重新编码为 H.264+AAC；不同宽高比的图片会等比缩放并以黑边补齐到基准分辨率。'
                      '图片可与视频混排，输出分辨率以「基准」素材为准',
                      '#0958d9', '#e6f4ff'),
            'video_diff': ('⚠ 素材参数不一致：拼接时需要重新编码对齐。请用每段视频左侧的「基准」单选按钮'
                          '选择对齐基准——输出将以基准视频的分辨率和帧率为准，其余视频向其缩放对齐',
                          '#ad6800', '#fff7e6'),
            'audio_diff': ('⚠ 画面参数一致，但音轨不一致：拼接时将统一重编码音频（无音轨的片段补静音）',
                          '#ad6800', '#fff7e6'),
            'unknown': ('⚠ 部分素材参数未识别：默认以第一个视频为对齐基准，可用「基准」单选按钮更改',
                       '#ad6800', '#fff7e6'),
            'probing': ('正在分析素材参数…', '#888', None),
            'empty': ('', '#888', None),
        }
        text, fg, bg = msgs[state]
        banner_label.config(text=text, fg=fg, bg=bg or app.cget('bg'))

    def refresh():
        state = consistency_state()
        for w in inner.winfo_children():
            w.destroy()
        for i, it in enumerate(items):
            card = tk.Frame(inner, bd=1, relief='solid', padx=8, pady=6)
            card.pack(fill='x', pady=4)
            head = tk.Frame(card)
            head.pack(fill='x')
            # 对齐基准单选：仅参数不一致（或未识别）时可选，其余情况无意义
            rb = tk.Radiobutton(head, text='基准', variable=baseline_var, value=i)
            if state in ('video_diff', 'unknown', 'images'):
                rb.config(state='normal')
                if i == baseline_var.get():
                    rb.config(fg='#d4380d')
            else:
                rb.config(state='disabled')
            rb.pack(side='left')
            tag = '【海康PS流】' if it['container'] == 'ps' else ''
            tk.Label(head, text='%d. %s%s' % (i + 1, it['name'], tag),
                     font=('Microsoft YaHei', 10, 'bold'), anchor='w').pack(side='left')
            tk.Button(head, text='删除', command=lambda i=i: del_item(i)).pack(side='right')
            tk.Button(head, text='↓', width=3, command=lambda i=i: move(i, 1)).pack(side='right')
            tk.Button(head, text='↑', width=3, command=lambda i=i: move(i, -1)).pack(side='right')

            # 参数行
            info = it.get('info')
            if it.get('kind') == 'image':
                if info is None:
                    ptxt, pcolor = '正在分析参数…', '#888'
                elif info.get('width'):
                    ptxt, pcolor = '图片 · %d×%d' % (info['width'], info['height']), '#666'
                else:
                    ptxt, pcolor = '参数识别失败，将在开始处理时重试', '#d4380d'
            elif info is None:
                ptxt, pcolor = '正在分析参数…', '#888'
            elif not info.get('width') and not info.get('duration'):
                ptxt, pcolor = '参数识别失败，将在开始处理时重试', '#d4380d'
            else:
                ptxt, pcolor = fmt_params(info), '#666'
            tk.Label(card, text=ptxt, fg=pcolor, anchor='w',
                     font=('Microsoft YaHei', 9)).pack(fill='x')

            if it.get('kind') == 'image':
                tk.Label(card, text='✔ 每张显示「图片间隔」设定的秒数（默认 1 秒），图片不支持裁剪。',
                         fg='#3a9d5d', anchor='w').pack(fill='x')
            else:
                if not it['ranges']:
                    tk.Label(card, text='✔ 未设置裁剪区间，该视频将整段参与拼接。',
                             fg='#3a9d5d', anchor='w').pack(fill='x')
                for k, (sv, ev) in enumerate(it['ranges']):
                    row = tk.Frame(card)
                    row.pack(fill='x', pady=1)
                    tk.Label(row, text='片段 %d：' % (k + 1)).pack(side='left')
                    tk.Entry(row, textvariable=sv, width=12).pack(side='left')
                    tk.Label(row, text=' → ').pack(side='left')
                    tk.Entry(row, textvariable=ev, width=12).pack(side='left')
                    tk.Label(row, text='（秒 / 分:秒 / 时:分:秒，结束留空=到结尾）',
                             fg='#999', font=('Microsoft YaHei', 8)).pack(side='left')
                    tk.Button(row, text='✕', command=lambda k=k, it=it: (it['ranges'].pop(k), refresh())).pack(side='left', padx=4)
                tk.Button(card, text='＋ 添加裁剪区间',
                          command=lambda it=it: (it['ranges'].append([tk.StringVar(), tk.StringVar()]), refresh())
                          ).pack(anchor='w', pady=2)
        update_banner(state)
        start_btn.config(state='normal' if items else 'disabled')

    def move(i, d):
        j = i + d
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
            b = baseline_var.get()
            if b == i:
                baseline_var.set(j)
            elif b == j:
                baseline_var.set(i)
            refresh()

    def del_item(i):
        items.pop(i)
        b = baseline_var.get()
        if not items:
            baseline_var.set(0)
        elif b == i:
            baseline_var.set(min(i, len(items) - 1))
        elif b > i:
            baseline_var.set(b - 1)
        refresh()

    def clear_all():
        items.clear()
        baseline_var.set(0)
        refresh()

    def add_files():
        paths = filedialog.askopenfilenames(
            title='选择视频（可多选）',
            filetypes=[('MP4 文件', '*.mp4'), ('所有文件', '*.*')])
        if not paths:
            return
        new_items = []
        for p in paths:
            it = {'path': p, 'name': os.path.basename(p),
                  'container': sniff_container(p), 'ranges': [], 'info': None}
            items.append(it)
            new_items.append(it)
        refresh()

        # 后台线程逐个识别参数，完成后统一刷新界面，避免阻塞 GUI
        def probe_worker():
            for it in new_items:
                q.put(('probe', (it, probe_import(it))))
            q.put(('probe_done', None))
        threading.Thread(target=probe_worker, daemon=True).start()

    def add_images():
        paths = filedialog.askopenfilenames(
            title='选择图片（可多选，按选择顺序拼接）',
            filetypes=[('图片文件', '*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp'),
                       ('所有文件', '*.*')])
        if not paths:
            return
        new_items = []
        for p in paths:
            it = {'path': p, 'name': os.path.basename(p), 'container': 'image',
                  'kind': 'image', 'ranges': [], 'info': None}
            items.append(it)
            new_items.append(it)
        refresh()

        def probe_worker():
            for it in new_items:
                q.put(('probe', (it, probe_import(it))))
            q.put(('probe_done', None))
        threading.Thread(target=probe_worker, daemon=True).start()

    def start():
        # 收集并校验
        todo = []
        for it in items:
            rs = []
            for sv, ev in it['ranges']:
                s = parse_time(sv.get()) if sv.get().strip() else 0.0
                e = parse_time(ev.get()) if ev.get().strip() else None
                if sv.get().strip() and s is None or ev.get().strip() and e is None:
                    messagebox.showerror('时间格式错误', '「%s」的时间格式不正确' % it['name'])
                    return
                if s is None:
                    s = 0.0
                if e is not None and e <= s:
                    messagebox.showerror('区间无效', '「%s」存在 开始≥结束 的区间' % it['name'])
                    return
                rs.append((s, e))
            todo.append({'path': it['path'], 'name': it['name'],
                         'container': it['container'],
                         'kind': it.get('kind'), 'ranges': rs})
        if not todo:
            return
        img_interval = 1.0
        if any(it.get('kind') == 'image' for it in todo):
            t_raw = interval_var.get().strip() or '1'
            img_interval = parse_time(t_raw)
            if img_interval is None or img_interval <= 0:
                messagebox.showerror('图片间隔无效',
                                     '「图片间隔」需要大于 0，支持 秒（如 1、2.5）或 分:秒（如 0:30）')
                return
        out = filedialog.asksaveasfilename(
            title='保存拼接后的视频', defaultextension='.mp4',
            initialfile='拼接视频_%s.mp4' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S'),
            filetypes=[('MP4 文件', '*.mp4')])
        if not out:
            return
        start_btn.config(state='disabled')
        prog['value'] = 0
        log_box.delete('1.0', 'end')
        base_idx = baseline_var.get()

        def worker():
            try:
                process(todo, out, log, progress, base_idx, img_interval)
                q.put(('done', (True, out)))
            except Exception as e:
                log('错误：' + str(e))
                q.put(('done', (False, str(e))))
        threading.Thread(target=worker, daemon=True).start()

    btns = toolbar
    tk.Button(btns, text='📂 添加视频', command=add_files).pack(side='left', padx=4)
    tk.Button(btns, text='🖼 添加图片', command=add_images).pack(side='left', padx=4)
    start_btn = tk.Button(btns, text='▶ 开始拼接', command=start,
                          bg='#4a90d9', fg='white', font=('Microsoft YaHei', 10, 'bold'))
    start_btn.pack(side='left', padx=4)
    tk.Button(btns, text='清空', command=clear_all).pack(side='right')
    tk.Label(btns, text='秒/张', fg='#666').pack(side='right', padx=(2, 8))
    tk.Entry(btns, textvariable=interval_var, width=6).pack(side='right')
    tk.Label(btns, text='图片间隔', fg='#666').pack(side='right', padx=(12, 2))

    refresh()
    poll()
    app.mainloop()

# ---------------- 自检（命令行） ----------------
def selftest(path):
    global FFMPEG
    FFMPEG = find_ffmpeg()
    print('ffmpeg:', FFMPEG)
    logs = []
    def log(m):
        logs.append(m)
    def prog(r, t):
        pass
    items = [{'path': path, 'name': os.path.basename(path),
              'container': sniff_container(path),
              'ranges': [(30.0, 35.0), (60.0, 65.0), (90.0, 95.0)]}]
    out = os.path.join(tempfile.gettempdir(), 'selftest_out.mp4')
    process(items, out, log, prog)
    info = probe(out, False, log)
    print('OUTPUT duration=%s size=%dKB %sx%s audio=%s' % (
        info['duration'], os.path.getsize(out) // 1024,
        info['width'], info['height'], info['has_audio']))
    ok = info['duration'] and abs(info['duration'] - 15) < 1.5
    print('SELFTEST', 'PASS' if ok else 'FAIL')
    sys.exit(0 if ok else 1)

def selftest_images(paths, interval=1.0):
    """图片拼视频自检：全部图片按 interval 秒/张拼接，校验输出时长 ≈ 张数×interval"""
    global FFMPEG
    FFMPEG = find_ffmpeg()
    print('ffmpeg:', FFMPEG)
    logs = []
    def log(m):
        logs.append(m)
    def prog(r, t):
        pass
    items = [{'path': p, 'name': os.path.basename(p), 'container': 'image',
              'kind': 'image', 'ranges': []} for p in paths]
    out = os.path.join(tempfile.gettempdir(), 'selftest_images_out.mp4')
    process(items, out, log, prog, 0, image_interval=interval)
    info = probe(out, False, log)
    expect = len(paths) * interval
    ok = info['duration'] and abs(info['duration'] - expect) <= max(0.5, expect * 0.1)
    print('OUTPUT duration=%s size=%dKB %sx%s audio=%s' % (
        info['duration'], os.path.getsize(out) // 1024,
        info['width'], info['height'], info['has_audio']))
    print('SELFTEST-IMAGES', 'PASS' if ok else 'FAIL')
    sys.exit(0 if ok else 1)

if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest(sys.argv[sys.argv.index('--selftest') + 1])
    elif '--selftest-images' in sys.argv:
        # 用法：--selftest-images [-t 秒] 图片1 [图片2 ...]
        args = sys.argv[sys.argv.index('--selftest-images') + 1:]
        interval, img_paths = 1.0, []
        i = 0
        while i < len(args):
            if args[i] == '-t':
                interval = float(args[i + 1])
                i += 2
            else:
                img_paths.append(args[i])
                i += 1
        if not img_paths:
            print('用法：--selftest-images [-t 秒] 图片1 [图片2 ...]')
            sys.exit(2)
        selftest_images(img_paths, interval)
    else:
        try:
            FFMPEG = find_ffmpeg()
        except RuntimeError as e:
            import tkinter.messagebox as mb
            mb.showerror('缺少依赖', str(e))
            sys.exit(1)
        launch_gui()
