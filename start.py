#!/usr/bin/env python3
"""
FastAPI 简化启动脚本

基于 fastapi-infra 基础设施包
"""

import argparse
import os
import signal
import sys

import uvicorn


def get_optimal_workers() -> int:
    """计算最优 worker 数量"""
    cpu_count = os.cpu_count() or 1
    return min((2 * cpu_count) + 1, 16)


def setup_signal_handlers():
    """设置优雅关闭信号处理"""
    def signal_handler(signum, frame):
        print(f"\n收到信号 {signum}，正在优雅关闭...")
        raise KeyboardInterrupt()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if sys.platform != "win32":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        print("已配置忽略 SIGHUP 信号（防止 SSH 断开导致服务关闭）")


def start_server(args):
    """启动 FastAPI 服务器"""
    setup_signal_handlers()
    
    # 确定运行模式
    is_production = args.env == "production"
    workers = args.workers or (get_optimal_workers() if is_production else 1)
    
    print(f"启动 FastAPI 服务")
    print(f"环境: {args.env}")
    print(f"地址: http://{args.host}:{args.port}")
    print(f"Workers: {workers}")
    print(f"CPU 核心数: {os.cpu_count()}")
    print("-" * 50)
    
    # 启用 uvloop（仅 Linux/macOS）
    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
            print("已启用 uvloop 事件循环")
        except ImportError:
            pass
    
    # 启动配置
    uvicorn_config = {
        "app": args.app,  # 支持自定义应用路径
        "host": args.host,
        "port": args.port,
        "workers": workers if workers > 1 else None,
        "log_level": "info" if is_production else "debug",
        "reload": not is_production and args.reload,
    }
    
    try:
        print("提示: 按 Ctrl+C 优雅关闭服务器")
        print("-" * 50)
        uvicorn.run(**uvicorn_config)
    except KeyboardInterrupt:
        print("\n服务器已优雅关闭")


def main():
    parser = argparse.ArgumentParser(description="FastAPI 启动脚本")
    
    parser.add_argument(
        "--env",
        choices=["development", "production"],
        default="development",
        help="运行环境"
    )
    parser.add_argument("--host", default="0.0.0.0", help="服务器地址")
    parser.add_argument("--port", type=int, default=8000, help="服务器端口")
    parser.add_argument("--workers", type=int, help="Worker 进程数")
    parser.add_argument("--reload", action="store_true", help="启用热重载（仅开发环境）")
    parser.add_argument("--app", default="app:app", help="应用模块路径（例如 main:app, app.main:app）")
    
    args = parser.parse_args()
    start_server(args)


if __name__ == "__main__":
    main()
