"""DevMind AI module."""

# search.py —— 语义检索（RAG 搜索）路由层
#
# 业务域：知识检索。前端输入一句话（query），后端用向量相似度在仓库的
# Issue/PR/代码/文档里找出最相关的历史记录，这就是「语义搜索 / RAG 检索」。
#
# 暴露端点：
#   POST /api/search —— 对某个仓库做语义检索，可带标签/路径/状态等过滤条件
#
# 依赖 service：app.services.rag.retrieval.search_similar_documents（底层向量检索实现）
from fastapi import APIRouter, Depends, HTTPException  # 引入路由、依赖注入、HTTP 异常
from sqlalchemy.orm import Session  # 引入数据库会话类型

from app.db.models import Repository  # Repository 是数据库里的「仓库」表模型
from app.db.session import get_db  # get_db 是 FastAPI 依赖，用来获取一个数据库会话
from app.schemas.search import SearchRequest, SearchResponse  # 请求体和响应体模型
from app.services.rag.retrieval import search_similar_documents  # 真正做向量检索的函数

router = APIRouter()  # 创建本模块的路由器（菜单）


@router.post("", response_model=SearchResponse)  # POST 请求，路径相对前缀；返回 SearchResponse
async def semantic_search(payload: SearchRequest, db: Session = Depends(get_db)) -> SearchResponse:
    # payload 是前端传来的请求体（含 repo_id、query、过滤条件等）；db 通过 Depends 自动注入
    repo = db.get(Repository, payload.repo_id)  # 按 repo_id 查数据库里的仓库记录
    if repo is None:
        # 仓库不存在就返回 404
        raise HTTPException(status_code=404, detail="未找到仓库")

    # 把前端可能给的过滤条件收集进一个字典；类比成「在图书馆里按书架、分类筛选」
    filters = {
        "label": payload.label,  # 按 Issue/PR 标签过滤
        "path": payload.path,  # 按代码路径过滤
        "module": payload.module,  # 按模块过滤
        "state": payload.state,  # 按状态（open/closed）过滤
        "start_date": payload.start_date,  # 时间范围起点
        "end_date": payload.end_date,  # 时间范围终点
    }
    results = search_similar_documents(
        db,
        payload.repo_id,  # 在哪个仓库里搜
        payload.query,  # 用户的问题/关键词
        source_type=payload.source_type,  # 想搜哪类数据（issue/pr/code...），可为 None
        limit=payload.limit,  # 最多返回几条
        # metadata_filters：只保留「有值」的过滤条件，空值不参与检索（类比：只带填了的筛选框）
        metadata_filters={key: value for key, value in filters.items() if value},
    )
    # 把检索结果包成响应模型返回
    return SearchResponse(results=results)
