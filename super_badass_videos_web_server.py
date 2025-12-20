"""
视频Web服务器 - 支持视频分享和秘密空间的Web应用
功能：视频流媒体播放、分块传输、连接监控、系统托盘管理
"""

from flask import Flask, send_file, request, jsonify, render_template_string, session
import os
import secrets
import time
import configparser
import signal
import sys
import psutil
import ctypes
import socket
import threading
import webbrowser
import requests
import multiprocessing
from datetime import datetime
from PIL import Image, ImageDraw
import pystray
from waitress import serve

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# 分块传输配置
CHUNK_SIZE = 65536  # 64KB分块大小

# 获取程序所在目录（兼容打包后的exe和Python脚本）
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

# 配置文件管理
config = configparser.ConfigParser()
config_path = os.path.join(application_path, 'config.ini')

DEFAULT_CONFIG = {
    'share_folder': './share',
    'secret_folder': './secret',
    'search_trigger': 'secret',
    'password': 'secret',
    'port': '12345',
    'monitor_username': '',
    'monitor_password': ''
}

def create_default_config(config_path):
    """创建默认配置文件，包含所有必要设置和说明"""
    config = configparser.ConfigParser()
    config['Settings'] = {
        'share_folder': DEFAULT_CONFIG['share_folder'],
        'secret_folder': DEFAULT_CONFIG['secret_folder'],
        'search_trigger': DEFAULT_CONFIG['search_trigger'],
        'password': DEFAULT_CONFIG['password'],
        'port': DEFAULT_CONFIG['port']
    }
    
    # 写入配置文件，添加注释
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write('# 如果分享的文件无法播放，或者无法播放声音，请自行安装ffmpeg，并借鉴如下修改编码方式指令 \n')
        f.write('# ffmpeg -i "%%f" -c:v libx264 -crf 23 -preset medium -c:a aac -b:a 192k -movflags +faststart "%%~dpnf.mp4" -y \n')
        f.write('\n')
        f.write('[Settings]\n')
        f.write('# 分享视频文件夹路径（相对于程序所在目录）\n')
        f.write(f'share_folder = {DEFAULT_CONFIG["share_folder"]}\n\n')
        f.write('# 秘密视频文件夹路径（相对于程序所在目录）\n')
        f.write(f'secret_folder = {DEFAULT_CONFIG["secret_folder"]}\n\n')
        f.write('# 搜索栏触发口令（输入此关键词触发密码验证）\n')
        f.write(f'search_trigger = {DEFAULT_CONFIG["search_trigger"]}\n\n')
        f.write('# 密码口令（验证后可访问秘密视频）\n')
        f.write(f'password = {DEFAULT_CONFIG["password"]}\n\n')
        f.write('# 程序监听端口\n')
        f.write(f'port = {DEFAULT_CONFIG["port"]}\n\n')
        f.write('# 监控页面账号（留空则不需要认证）\n')
        f.write(f'monitor_username = {DEFAULT_CONFIG["monitor_username"]}\n\n')
        f.write('# 监控页面密码（留空则不需要认证）\n')
        f.write(f'monitor_password = {DEFAULT_CONFIG["monitor_password"]}\n')

# 初始化配置
if not os.path.exists(config_path):
    create_default_config(config_path)

config.read(config_path, encoding='utf-8')
VIDEO_ROOT = config.get('Settings', 'share_folder', fallback=DEFAULT_CONFIG['share_folder'])
SECRET_VIDEO_ROOT = config.get('Settings', 'secret_folder', fallback=DEFAULT_CONFIG['secret_folder'])
SEARCH_TRIGGER = config.get('Settings', 'search_trigger', fallback=DEFAULT_CONFIG['search_trigger'])
SECRET_PASSWORD = config.get('Settings', 'password', fallback=DEFAULT_CONFIG['password'])
PORT = config.getint('Settings', 'port', fallback=int(DEFAULT_CONFIG['port']))
MONITOR_USERNAME = config.get('Settings', 'monitor_username', fallback=DEFAULT_CONFIG['monitor_username'])
MONITOR_PASSWORD = config.get('Settings', 'monitor_password', fallback=DEFAULT_CONFIG['monitor_password'])

# 转换为绝对路径
if not os.path.isabs(VIDEO_ROOT):
    VIDEO_ROOT = os.path.join(application_path, VIDEO_ROOT)
else:
    VIDEO_ROOT = os.path.abspath(VIDEO_ROOT)

if not os.path.isabs(SECRET_VIDEO_ROOT):
    SECRET_VIDEO_ROOT = os.path.join(application_path, SECRET_VIDEO_ROOT)
else:
    SECRET_VIDEO_ROOT = os.path.abspath(SECRET_VIDEO_ROOT)

# 自动创建文件夹
if not os.path.exists(VIDEO_ROOT):
    os.makedirs(VIDEO_ROOT)
if not os.path.exists(SECRET_VIDEO_ROOT):
    os.makedirs(SECRET_VIDEO_ROOT)

# Token管理：{token: {'expire_time': timestamp, 'used_time': timestamp or None}}
valid_tokens = {}
TOKEN_USED_CLEANUP_TIME = 3600  # Token清理时间1小时

# 连接监控
active_connections = {}
connection_lock = threading.Lock()
CONNECTION_TIMEOUT = 30
MAX_CONNECTIONS = 100
cleanup_thread = None

# 系统托盘
tray_icon = None

# 网卡信息缓存
ip_to_interface_cache = {}
interface_cache_lock = threading.Lock()
interface_cache_last_update = 0
INTERFACE_CACHE_EXPIRE = 60

def get_interface_name(server_ip):
    """获取IP对应的网卡名称，使用缓存提升性能"""
    global interface_cache_last_update
    current_time = time.time()
    
    with interface_cache_lock:
        if current_time - interface_cache_last_update > INTERFACE_CACHE_EXPIRE:
            # 重建缓存
            ip_to_interface_cache.clear()
            try:
                for iface, addrs in psutil.net_if_addrs().items():
                    for addr in addrs:
                        if addr.family == socket.AF_INET:
                            ip_to_interface_cache[addr.address] = iface
            except:
                pass
            interface_cache_last_update = current_time
        
        return ip_to_interface_cache.get(server_ip, "未知接口")

# 视频列表缓存
video_list_cache = {
    'normal': {'files': [], 'last_update': 0},
    'secret': {'files': [], 'last_update': 0}
}
video_cache_lock = threading.Lock()
VIDEO_CACHE_EXPIRE = 300  # 缓存5分钟

def get_video_list(is_secret=False, force_refresh=False):
    """获取视频列表，使用缓存减少文件系统扫描"""
    cache_key = 'secret' if is_secret else 'normal'
    current_time = time.time()
    
    with video_cache_lock:
        cache_data = video_list_cache[cache_key]
        if not force_refresh and cache_data['files'] and (current_time - cache_data['last_update']) < VIDEO_CACHE_EXPIRE:
            return cache_data['files'].copy()
    
    # 重新扫描文件系统
    video_root = SECRET_VIDEO_ROOT if is_secret else VIDEO_ROOT
    video_files = []
    
    if not os.path.exists(video_root):
        with video_cache_lock:
            video_list_cache[cache_key]['files'] = []
            video_list_cache[cache_key]['last_update'] = current_time
        return video_files
    
    for root, dirs, files in os.walk(video_root):
        for file in files:
            if file.lower().endswith(('.mp4', '.webm', '.ogg', '.mkv', '.rmvb', '.avi', '.flv', '.mov', '.wmv')):
                rel_dir = os.path.relpath(root, video_root)
                rel_file = os.path.join(rel_dir, file) if rel_dir != '.' else file
                video_files.append(rel_file.replace('\\', '/'))
    
    with video_cache_lock:
        video_list_cache[cache_key]['files'] = video_files
        video_list_cache[cache_key]['last_update'] = current_time
    
    return video_files

def clean_expired_tokens():
    """清理过期和已使用的Token"""
    current_time = time.time()
    expired = []
    
    for token, token_info in valid_tokens.items():
        if isinstance(token_info, (int, float)):
            if token_info > 0 and current_time > token_info:
                expired.append(token)
        elif isinstance(token_info, dict):
            expire_time = token_info.get('expire_time', 0)
            used_time = token_info.get('used_time')
            
            if used_time is None and expire_time > 0 and current_time > expire_time:
                expired.append(token)
            elif used_time is not None and current_time > used_time + TOKEN_USED_CLEANUP_TIME:
                expired.append(token)
    
    for token in expired:
        del valid_tokens[token]
    
    return len(expired)

def is_token_valid(token):
    """检查Token是否有效，包括已使用的Token"""
    if not token or token not in valid_tokens:
        return False
    
    token_info = valid_tokens[token]
    current_time = time.time()
    
    if isinstance(token_info, (int, float)):
        if token_info == -1:
            return True
        return token_info > current_time
    
    if isinstance(token_info, dict):
        expire_time = token_info.get('expire_time', 0)
        used_time = token_info.get('used_time')
        
        if used_time is None:
            return expire_time > current_time
        return current_time <= used_time + TOKEN_USED_CLEANUP_TIME
    
    return False

def cleanup_expired_connections():
    """后台线程：定期清理过期连接和Token，自适应调整频率"""
    while True:
        try:
            current_time = time.time()
            
            with connection_lock:
                connection_count = len(active_connections)
                expired = [ip for ip, info in active_connections.items() 
                          if current_time - info.get('last_seen', 0) > CONNECTION_TIMEOUT]
                
                if expired:
                    for ip in expired:
                        del active_connections[ip]
            
            cleaned_tokens = clean_expired_tokens()
            
            # 自适应调整清理间隔
            if connection_count > 50:
                sleep_time = 30
            elif connection_count > 10:
                sleep_time = 60
            elif connection_count > 0:
                sleep_time = 120
            else:
                sleep_time = 300
            
            time.sleep(sleep_time)
            
        except Exception as e:
            time.sleep(60)

def cleanup_oldest_connections(count=10):
    """清理最旧的连接，释放空间"""
    with connection_lock:
        if len(active_connections) >= MAX_CONNECTIONS:
            sorted_connections = sorted(active_connections.items(), 
                                       key=lambda x: x[1].get('last_seen', 0))
            for ip, _ in sorted_connections[:count]:
                del active_connections[ip]

@app.before_request
def track_connection():
    """跟踪和记录客户端连接信息"""
    if request.path in ['/monitor', '/monitor-data']:
        return
    
    client_ip = request.remote_addr
    client_port = request.environ.get('REMOTE_PORT', 'N/A')
    server_ip = request.host.split(':')[0]
    
    if client_ip not in active_connections and len(active_connections) >= MAX_CONNECTIONS:
        cleanup_oldest_connections(10)
    
    with connection_lock:
        if client_ip not in active_connections:
            if len(active_connections) >= MAX_CONNECTIONS:
                cleanup_oldest_connections(10)
            
            interface_name = get_interface_name(server_ip)
            
            active_connections[client_ip] = {
                'server_ip': server_ip,
                'client_port': client_port,
                'interface': interface_name,
                'last_seen': time.time(),
                'video': '未播放',
                'position': 0,
                'duration': 0,
                'bandwidth_down': 0,
                'bandwidth_up': 0,
                'connected_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        else:
            active_connections[client_ip]['last_seen'] = time.time()
            active_connections[client_ip]['client_port'] = client_port

@app.route('/')
def index():
    """主页：视频列表和播放器界面"""
    user_agent = request.headers.get('User-Agent', '').lower()
    is_mobile = any(x in user_agent for x in ['mobile', 'android', 'iphone', 'ipad', 'ipod'])
    
    secret_token = request.args.get('secretnumber', '')
    is_secret_mode = False
    
    if secret_token:
        clean_expired_tokens()
        if secret_token in valid_tokens:
            token_info = valid_tokens[secret_token]
            
            if isinstance(token_info, (int, float)):
                if token_info > 0:
                    is_secret_mode = True
                    valid_tokens[secret_token] = -1
                else:
                    return render_template_string('<script>alert("访问链接已失效，不能重复使用！"); window.location.href="/";</script>')
            elif isinstance(token_info, dict):
                used_time = token_info.get('used_time')
                if used_time is None:
                    is_secret_mode = True
                    valid_tokens[secret_token]['used_time'] = time.time()
                else:
                    return render_template_string('<script>alert("访问链接已失效，不能重复使用！"); window.location.href="/";</script>')
        else:
            return render_template_string('<script>alert("访问链接已失效！"); window.location.href="/";</script>')
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>视频分享中心</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            display: flex;
            height: 100vh;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            overflow: hidden;
        }
        
        body.mobile {
            flex-direction: column;
        }
        
        #sidebar {
            width: 320px;
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            box-shadow: 2px 0 20px rgba(0,0,0,0.1);
            overflow: hidden;
            transition: all 0.5s ease;
            z-index: 100;
            display: flex;
            flex-direction: column;
        }
        
        /* 当屏幕宽度小于1000px时隐藏侧边栏（桌面端） */
        @media (max-width: 1000px) {
            body.desktop #sidebar {
                display: none;
            }
            
            body.desktop #main {
                width: 100%;
            }
        }
        
        /* 移动端侧边栏样式 */
        body.mobile #sidebar {
            width: 100%;
            height: 40vh;
            position: fixed;
            bottom: 0;
            left: 0;
            border-radius: 20px 20px 0 0;
            box-shadow: 0 -5px 30px rgba(0,0,0,0.3);
            transform: translateY(0);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        
        body.mobile #sidebar.collapsed {
            transform: translateY(calc(100% - 60px));
        }
        
        /* 移动端横屏时隐藏侧边栏 */
        @media (max-width: 1000px) and (orientation: landscape) {
            body.mobile #sidebar {
                display: none !important;
            }
            
            body.mobile #main {
                height: 100vh;
            }
        }
        
        #sidebar::-webkit-scrollbar { width: 8px; }
        #sidebar::-webkit-scrollbar-track { background: #f1f1f1; }
        #sidebar::-webkit-scrollbar-thumb { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 4px;
        }
        #sidebar::-webkit-scrollbar-thumb:hover { background: #764ba2; }
        
        .scrollable-content::-webkit-scrollbar { width: 6px; }
        .scrollable-content::-webkit-scrollbar-track { background: #f1f1f1; }
        .scrollable-content::-webkit-scrollbar-thumb { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 3px;
        }
        .scrollable-content::-webkit-scrollbar-thumb:hover { background: #764ba2; }
        
        .sidebar-header {
            padding: 25px 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            font-size: 24px;
            font-weight: 600;
            text-align: center;
            box-shadow: 0 2px 10px rgba(0,0,0,0.15);
            position: relative;
            cursor: pointer;
            user-select: none;
            flex-shrink: 0;
        }
        
        body.mobile .sidebar-header {
            padding: 15px 20px;
            font-size: 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }
        
        .toggle-icon {
            display: none;
            font-size: 20px;
            transition: transform 0.5s;
        }
        
        body.mobile .toggle-icon {
            display: block;
        }
        
        body.mobile #sidebar.collapsed .toggle-icon {
            transform: rotate(180deg);
        }
        
        .search-box {
            padding: 15px;
            background: white;
            border-bottom: 1px solid #e0e0e0;
            flex-shrink: 0;
        }
        
        body.mobile .search-box {
            flex-shrink: 0;
        }
        
        .search-box input {
            width: 100%;
            padding: 10px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 25px;
            font-size: 14px;
            outline: none;
            transition: all 0.5s;
        }
        
        .search-box input:focus {
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .video-item {
            padding: 15px 20px;
            cursor: pointer;
            border-bottom: 1px solid #f0f0f0;
            transition: all 0.5s ease;
            display: flex;
            align-items: center;
            gap: 12px;
            position: relative;
        }
        
        body.mobile .video-item {
            padding: 12px 15px;
            font-size: 14px;
        }
        
        .video-item::before {
            content: '▶';
            font-size: 12px;
            color: #667eea;
            opacity: 0;
            transition: opacity 0.5s;
        }
        
        body.mobile .video-item::before {
            opacity: 1;
            font-size: 10px;
        }
        
        .video-item:hover {
            background: linear-gradient(90deg, rgba(102, 126, 234, 0.1) 0%, rgba(118, 75, 162, 0.1) 100%);
            padding-left: 25px;
            transform: translateX(5px);
        }
        
        body.mobile .video-item:hover {
            transform: none;
            padding-left: 15px;
        }
        
        .video-item:hover::before { opacity: 1; }
        
        .video-item:active {
            background: rgba(102, 126, 234, 0.2);
        }
        
        .video-item.active {
            background: linear-gradient(90deg, rgba(102, 126, 234, 0.15) 0%, rgba(118, 75, 162, 0.15) 100%);
            border-left: 4px solid #667eea;
            font-weight: 600;
            color: #667eea;
        }
        
        .video-name {
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            font-size: 14px;
        }
        
        /* 文件夹样式 */
        .folder-item {
            padding: 12px 20px;
            cursor: pointer;
            border-bottom: 1px solid #f0f0f0;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 10px;
            background: rgba(102, 126, 234, 0.05);
            font-weight: 600;
            color: #667eea;
            user-select: none;
        }
        
        body.mobile .folder-item {
            padding: 10px 15px;
            font-size: 14px;
        }
        
        .folder-item:hover {
            background: rgba(102, 126, 234, 0.1);
        }
        
        .folder-icon {
            font-size: 14px;
            transition: transform 0.3s ease;
        }
        
        .folder-item.collapsed .folder-icon {
            transform: rotate(-90deg);
        }
        
        .folder-name {
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .folder-count {
            font-size: 12px;
            color: #999;
            font-weight: normal;
        }
        
        .folder-videos {
            display: none;
            background: rgba(0, 0, 0, 0.02);
        }
        
        .folder-videos.expanded {
            display: block;
        }
        
        .folder-videos .video-item {
            padding-left: 40px;
        }
        
        body.mobile .folder-videos .video-item {
            padding-left: 30px;
        }
        
        #main {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 30px;
            gap: 20px;
            transition: width 0.5s ease;
        }
        
        body.mobile #main {
            padding: 15px;
            padding-bottom: calc(40vh + 15px);
            height: 100vh;
            overflow-y: auto;
        }
        
        /* 移动端横屏时调整主内容区域 */
        @media (max-width: 1000px) and (orientation: landscape) {
            body.mobile #main {
                padding-bottom: 15px;
            }
        }
        
        .player-container {
            background: rgba(0, 0, 0, 0.8);
            border-radius: 15px;
            padding: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            max-width: 90%;
            backdrop-filter: blur(10px);
            transition: all 0.5s ease;
        }
        
        body.mobile .player-container {
            width: 100%;
            max-width: 100%;
            padding: 15px;
            border-radius: 10px;
        }
        
        #player {
            width: 100%;
            max-width: 1000px;
            border-radius: 10px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.4);
            transition: width 0.5s ease, height 0.5s ease;
        }
        
        body.mobile #player {
            max-width: 100%;
            border-radius: 8px;
        }
        
        .video-title {
            color: white;
            font-size: 20px;
            font-weight: 600;
            text-align: center;
            margin-top: 15px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.5);
            max-width: 1000px;
        }
        
        body.mobile .video-title {
            font-size: 16px;
            margin-top: 10px;
            padding: 0 10px;
        }
        
        .empty-state {
            color: white;
            font-size: 18px;
            text-align: center;
            opacity: 0.8;
        }
        
        body.mobile .empty-state {
            font-size: 16px;
            padding: 20px;
        }
        
        /* 可滚动内容容器 */
        .scrollable-content {
            flex: 1;
            overflow-y: auto;
            overflow-x: hidden;
        }
        
        body.mobile .scrollable-content {
            display: flex;
            flex-direction: column;
        }
        
        .video-count {
            padding: 10px 20px;
            background: rgba(102, 126, 234, 0.1);
            text-align: center;
            font-size: 13px;
            color: #667eea;
            font-weight: 500;
            flex-shrink: 0;
        }
        
        body.mobile .video-count {
            padding: 8px 15px;
            font-size: 12px;
            flex-shrink: 0;
        }
        
        .search-box {
            padding: 15px;
            background: white;
            border-bottom: 1px solid #e0e0e0;
        }
        
        body.mobile .search-box {
            padding: 10px 15px;
        }
        
        .search-box input {
            width: 100%;
            padding: 10px 15px;
            border: 2px solid #e0e0e0;
            border-radius: 25px;
            font-size: 14px;
            outline: none;
            transition: all 0.5s;
        }
        
        body.mobile .search-box input {
            padding: 8px 12px;
            font-size: 13px;
        }
        
        /* 全屏播放按钮 */
        .fullscreen-btn {
            display: none;
            position: absolute;
            top: 20px;
            right: 20px;
            background: rgba(0,0,0,0.6);
            color: white;
            border: none;
            border-radius: 50%;
            width: 45px;
            height: 45px;
            font-size: 20px;
            cursor: pointer;
            z-index: 10;
            backdrop-filter: blur(5px);
        }
        
        body.mobile .fullscreen-btn {
            display: block;
        }
        
        .fullscreen-btn:active {
            background: rgba(0,0,0,0.8);
        }
        
        /* 全页面播放按钮 */
        .fullpage-btn {
            background: rgba(255,255,255,0.15);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
            border-radius: 15px;
            padding: 6px 12px;
            font-size: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 5px;
            backdrop-filter: blur(5px);
            user-select: none;
            transition: all 0.5s ease;
        }
        
        body.mobile .fullpage-btn {
            display: none;  /* 手机端隐藏全页面播放按钮 */
        }
        
        .fullpage-btn:hover {
            background: rgba(255,255,255,0.25);
        }
        
        .fullpage-btn:active {
            background: rgba(255,255,255,0.3);
        }
        
        .fullpage-btn.active {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-color: #667eea;
        }
        
        .fullpage-icon {
            font-size: 12px;
        }
        
        body.mobile .fullpage-icon {
            font-size: 11px;
        }
        
        /* 按钮容器 */
        .controls-container {
            display: flex;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 10px;
            position: relative;
            z-index: 10;
        }
        
        body.mobile .controls-container {
            gap: 8px;
        }
        
        /* 左侧按钮组 */
        .left-controls {
            display: flex;
            gap: 10px;
        }
        
        body.mobile .left-controls {
            gap: 8px;
        }
        
        /* 右侧按钮组 */
        .right-controls {
            display: flex;
            gap: 10px;
        }
        
        body.mobile .right-controls {
            gap: 8px;
        }
        
        /* 全页面播放模式样式 */
        body.fullpage-mode #sidebar {
            display: none;
        }
        
        body.fullpage-mode #main {
            padding: 0;
        }
        
        body.fullpage-mode .player-container {
            max-width: 100%;
            width: 100%;
            height: 100vh;
            border-radius: 0;
            padding: 0;
            display: flex;
            flex-direction: column;
            justify-content: center;
            background: #000;
            position: relative;
        }
        
        body.fullpage-mode #player {
            max-width: 100%;
            width: 100%;
            height: 100%;
            border-radius: 0;
            object-fit: contain;
            position: absolute;
            top: 0;
            left: 0;
            z-index: 1;
        }
        
        body.fullpage-mode .video-title {
            display: none;
        }
        
        body.fullpage-mode .controls-container {
            position: absolute;
            top: 20px;
            left: 20px;
            right: 20px;
            width: auto;
            z-index: 100;
            margin-bottom: 0;
        }
        
        /* 连续播放按钮 */
        .autoplay-btn {
            background: rgba(255,255,255,0.15);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
            border-radius: 15px;
            padding: 6px 12px;
            font-size: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 5px;
            backdrop-filter: blur(5px);
            user-select: none;
            transition: all 0.5s ease;
            min-width: 100px;
        }
        
        body.mobile .autoplay-btn {
            padding: 5px 10px;
            font-size: 11px;
            gap: 4px;
            min-width: 90px;
        }
        
        .autoplay-btn:hover {
            background: rgba(255,255,255,0.25);
        }
        
        .autoplay-btn:active {
            background: rgba(255,255,255,0.3);
        }
        
        .autoplay-btn.active {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-color: #667eea;
        }
        
        .autoplay-icon {
            font-size: 12px;
        }
        
        body.mobile .autoplay-icon {
            font-size: 11px;
        }
        
        /* 上一集/下一集按钮 */
        .episode-btn {
            background: rgba(255,255,255,0.15);
            color: white;
            border: 1px solid rgba(255,255,255,0.3);
            border-radius: 15px;
            padding: 6px 12px;
            font-size: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 5px;
            backdrop-filter: blur(5px);
            user-select: none;
            transition: all 0.5s ease;
        }
        
        body.mobile .episode-btn {
            padding: 5px 10px;
            font-size: 11px;
            gap: 4px;
        }
        
        .episode-btn:hover:not(:disabled) {
            background: rgba(255,255,255,0.25);
        }
        
        .episode-btn:active:not(:disabled) {
            background: rgba(255,255,255,0.3);
        }
        
        .episode-btn:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }
        
        .episode-icon {
            font-size: 12px;
        }
        
        body.mobile .episode-icon {
            font-size: 11px;
        }
        
        /* 小屏幕优化：宽度小于600px时缩小按钮 */
        @media (max-width: 600px) {
            .controls-container {
                gap: 6px;
            }
            
            .left-controls, .right-controls {
                gap: 6px;
            }
            
            .autoplay-btn, .fullpage-btn, .episode-btn {
                padding: 4px 8px;
                font-size: 10px;
                border-radius: 10px;
                min-width: auto;
            }
            
            .autoplay-btn {
                min-width: 70px;
            }
            
            .autoplay-icon, .fullpage-icon, .episode-icon {
                font-size: 9px;
            }
            
            .autoplay-btn span:last-child,
            .episode-btn span:last-child {
                font-size: 10px;
            }
            
            .fullpage-btn span:last-child {
                display: none; /* 隐藏"全页面"文字，只保留图标 */
            }
        }
        
        /* 极小屏幕优化：宽度小于400px时进一步缩小 */
        @media (max-width: 400px) {
            .controls-container {
                gap: 4px;
            }
            
            .left-controls, .right-controls {
                gap: 4px;
            }
            
            .autoplay-btn, .fullpage-btn, .episode-btn {
                padding: 3px 6px;
                font-size: 9px;
                border-radius: 8px;
            }
            
            .autoplay-btn {
                min-width: 60px;
            }
            
            .autoplay-icon, .fullpage-icon, .episode-icon {
                font-size: 8px;
            }
            
            /* 缩短按钮文字 */
            .autoplay-btn span:last-child {
                font-size: 9px;
            }
            
            .episode-btn span:last-child {
                display: none; /* 只显示图标 */
            }
        }
    </style>
</head>
<body class="{{ 'mobile' if is_mobile else 'desktop' }}">
    <div id="main">
        <button class="fullscreen-btn" id="fullscreenBtn" onclick="toggleFullscreen()">⛶</button>
        <div class="player-container" id="playerContainer" style="display: none;">
            <div class="controls-container">
                <div class="left-controls">
                    <button class="autoplay-btn" id="autoplayBtn" onclick="toggleAutoplay()">
                        <span class="autoplay-icon">🔁</span>
                        <span id="autoplayText">连续播放</span>
                    </button>
                    <button class="fullpage-btn" id="fullpageBtn" onclick="toggleFullpage()">
                        <span class="fullpage-icon">⬜</span>
                        <span id="fullpageText">全页面</span>
                    </button>
                </div>
                <div class="right-controls">
                    <button class="episode-btn" id="prevBtn" onclick="playPrevious()">
                        <span class="episode-icon">⏮</span>
                        <span>上一集</span>
                    </button>
                    <button class="episode-btn" id="nextBtn" onclick="playNext()">
                        <span class="episode-icon">⏭</span>
                        <span>下一集</span>
                    </button>
                </div>
            </div>
            <video id="player" controls playsinline webkit-playsinline preload="metadata"></video>
            <div class="video-title" id="videoTitle"></div>
        </div>
        <div class="empty-state" id="emptyState">
            <div>🎬</div>
            <div style="margin-top: 10px;">请选择视频播放</div>
        </div>
    </div>
    <div id="sidebar">
        <div class="sidebar-header" id="sidebarHeader">
            <span id="libraryTitle">{{ '🔒 秘密视频库' if is_secret_mode else '🎬 视频库' }}</span>
            <span class="toggle-icon">▼</span>
        </div>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="🔍 搜索视频..." />
        </div>
        <div class="scrollable-content">
            <div class="video-count" id="videoCount">加载中...</div>
            <div id="videoList"></div>
        </div>
    </div>
    
    <!-- 密码输入对话框 -->
    <div id="passwordModal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); z-index: 10000; align-items: center; justify-content: center;">
        <div style="background: white; padding: 30px; border-radius: 15px; box-shadow: 0 10px 50px rgba(0,0,0,0.5); max-width: 400px; width: 90%;">
            <h2 style="margin: 0 0 20px 0; color: #667eea; text-align: center;">🔐 秘密空间</h2>
            <p style="margin: 0 0 20px 0; color: #666; text-align: center;">请输入访问密码</p>
            <input type="password" id="passwordInput" placeholder="请输入密码" style="width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 16px; margin-bottom: 15px; box-sizing: border-box;" />
            <div style="display: flex; gap: 10px;">
                <button onclick="cancelPassword()" style="flex: 1; padding: 12px; background: #ccc; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer;">取消</button>
                <button onclick="verifyPassword()" style="flex: 1; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer;">确认</button>
            </div>
        </div>
    </div>
    
<script>
const videoListEl = document.getElementById('videoList');
const player = document.getElementById('player');
const playerContainer = document.getElementById('playerContainer');
const emptyState = document.getElementById('emptyState');
const videoTitle = document.getElementById('videoTitle');
const searchInput = document.getElementById('searchInput');
const videoCount = document.getElementById('videoCount');
const sidebar = document.getElementById('sidebar');
const sidebarHeader = document.getElementById('sidebarHeader');
const passwordModal = document.getElementById('passwordModal');
const passwordInput = document.getElementById('passwordInput');
const isMobile = document.body.classList.contains('mobile');
const isSecretMode = {{ 'true' if is_secret_mode else 'false' }};
let currentVideo = null;
let allVideos = [];
let isAutoplayEnabled = false;
let isFullpageMode = false;

// 获取URL参数
const urlParams = new URLSearchParams(window.location.search);
const secretToken = urlParams.get('secretnumber') || '';

// 如果是秘密模式，监听页面刷新事件
if (isSecretMode) {
    // 检测页面是否是刷新（而不是首次加载）
    const pageAccessedByReload = (
        (window.performance.navigation && window.performance.navigation.type === 1) ||
        window.performance
            .getEntriesByType('navigation')
            .map((nav) => nav.type)
            .includes('reload')
    );
    
    if (pageAccessedByReload) {
        // 页面被刷新，通知服务器使token失效
        fetch('/invalidate-token?secretnumber=' + secretToken, {method: 'POST'});
    }
    
    // 监听刷新/关闭前的事件
    window.addEventListener('beforeunload', function() {
        // 使用 sendBeacon 确保请求能发送出去
        navigator.sendBeacon('/invalidate-token?secretnumber=' + secretToken);
    });
}

// 加载视频列表
const videosUrl = secretToken ? '/videos?secretnumber=' + secretToken : '/videos';
fetch(videosUrl).then(r => r.json()).then(list => {
    allVideos = list;
    renderVideoList(list);
    videoCount.textContent = `共 ${list.length} 个视频`;
    
    // 自动加载上次播放的视频（秘密模式下使用不同的key）
    const storageKey = isSecretMode ? 'lastSecretVideo' : 'lastVideo';
    const lastVideo = localStorage.getItem(storageKey);
    if (lastVideo && list.includes(lastVideo)) loadVideo(lastVideo);
    
    // 恢复连续播放状态
    const autoplayKey = isSecretMode ? 'autoplaySecretEnabled' : 'autoplayEnabled';
    const savedAutoplay = localStorage.getItem(autoplayKey);
    if (savedAutoplay === 'true') {
        isAutoplayEnabled = true;
        updateAutoplayButton();
    }
    
    // 恢复全页面播放状态
    if (lastVideo && list.includes(lastVideo)) {
        restoreFullpageMode();
    }
});

function renderVideoList(list) {
    videoListEl.innerHTML = '';
    
    // 组织文件到文件夹结构
    const folderMap = {};
    const rootVideos = [];
    
    list.forEach(v => {
        const parts = v.split('/');
        if (parts.length > 1) {
            // 有文件夹
            const folderName = parts[0];
            if (!folderMap[folderName]) {
                folderMap[folderName] = [];
            }
            folderMap[folderName].push(v);
        } else {
            // 根目录视频
            rootVideos.push(v);
        }
    });
    
    // 渲染根目录视频
    rootVideos.forEach(v => {
        const div = document.createElement('div');
        div.className = 'video-item';
        div.innerHTML = `<span class="video-name" title="${v}">${v}</span>`;
        div.onclick = () => loadVideo(v);
        if (v === currentVideo) div.classList.add('active');
        videoListEl.appendChild(div);
    });
    
    // 渲染文件夹
    Object.keys(folderMap).sort().forEach(folderName => {
        const videos = folderMap[folderName];
        
        // 创建文件夹项
        const folderDiv = document.createElement('div');
        folderDiv.className = 'folder-item';
        folderDiv.innerHTML = `
            <span class="folder-icon">▼</span>
            <span class="folder-name" title="${folderName}">📁 ${folderName}</span>
            <span class="folder-count">(${videos.length})</span>
        `;
        
        // 创建视频容器
        const videosDiv = document.createElement('div');
        videosDiv.className = 'folder-videos expanded';
        
        videos.forEach(v => {
            const videoDiv = document.createElement('div');
            videoDiv.className = 'video-item';
            const fileName = v.split('/').pop();
            videoDiv.innerHTML = `<span class="video-name" title="${v}">${fileName}</span>`;
            videoDiv.onclick = () => loadVideo(v);
            if (v === currentVideo) videoDiv.classList.add('active');
            videosDiv.appendChild(videoDiv);
        });
        
        // 文件夹点击事件
        folderDiv.onclick = () => {
            folderDiv.classList.toggle('collapsed');
            videosDiv.classList.toggle('expanded');
        };
        
        videoListEl.appendChild(folderDiv);
        videoListEl.appendChild(videosDiv);
    });
}

function loadVideo(v, startFromBeginning = false) {
    currentVideo = v;
    const videoUrl = secretToken ? 
        '/video/' + encodeURIComponent(v) + '?secretnumber=' + secretToken : 
        '/video/' + encodeURIComponent(v);
    
    player.src = videoUrl;
    videoTitle.textContent = v;
    
    // 使用不同的storage key
    const storageKey = isSecretMode ? 'lastSecretVideo' : 'lastVideo';
    const timeKey = isSecretMode ? 'secretVideoTime_' + v : 'videoTime_' + v;
    
    localStorage.setItem(storageKey, v);
    
    // 如果需要从头开始播放，则设置为0，否则恢复上次播放位置
    if (startFromBeginning) {
        player.currentTime = 0;
        localStorage.setItem(timeKey, '0');
    } else {
        player.currentTime = parseFloat(localStorage.getItem(timeKey)) || 0;
    }
    
    playerContainer.style.display = 'block';
    emptyState.style.display = 'none';
    
    // 更新选中状态
    document.querySelectorAll('.video-item').forEach(item => {
        item.classList.remove('active');
        const videoNameEl = item.querySelector('.video-name');
        if (videoNameEl && videoNameEl.getAttribute('title') === v) {
            item.classList.add('active');
            // 如果视频在文件夹中，确保文件夹是展开的
            const parentFolder = item.parentElement;
            if (parentFolder && parentFolder.classList.contains('folder-videos')) {
                parentFolder.classList.add('expanded');
                const folderItem = parentFolder.previousElementSibling;
                if (folderItem && folderItem.classList.contains('folder-item')) {
                    folderItem.classList.remove('collapsed');
                }
            }
        }
    });
    
    // 移动端不再自动收起侧边栏
    // if (isMobile) {
    //     sidebar.classList.add('collapsed');
    // }
    
    // 恢复全页面播放状态
    restoreFullpageMode();
    
    // 更新按钮状态
    updateEpisodeButtons();
    
    player.play();
}

// 上报播放状态到服务器
function reportPlayStatus() {
    if (currentVideo && player.duration) {
        fetch('/update-status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                video: currentVideo,
                position: player.currentTime,
                duration: player.duration
            })
        }).catch(() => {});  // 忽略错误
    }
}

// 定期上报播放状态（每5秒）
setInterval(reportPlayStatus, 5000);

// 记录播放进度和总时长
player.ontimeupdate = function() {
    if (currentVideo) {
        const timeKey = isSecretMode ? 'secretVideoTime_' + currentVideo : 'videoTime_' + currentVideo;
        localStorage.setItem(timeKey, player.currentTime);
    }
};

// 记录视频总时长
player.onloadedmetadata = function() {
    if (currentVideo && player.duration) {
        const durationKey = isSecretMode ? 'secretVideoDuration_' + currentVideo : 'videoDuration_' + currentVideo;
        localStorage.setItem(durationKey, player.duration);
        // 立即上报一次状态
        reportPlayStatus();
    }
};

// 视频播放结束事件
player.onended = function() {
    if (isAutoplayEnabled && allVideos.length > 0) {
        // 找到当前视频的索引
        const currentIndex = allVideos.indexOf(currentVideo);
        if (currentIndex !== -1 && currentIndex < allVideos.length - 1) {
            // 播放下一个视频，总是从头开始
            loadVideo(allVideos[currentIndex + 1], true);
        } else if (currentIndex === allVideos.length - 1) {
            // 如果是最后一个视频，循环到第一个，从头开始
            loadVideo(allVideos[0], true);
        }
    }
};

// 切换连续播放状态
function toggleAutoplay() {
    isAutoplayEnabled = !isAutoplayEnabled;
    updateAutoplayButton();
    
    // 保存状态到localStorage
    const autoplayKey = isSecretMode ? 'autoplaySecretEnabled' : 'autoplayEnabled';
    localStorage.setItem(autoplayKey, isAutoplayEnabled.toString());
}

// 更新连续播放按钮状态
function updateAutoplayButton() {
    const btn = document.getElementById('autoplayBtn');
    const text = document.getElementById('autoplayText');
    
    if (isAutoplayEnabled) {
        btn.classList.add('active');
        text.textContent = '连续播放：开';
    } else {
        btn.classList.remove('active');
        text.textContent = '连续播放：关';
    }
}

// 播放上一集
function playPrevious() {
    if (!currentVideo || allVideos.length === 0) return;
    
    const currentIndex = allVideos.indexOf(currentVideo);
    if (currentIndex > 0) {
        // 从头开始播放上一集
        loadVideo(allVideos[currentIndex - 1], true);
    } else {
        // 如果是第一个视频，跳转到最后一个，从头开始
        loadVideo(allVideos[allVideos.length - 1], true);
    }
}

// 播放下一集
function playNext() {
    if (!currentVideo || allVideos.length === 0) return;
    
    const currentIndex = allVideos.indexOf(currentVideo);
    if (currentIndex !== -1 && currentIndex < allVideos.length - 1) {
        // 从头开始播放下一集
        loadVideo(allVideos[currentIndex + 1], true);
    } else {
        // 如果是最后一个视频，跳转到第一个，从头开始
        loadVideo(allVideos[0], true);
    }
}

// 更新上一集/下一集按钮状态
function updateEpisodeButtons() {
    const prevBtn = document.getElementById('prevBtn');
    const nextBtn = document.getElementById('nextBtn');
    
    if (!currentVideo || allVideos.length === 0) {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        return;
    }
    
    // 总是启用按钮，因为支持循环播放
    prevBtn.disabled = false;
    nextBtn.disabled = false;
}

// 搜索功能
searchInput.addEventListener('input', function() {
    const keyword = this.value.toLowerCase();
    
    // 检测是否输入了触发词
    if (keyword === '{{ search_trigger }}' && !isSecretMode) {
        // 显示密码对话框
        passwordModal.style.display = 'flex';
        passwordInput.value = '';
        passwordInput.focus();
        // 清空搜索框
        this.value = '';
        return;
    }
    
    const filtered = allVideos.filter(v => v.toLowerCase().includes(keyword));
    renderVideoList(filtered);
    videoCount.textContent = `${filtered.length} / ${allVideos.length} 个视频`;
});

// 密码验证
function verifyPassword() {
    const password = passwordInput.value;
    
    fetch('/verify-secret', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ password: password })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            // 密码正确，跳转到秘密空间
            window.location.href = '/?secretnumber=' + data.token;
        } else {
            alert('密码错误！');
            passwordInput.value = '';
            passwordInput.focus();
        }
    })
    .catch(err => {
        alert('验证失败，请重试');
    });
}

// 取消密码输入
function cancelPassword() {
    passwordModal.style.display = 'none';
    passwordInput.value = '';
}

// 密码输入框回车提交
passwordInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') {
        verifyPassword();
    }
});

// 键盘快捷键（仅桌面端）
if (!isMobile) {
    document.addEventListener('keydown', function(e) {
        if (e.target.tagName === 'INPUT') return;
        
        if (e.key === ' ') {
            // 防止键盘重复触发（长按时）
            if (e.repeat) return;
            e.preventDefault();
            if (player.paused) player.play();
            else player.pause();
        }
        
        // F键切换全页面播放
        if (e.key === 'f' || e.key === 'F') {
            if (e.repeat) return;
            e.preventDefault();
            if (currentVideo) {
                toggleFullpage();
            }
        }
        
        // Esc键退出全页面播放
        if (e.key === 'Escape' && isFullpageMode) {
            if (e.repeat) return;
            e.preventDefault();
            toggleFullpage();
        }
        
        // 左方向键后退5秒（支持长按连续跳转）
        if (e.key === 'ArrowLeft' && !e.ctrlKey) {
            e.preventDefault();
            player.currentTime = Math.max(0, player.currentTime - 5);
        }
        
        // 右方向键前进5秒（支持长按连续跳转）
        if (e.key === 'ArrowRight' && !e.ctrlKey) {
            e.preventDefault();
            player.currentTime = Math.min(player.duration, player.currentTime + 5);
        }
        
        // Ctrl+左方向键播放上一集
        if (e.key === 'ArrowLeft' && e.ctrlKey) {
            if (e.repeat) return;
            e.preventDefault();
            playPrevious();
        }
        
        // Ctrl+右方向键播放下一集
        if (e.key === 'ArrowRight' && e.ctrlKey) {
            if (e.repeat) return;
            e.preventDefault();
            playNext();
        }
    });
}

// 移动端侧边栏切换
if (isMobile) {
    sidebarHeader.addEventListener('click', function() {
        sidebar.classList.toggle('collapsed');
    });
    
    // 初始化时不再自动收起
    // setTimeout(() => {
    //     if (!currentVideo) {
    //         sidebar.classList.add('collapsed');
    //     }
    // }, 2000);
}

// 全屏功能
function toggleFullscreen() {
    if (!document.fullscreenElement) {
        if (player.requestFullscreen) {
            player.requestFullscreen();
        } else if (player.webkitRequestFullscreen) {
            player.webkitRequestFullscreen();
        } else if (player.webkitEnterFullscreen) {
            player.webkitEnterFullscreen();
        }
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
    }
}

// 全页面播放功能
function toggleFullpage() {
    // 手机端禁用全页面播放功能
    if (isMobile) return;
    
    isFullpageMode = !isFullpageMode;
    updateFullpageButton();
    
    // 保存状态
    const storageKey = isSecretMode ? 'fullpageSecretMode' : 'fullpageMode';
    localStorage.setItem(storageKey, isFullpageMode.toString());
}

// 更新全页面播放按钮状态
function updateFullpageButton() {
    const btn = document.getElementById('fullpageBtn');
    const icon = document.querySelector('.fullpage-icon');
    const text = document.getElementById('fullpageText');
    
    if (isFullpageMode) {
        document.body.classList.add('fullpage-mode');
        btn.classList.add('active');
        icon.textContent = '◱';
        text.textContent = '全页面：开';
    } else {
        document.body.classList.remove('fullpage-mode');
        btn.classList.remove('active');
        icon.textContent = '⬜';
        text.textContent = '全页面：关';
    }
}

// 恢复全页面播放状态
function restoreFullpageMode() {
    // 手机端不恢复全页面播放状态，默认关闭
    if (isMobile) {
        isFullpageMode = false;
        updateFullpageButton();
        return;
    }
    
    const storageKey = isSecretMode ? 'fullpageSecretMode' : 'fullpageMode';
    const savedFullpage = localStorage.getItem(storageKey);
    if (savedFullpage === 'true' && currentVideo) {
        isFullpageMode = true;
        updateFullpageButton();
    }
}

// 阻止移动端双击缩放
if (isMobile) {
    let lastTouchEnd = 0;
    document.addEventListener('touchend', function(e) {
        const now = Date.now();
        if (now - lastTouchEnd <= 300) {
            e.preventDefault();
        }
        lastTouchEnd = now;
    }, false);
    
    // 监听屏幕方向变化
    function handleOrientationChange() {
        // 横屏时确保侧边栏隐藏（CSS已处理，这里只是确保状态一致）
        // 竖屏时恢复侧边栏
        const isLandscape = window.matchMedia('(orientation: landscape)').matches;
        
        if (isLandscape) {
            // 横屏：侧边栏被CSS隐藏
            console.log('横屏模式：视频库已隐藏');
        } else {
            // 竖屏：侧边栏显示
            console.log('竖屏模式：视频库已显示');
        }
    }
    
    // 初始检查
    handleOrientationChange();
    
    // 监听方向变化
    window.addEventListener('orientationchange', handleOrientationChange);
    window.addEventListener('resize', handleOrientationChange);
}
</script>
</body>
</html>
''', is_mobile=is_mobile, is_secret_mode=is_secret_mode, search_trigger=SEARCH_TRIGGER)

# 健康检查端点（用于识别程序）
@app.route('/health-check')
def health_check():
    """健康检查端点，用于识别程序身份"""
    return jsonify({
        'app': 'VideoWebServer',
        'version': '1.0',
        'status': 'running',
        'pid': os.getpid()
    })

@app.route('/verify-secret', methods=['POST'])
def verify_secret():
    """验证密码并生成访问Token"""
    data = request.get_json()
    password = data.get('password', '')
    
    if password == SECRET_PASSWORD:
        token = secrets.token_urlsafe(32)
        valid_tokens[token] = {
            'expire_time': time.time() + 300,
            'used_time': None
        }
        return jsonify({'success': True, 'token': token})
    else:
        return jsonify({'success': False})

@app.route('/invalidate-token', methods=['POST'])
def invalidate_token():
    """使Token失效"""
    secret_token = request.args.get('secretnumber', '')
    if secret_token and secret_token in valid_tokens:
        del valid_tokens[secret_token]
    return '', 204

@app.route('/update-status', methods=['POST'])
def update_status():
    """接收并更新客户端播放状态"""
    data = request.get_json()
    client_ip = request.remote_addr
    
    with connection_lock:
        if client_ip in active_connections:
            active_connections[client_ip].update({
                'video': data.get('video', '未播放'),
                'position': data.get('position', 0),
                'duration': data.get('duration', 0),
                'last_seen': time.time()
            })
    
    return jsonify({'success': True})

@app.route('/monitor')
def monitor():
    """监控页面：显示服务器运行状态和连接信息"""
    if MONITOR_USERNAME and MONITOR_PASSWORD:
        auth = request.authorization
        if not auth or auth.username != MONITOR_USERNAME or auth.password != MONITOR_PASSWORD:
            return ('认证失败', 401, {
                'WWW-Authenticate': 'Basic realm="Monitor Login Required"'
            })
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>服务器运行状态监控</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .header {
            background: white;
            padding: 20px 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            color: #667eea;
            font-size: 24px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .status-badge {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.5s ease;
        }
        
        .status-badge:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        
        .status-badge:active {
            transform: translateY(0);
        }
        
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .stat-card {
            background: white;
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            text-align: center;
        }
        
        .stat-value {
            font-size: 32px;
            font-weight: 700;
            color: #667eea;
            margin-bottom: 5px;
        }
        
        .stat-label {
            color: #666;
            font-size: 14px;
        }
        
        .connections {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
        }
        
        .connections h2 {
            color: #667eea;
            margin-bottom: 20px;
            font-size: 20px;
        }
        
        .connection-item {
            background: linear-gradient(90deg, rgba(102, 126, 234, 0.05) 0%, rgba(118, 75, 162, 0.05) 100%);
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 15px;
            border-left: 4px solid #667eea;
        }
        
        .connection-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        
        .client-ip {
            font-size: 18px;
            font-weight: 600;
            color: #333;
        }
        
        .connection-time {
            color: #999;
            font-size: 12px;
        }
        
        .connection-details {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }
        
        .detail-item {
            display: flex;
            flex-direction: column;
        }
        
        .detail-label {
            color: #999;
            font-size: 12px;
            margin-bottom: 5px;
        }
        
        .detail-value {
            color: #333;
            font-size: 14px;
            font-weight: 500;
        }
        
        .progress-bar {
            background: #e0e0e0;
            height: 8px;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 5px;
        }
        
        .progress-fill {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            height: 100%;
            transition: width 0.5s ease;
        }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #999;
        }
        
        .empty-state-icon {
            font-size: 64px;
            margin-bottom: 20px;
            opacity: 0.5;
        }
        
        .refresh-info {
            text-align: center;
            color: white;
            margin-top: 20px;
            font-size: 14px;
            opacity: 0.9;
        }
        
        /* 访问地址弹窗 */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .modal.show {
            display: flex;
        }
        
        .modal-content {
            background: white;
            border-radius: 15px;
            padding: 30px;
            max-width: 600px;
            width: 100%;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            animation: modalSlideIn 0.5s ease;
        }
        
        .modal-content::-webkit-scrollbar {
            width: 6px;
        }
        
        .modal-content::-webkit-scrollbar-track {
            background: #f5f5f5;
            border-radius: 10px;
            margin: 4px 0;
        }
        
        .modal-content::-webkit-scrollbar-thumb {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 10px;
            border: 2px solid #f5f5f5;
            background-clip: padding-box;
        }
        
        .modal-content::-webkit-scrollbar-thumb:hover {
            background: linear-gradient(135deg, #764ba2 0%, #667eea 100%);
            background-clip: padding-box;
        }
        
        @keyframes modalSlideIn {
            from {
                transform: translateY(-50px);
                opacity: 0;
            }
            to {
                transform: translateY(0);
                opacity: 1;
            }
        }
        
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid #f0f0f0;
        }
        
        .modal-header h2 {
            color: #667eea;
            font-size: 20px;
            margin: 0;
        }
        
        .modal-close {
            background: none;
            border: none;
            font-size: 24px;
            color: #999;
            cursor: pointer;
            padding: 0;
            width: 30px;
            height: 30px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            transition: all 0.2s;
        }
        
        .modal-close:hover {
            background: #f0f0f0;
            color: #333;
        }
        
        .url-section {
            margin-bottom: 25px;
        }
        
        .url-section-title {
            color: #333;
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .url-item {
            background: linear-gradient(90deg, rgba(102, 126, 234, 0.05) 0%, rgba(118, 75, 162, 0.05) 100%);
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 10px;
            border-left: 3px solid #667eea;
        }
        
        .url-interface {
            color: #999;
            font-size: 12px;
            margin-bottom: 5px;
        }
        
        .url-link {
            color: #667eea;
            font-size: 14px;
            font-weight: 500;
            word-break: break-all;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .copy-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 5px 12px;
            border-radius: 5px;
            font-size: 12px;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }
        
        .copy-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 3px 10px rgba(102, 126, 234, 0.3);
        }
        
        .copy-btn:active {
            transform: translateY(0);
        }
        
        .copy-btn.copied {
            background: #4caf50;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>
                <span>📊</span>
                <span>服务器运行状态监控</span>
            </h1>
            <div class="status-badge" onclick="showAccessUrls()">运行中</div>
        </div>
        
        <!-- 访问地址弹窗 -->
        <div id="urlModal" class="modal" onclick="closeModal(event)">
            <div class="modal-content" onclick="event.stopPropagation()">
                <div class="modal-header">
                    <h2>🌐 可访问地址</h2>
                    <button class="modal-close" onclick="closeModal()">&times;</button>
                </div>
                <div id="urlList">
                    <div style="text-align: center; padding: 20px; color: #999;">
                        加载中...
                    </div>
                </div>
            </div>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value" id="activeConnections">0</div>
                <div class="stat-label">活动连接</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="totalClients">0</div>
                <div class="stat-label">总客户端数</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="serverPort">{{ port }}</div>
                <div class="stat-label">监听端口</div>
            </div>
        </div>
        
        <div class="connections">
            <h2>连接详情</h2>
            <div id="connectionsList">
                <div class="empty-state">
                    <div class="empty-state-icon">🔌</div>
                    <div>当前没有活动连接</div>
                </div>
            </div>
        </div>
        
        <div class="refresh-info">
            ⟳ 每3秒自动刷新
        </div>
    </div>
    
    <script>
        let cachedAccessUrls = [];
        
        function formatTime(seconds) {
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return mins + ':' + (secs < 10 ? '0' : '') + secs;
        }
        
        function formatDuration(seconds) {
            const now = Date.now();
            const duration = Math.floor((now - seconds * 1000) / 1000);
            if (duration < 60) return duration + '秒前';
            if (duration < 3600) return Math.floor(duration / 60) + '分钟前';
            return Math.floor(duration / 3600) + '小时前';
        }
        
        function showAccessUrls() {
            const modal = document.getElementById('urlModal');
            modal.classList.add('show');
            
            if (cachedAccessUrls.length > 0) {
                renderAccessUrls(cachedAccessUrls);
            }
        }
        
        function closeModal(event) {
            if (!event || event.target.id === 'urlModal') {
                document.getElementById('urlModal').classList.remove('show');
            }
        }
        
        function copyToClipboard(text, button) {
            navigator.clipboard.writeText(text).then(() => {
                const originalText = button.textContent;
                button.textContent = '✓ 已复制';
                button.classList.add('copied');
                setTimeout(() => {
                    button.textContent = originalText;
                    button.classList.remove('copied');
                }, 2000);
            }).catch(err => {
                alert('复制失败，请手动复制');
            });
        }
        
        function renderAccessUrls(urls) {
            const urlList = document.getElementById('urlList');
            
            // 分组：本地地址和网络地址
            const localUrls = urls.filter(u => u.interface === '本地回环地址');
            const networkUrls = urls.filter(u => u.interface !== '本地回环地址');
            
            let html = '';
            
            if (localUrls.length > 0) {
                html += '<div class="url-section">';
                html += '<div class="url-section-title">🏠 本地访问 - 仅限本机</div>';
                localUrls.forEach(item => {
                    html += `
                        <div class="url-item">
                            <div class="url-link">
                                <span>${item.url}</span>
                                <button class="copy-btn" onclick="copyToClipboard('${item.url}', this)">复制</button>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
            }
            
            if (networkUrls.length > 0) {
                html += '<div class="url-section">';
                html += '<div class="url-section-title">🌐 局域网访问 - 可供其他设备访问</div>';
                networkUrls.forEach(item => {
                    html += `
                        <div class="url-item">
                            <div class="url-interface">${item.interface}</div>
                            <div class="url-link">
                                <span>${item.url}</span>
                                <button class="copy-btn" onclick="copyToClipboard('${item.url}', this)">复制</button>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
            }
            
            if (localUrls.length === 0 && networkUrls.length === 0) {
                html = '<div style="text-align: center; padding: 20px; color: #999;">暂无可用地址</div>';
            }
            
            urlList.innerHTML = html;
        }
        
        function updateMonitor() {
            fetch('/monitor-data')
                .then(r => r.json())
                .then(data => {
                    // 缓存访问地址
                    if (data.access_urls) {
                        cachedAccessUrls = data.access_urls;
                    }
                    
                    // 更新统计数据
                    document.getElementById('activeConnections').textContent = data.active_count;
                    document.getElementById('totalClients').textContent = data.total_clients;
                    
                    // 更新连接列表
                    const listEl = document.getElementById('connectionsList');
                    
                    if (data.connections.length === 0) {
                        listEl.innerHTML = `
                            <div class="empty-state">
                                <div class="empty-state-icon">🔌</div>
                                <div>当前没有活动连接</div>
                            </div>
                        `;
                    } else {
                        listEl.innerHTML = data.connections.map(conn => {
                            const progress = conn.duration > 0 ? (conn.position / conn.duration * 100).toFixed(1) : 0;
                            const progressWidth = Math.min(100, Math.max(0, progress));
                            
                            return `
                                <div class="connection-item">
                                    <div class="connection-header">
                                        <div class="client-ip">🖥️ ${conn.client_ip}:${conn.client_port}</div>
                                        <div class="connection-time">${formatDuration(conn.last_seen)}</div>
                                    </div>
                                    <div class="connection-details">
                                        <div class="detail-item">
                                            <div class="detail-label">服务器接口</div>
                                            <div class="detail-value">${conn.interface}</div>
                                        </div>
                                        <div class="detail-item">
                                            <div class="detail-label">服务器IP</div>
                                            <div class="detail-value">${conn.server_ip}</div>
                                        </div>
                                        <div class="detail-item">
                                            <div class="detail-label">播放视频</div>
                                            <div class="detail-value">${conn.video}</div>
                                        </div>
                                        <div class="detail-item">
                                            <div class="detail-label">播放进度</div>
                                            <div class="detail-value">
                                                ${progress}% (${formatTime(conn.position)} / ${formatTime(conn.duration)})
                                                <div class="progress-bar">
                                                    <div class="progress-fill" style="width: ${progressWidth}%"></div>
                                                </div>
                                            </div>
                                        </div>
                                        <div class="detail-item">
                                            <div class="detail-label">下载速率</div>
                                            <div class="detail-value">${conn.bandwidth_down.toFixed(2)} KB/s</div>
                                        </div>
                                        <div class="detail-item">
                                            <div class="detail-label">上传速率</div>
                                            <div class="detail-value">${conn.bandwidth_up.toFixed(2)} KB/s</div>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }).join('');
                    }
                })
                .catch(err => {
                    console.error('Failed to update monitor:', err);
                });
        }
        
        // 初始加载
        updateMonitor();
        
        // 优化4：智能刷新 - 页面可见时才刷新，降低CPU和网络开销
        let updateInterval = null;
        let isPageVisible = true;
        
        function startUpdating() {
            if (!updateInterval) {
                updateInterval = setInterval(updateMonitor, 3000);  // 从2秒改为3秒
            }
        }
        
        function stopUpdating() {
            if (updateInterval) {
                clearInterval(updateInterval);
                updateInterval = null;
            }
        }
        
        // 监听页面可见性变化
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                stopUpdating();
                isPageVisible = false;
            } else {
                updateMonitor();  // 立即更新一次
                startUpdating();
                isPageVisible = true;
            }
        });
        
        // 启动定时更新
        startUpdating();
        
        // ESC键关闭弹窗
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' || event.key === 'Esc') {
                const modal = document.getElementById('urlModal');
                if (modal.classList.contains('show')) {
                    modal.classList.remove('show');
                }
            }
        });
    </script>
</body>
</html>
''', port=PORT)

# 监控数据API
@app.route('/monitor-data')
def monitor_data():
    """返回监控数据"""
    # 如果配置了监控账号密码，则需要认证
    if MONITOR_USERNAME and MONITOR_PASSWORD:
        auth = request.authorization
        if not auth or auth.username != MONITOR_USERNAME or auth.password != MONITOR_PASSWORD:
            return jsonify({'error': '认证失败'}), 401
    
    # 优化：使用局部变量减少锁持有时间
    connections_snapshot = []
    with connection_lock:
        # 注意：不在这里清理，由后台线程负责清理
        for client_ip, info in active_connections.items():
            connections_snapshot.append({
                'client_ip': client_ip,
                'client_port': info.get('client_port', 'N/A'),
                'server_ip': info.get('server_ip', 'N/A'),
                'interface': info.get('interface', 'N/A'),
                'video': info.get('video', '未播放'),
                'position': info.get('position', 0),
                'duration': info.get('duration', 0),
                'bandwidth_down': info.get('bandwidth_down', 0),
                'bandwidth_up': info.get('bandwidth_up', 0),
                'last_seen': info.get('last_seen', 0)
            })
    
    # 获取所有可访问的IP地址（在锁外执行）
    ip_addresses = get_all_ip_addresses()
    access_urls = []
    for interface, ip in ip_addresses:
        access_urls.append({
            'interface': interface,
            'ip': ip,
            'url': f'http://{ip}:{PORT}'
        })
    
    return jsonify({
        'active_count': len(connections_snapshot),
        'total_clients': len(connections_snapshot),
        'connections': connections_snapshot,
        'access_urls': access_urls
    })

@app.route('/videos')
def videos():
    """获取视频列表API"""
    secret_token = request.args.get('secretnumber', '')
    is_secret = is_token_valid(secret_token)
    
    return jsonify(get_video_list(is_secret=is_secret))

@app.route('/video/<path:filename>')
def video(filename):
    """视频流媒体服务，支持分块传输和断点续传"""
    secret_token = request.args.get('secretnumber', '')
    is_secret = is_token_valid(secret_token)
    
    video_root = SECRET_VIDEO_ROOT if is_secret else VIDEO_ROOT
    file_path = os.path.join(video_root, filename)
    
    if not os.path.isfile(file_path):
        return 'File not found', 404

    ext = os.path.splitext(filename)[1].lower()
    mime_types = {
        '.mp4': 'video/mp4',
        '.webm': 'video/webm',
        '.ogg': 'video/ogg',
        '.mkv': 'video/x-matroska',
        '.rmvb': 'application/vnd.rn-realmedia-vbr',
        '.avi': 'video/x-msvideo',
        '.flv': 'video/x-flv',
        '.mov': 'video/quicktime'
    }
    mimetype = mime_types.get(ext, 'video/mp4')
    
    size = os.path.getsize(file_path)
    range_header = request.headers.get('Range', None)
    
    if not range_header:
        def generate_full():
            """生成完整文件流"""
            try:
                with open(file_path, 'rb') as f:
                    while True:
                        data = f.read(CHUNK_SIZE)
                        if not data:
                            break
                        yield data
            except IOError as e:
                yield b''
        
        return app.response_class(
            generate_full(),
            200,
            mimetype=mimetype,
            headers={
                'Content-Length': str(size),
                'Accept-Ranges': 'bytes'
            }
        )
    
    byte1, byte2 = 0, size - 1
    m = range_header.replace('bytes=', '').split('-')
    if m[0]: 
        byte1 = int(m[0])
    if m[1]: 
        byte2 = int(m[1])
    
    byte1 = max(0, min(byte1, size - 1))
    byte2 = max(byte1, min(byte2, size - 1))
    length = byte2 - byte1 + 1
    
    def generate_range():
        """生成指定范围的文件流"""
    return app.response_class(
        generate_range(),
        206,
        mimetype=mimetype,
        headers={
            'Content-Range': f'bytes {byte1}-{byte2}/{size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(length)
        }
    )

def show_message_box(title, message, style):
    """显示Windows消息框"""
    return ctypes.windll.user32.MessageBoxW(0, message, title, style)

def get_all_ip_addresses():
    """获取本机所有可用IP地址"""
    ip_list = []
    
    ip_list.append(("本地回环地址", "127.0.0.1"))
    ip_list.append(("本地回环地址", "localhost"))
    
    try:
        for interface_name, interface_addresses in psutil.net_if_addrs().items():
            for address in interface_addresses:
                if address.family == socket.AF_INET and not address.address.startswith('127.'):
                    ip_list.append((interface_name, address.address))
    except Exception:
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            if not local_ip.startswith('127.'):
                ip_list.append(("局域网地址", local_ip))
        except:
            pass
    
    return ip_list

def get_pid_by_port(port):
    """根据端口号获取占用进程的PID"""
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == 'LISTEN':
                return conn.pid
    except (psutil.AccessDenied, PermissionError):
        try:
            import subprocess
            result = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
            for line in result.stdout.split('\n'):
                if f':{port}' in line and 'LISTENING' in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        try:
                            return int(parts[-1])
                        except ValueError:
                            pass
        except:
            pass
    return None

def check_port_available(port):
    """检查端口可用性并智能处理冲突"""
    pid = get_pid_by_port(port)
    
    if pid is None:
        return True
    
    try:
        response = requests.get(f'http://127.0.0.1:{port}/health-check', timeout=2)
        if response.status_code == 200:
            data = response.json()
            if data.get('app') == 'VideoWebServer':
                message = f"检测到端口 {port} 已被本程序占用\n进程 PID: {pid}\n\n是否关闭老进程并重新启动？"
                result = show_message_box("视频服务器 - 启动确认", message, 4 | 48)
                
                if result == 6:
                    try:
                        proc = psutil.Process(pid)
                        proc.terminate()  # 先尝试优雅关闭
                        try:
                            proc.wait(timeout=5)  # 等待最多5秒
                        except psutil.TimeoutExpired:
                            proc.kill()  # 如果不行就强制关闭
                        time.sleep(1)  # 等待端口释放
                        return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                        show_message_box("视频服务器 - 错误", f"无法关闭进程 {pid}\n\n错误: {str(e)}", 16)
                        sys.exit(1)
                else:
                    # 用户选择不关闭
                    sys.exit(0)
    except requests.exceptions.RequestException:
        # 无法连接或不是本程序
        pass
    
    # 3. 端口被其他程序占用
    # MB_OK = 0, MB_ICONERROR = 16
    message = f"端口 {port} 已被其他程序占用！\n\n占用进程 PID: {pid}\n\n请关闭占用端口的程序后重试，或修改 config.ini 中的端口配置。"
    show_message_box("视频服务器 - 端口被占用", message, 16)
    sys.exit(1)

def check_and_kill_existing_process():
    """启动前检查并处理端口占用"""
    check_port_available(PORT)

def create_tray_icon():
    """创建精美的系统托盘图标"""
    width = 64
    height = 64
    
    image = Image.new('RGBA', (width, height), color=(0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    
    center_x, center_y = width // 2, height // 2
    radius = 28
    
    dc.ellipse([center_x - radius - 2, center_y - radius - 2, 
                center_x + radius + 2, center_y + radius + 2], 
               fill=(102, 126, 234, 200))
    
    dc.ellipse([center_x - radius, center_y - radius, 
                center_x + radius, center_y + radius], 
               fill=(118, 75, 162, 255))
    
    dc.ellipse([center_x - radius + 4, center_y - radius + 4, 
                center_x + radius - 4, center_y + radius - 4], 
               fill=(138, 95, 182, 255))
    
    play_size = 18
    play_x = center_x - 2
    play_y = center_y
    
    dc.polygon([
        (play_x - play_size//3, play_y - play_size//2),
        (play_x - play_size//3, play_y + play_size//2),
        (play_x + play_size*2//3, play_y)
    ], fill=(255, 255, 255, 255))
    
    dc.ellipse([center_x - radius + 8, center_y - radius + 8,
                center_x - radius + 14, center_y - radius + 14],
               fill=(255, 255, 255, 100))
    
    return image

def get_connection_status():
    """获取当前连接状态信息"""
    with connection_lock:
        active_count = len(active_connections)
    
    return f"运行中 | 连接数: {active_count} / {MAX_CONNECTIONS}"

def show_monitoring_window():
    """在浏览器中打开监控页面"""
    webbrowser.open(f'http://127.0.0.1:{PORT}/monitor')

def on_quit_tray():
    """退出托盘程序和服务器"""
    global tray_icon
    if tray_icon:
        tray_icon.stop()
    
    os._exit(0)

def setup_tray_icon():
    """配置并启动系统托盘图标"""
    global tray_icon
    
    icon_image = create_tray_icon()
    
    menu = pystray.Menu(
        pystray.MenuItem("运行状态监控", lambda: show_monitoring_window()),
        pystray.MenuItem("退出", lambda: on_quit_tray())
    )
    
    tray_icon = pystray.Icon(
        "video_server",
        icon_image,
        "视频服务器",
        menu
    )
    
    def update_tooltip():
        """定期更新托盘图标提示信息"""
    def update_tooltip():
        """定期更新托盘图标提示信息"""
        while True:
            try:
                tray_icon.title = f"视频服务器\n{get_connection_status()}"
                time.sleep(5)
            except:
                break
    
    tooltip_thread = threading.Thread(target=update_tooltip, daemon=True)
    tooltip_thread.start()
    
    tray_icon.run()

def signal_handler(sig, frame):
    """处理中断信号"""
    sys.exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    
    check_and_kill_existing_process()
    
    # 根据CPU核心数优化线程数
    try:
        cpu_count = multiprocessing.cpu_count()
        optimal_threads = max(6, min(48, cpu_count * 3))
    except:
        optimal_threads = 12
    
    try:
        cleanup_thread = threading.Thread(target=cleanup_expired_connections, daemon=True)
        cleanup_thread.start()
        
        def run_flask():
            serve(app, host='0.0.0.0', port=PORT, threads=optimal_threads, channel_timeout=180)
        
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        setup_tray_icon()
        
    except Exception as e:
        show_message_box("视频服务器 - 启动失败", f"启动失败！\n\n错误信息: {str(e)}", 16)
        sys.exit(1)
