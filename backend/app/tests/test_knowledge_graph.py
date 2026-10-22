"""DevMind AI module."""

# os：标准库，用来在导入数据库模块之前设置环境变量（决定连哪个数据库）。
import os
# uuid：标准库，用来生成全局唯一的随机 id（团队成员、笔记等需要唯一标识）。
import uuid

# 在导入任何数据库相关代码之前，先把 DATABASE_URL 设成"内存 SQLite"。
# 为什么：必须在 import 之前设置，因为有些模块在导入时就会读取这个环境变量来建立连接；
#       用 :memory: 内存库意味着数据只活在本次测试进程里，跑完即焚，不污染真实数据库、也不用建文件。
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

# SQLAlchemy：Python 的 ORM（对象关系映射），把数据库表映射成 Python 类、用 Python 代码来操作数据库。
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 导入项目里的数据库模型（表结构）和知识图谱服务。
from app.db.models import Base, Document, Issue, PRFile, PullRequest, Repository
from app.services.knowledge_graph import knowledge_graph_payload, rebuild_knowledge_graph


def _db_session():
    """_db_session."""
    # 测试专用：建一个全新的内存数据库引擎。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    # 根据模型定义，在内存库里把所有表都建出来（create_all）。
    Base.metadata.create_all(engine)
    # sessionmaker 是个"会话工厂"，bind=engine 把它绑到这个库；() 真正产出一个会话对象（db），用它来增删改查。
    return sessionmaker(bind=engine)()


def test_rebuild_knowledge_graph_creates_explicit_project_edges() -> None:
    # 开一个干净会话。
    db = _db_session()
    # 建一个仓库记录（Repository 表的一行）。
    repo = Repository(owner="local", name="demo", full_name="local/demo")
    db.add(repo)
    # flush 把内存里的对象推送到数据库拿到自增 id，但还没真正提交（commit）。
    db.flush()

    # 给团队成员和笔记各生成一个唯一 id。
    member_id = uuid.uuid4()
    note_id = uuid.uuid4()
    # 造一个 Issue：编号 12、标题、状态 open、指派给 alice。
    issue = Issue(repo_id=repo.id, number=12, title="Auth refresh fails", state="open", assignees=["alice"])
    # 造一个 PR：编号 4、标题、状态 closed、正文 "Fixes #12"（表示这个 PR 修复了 12 号 issue）。
    pr = PullRequest(repo_id=repo.id, number=4, title="Refresh tokens", state="closed", body="Fixes #12", author="alice")
    # 一次性把：团队成员文档、记忆笔记文档、issue、pr 都加进会话。
    db.add_all(
        [
            # 团队成员文档：source_type=team_member，内容是 Alice 的信息。
            Document(
                repo_id=repo.id,
                source_type="team_member",
                source_id=member_id,
                title="Alice",
                content="name: alice",
                meta={"name": "alice", "role": "Backend"},
            ),
            # 记忆笔记文档：提到 token 刷新在 backend/app/auth.py，并引用了 #12。
            Document(
                repo_id=repo.id,
                source_type="memory_note",
                source_id=note_id,
                title="Auth convention",
                content="Keep token refresh in backend/app/auth.py. See #12.",
                meta={"display_title": "Auth convention"},
            ),
            issue,
            pr,
        ]
    )
    db.flush()
    # 给 PR 加一个改动文件记录：backend/app/auth.py 被修改。
    db.add(PRFile(pr_id=pr.id, filename="backend/app/auth.py", status="modified"))
    # commit：正式落库，前面的所有 add/flush 一次性生效。
    db.commit()

    # 调用真实函数：根据数据库现有数据，重建知识图谱（把实体和关系算出来存好）。
    result = rebuild_knowledge_graph(db, repo)
    # 取"以这个 PR 为中心、深度 1"的图谱视图（只看和 PR 直接相关的一层邻居）。
    graph = knowledge_graph_payload(db, repo, center_type="pull_request", center_id=str(pr.id), depth=1)
    # 再取一份"全局概览"图谱（不指定中心）。
    overview = knowledge_graph_payload(db, repo)
    # 把所有边的 (起点类型, 终点类型, 关系) 收成集合，方便断言某种关系是否存在。
    edge_keys = {(edge["from_type"], edge["to_type"], edge["relation"]) for edge in graph["edges"]}

    # 断言：重建出的边数至少有 4 条（实体之间确实连出了多条关系）。
    assert result["edges"] >= 4
    # 断言：存在 "pull_request -> issue" 且关系是 "resolves"（PR 修复 issue，由正文 Fixes #12 推出）。
    assert ("pull_request", "issue", "resolves") in edge_keys
    # 断言：存在 "pull_request -> code_file" 关系 "touches_file"（PR 改动了某个代码文件）。
    assert ("pull_request", "code_file", "touches_file") in edge_keys
    # 断言：存在 "issue -> team_member" 关系 "assigned_to"（issue 指派给了成员）。
    assert ("issue", "team_member", "assigned_to") in edge_keys
    # 断言：在概览图谱里，存在一条从 memory_note 出发、关系为 "cites" 的边（笔记引用了别的实体）。
    assert any(edge["from_type"] == "memory_note" and edge["relation"] == "cites" for edge in overview["edges"])
    # 断言：以 PR 为中心的图谱里，确实有一个 type=pull_request 且 id 等于该 PR 的节点（中心点自己出现在图里）。
    assert any(node["type"] == "pull_request" and node["id"] == str(pr.id) for node in graph["nodes"])
