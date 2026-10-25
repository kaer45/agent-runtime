import uvicorn
import logging

if __name__ == "__main__":
    # 配置全局日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    
    # 启动 FastAPI
    uvicorn.run(
        "api.server:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )