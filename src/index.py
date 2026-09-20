from app import create_server


if __name__ == "__main__":
    server = create_server()
    if server.service.config.scheduler_enabled:
        server.service.start_scheduler()
    print("无人机运行管理服务已启动", flush=True)
    try:
        server.serve_forever()
    finally:
        server.service.close()
