import json
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from agent.runtime import AgentRuntime

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # 拿到 agent-runtime 根目录

logger = logging.getLogger(__name__)

# 1. 应用与中间件配置
app = FastAPI(
    title="葡萄酒酿制 Agent",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境建议改为具体的前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# 2. 全局单例 Runtime（避免每个请求都重建客户端和调度器）
runtime = AgentRuntime()

# 3. 路由定义
@app.get("/")
async def index():
    """返回前端测试页面。"""
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/chat")
async def chat(message: str):
    """SSE 接口：接收用户输入，流式返回 Agent 事件。"""
    
    async def event_generator():
        try:
            async for event in runtime.run(message):
                # 将 RuntimeEvent 转换为 SSE 要求的格式
                data = json.dumps(event.to_dict(), ensure_ascii=False)
                yield f"data: {data}\n\n"
        except Exception as e:
            logger.exception("SSE 流式输出发生异常")
            error = {
                "type": "runtime_error",
                "data": {"error": str(e)}
            }
            yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # 防止 Nginx 等反向代理缓冲
        }
    )


@app.get("/health")
async def health():
    """健康检查接口。"""
    return {"status": "ok"}