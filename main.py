import uvicorn

from logging_config import setup_logging

if __name__ == "__main__":
    # 配置全局日志（uvicorn reload 的子进程由 api/server.py 再配置一次）
    setup_logging()
    
    # 启动 FastAPI
    uvicorn.run(
        "api.server:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )