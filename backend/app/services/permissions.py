"""
services/permissions.py —— 基于角色的权限控制（RBAC）与审计日志
============================================================================

项目目前是「演示版」，没有真正的多用户登录，但为了代码结构像真实系统，这里实现了一套
「角色-权限」机制：不同角色（owner / maintainer / developer / viewer）能做的事不同。

- ROLE_PERMISSIONS：一张「角色能做哪些操作」的查表
- ensure_demo_user  ：保证数据库里至少有一个演示用户（没有就建一个 owner）
- has_permission     ：判断某用户是否有某权限
- require_permission ：做成 FastAPI 的「依赖」，套在接口上就能自动拦截无权限请求
- write_audit_log    ：把「谁、对什么、做了什么操作、成功没」记到审计表里

权限代号含义（冒号前是资源，后是动作）：
  repo:read 读仓库 / repo:sync 同步 / agent:run 跑 Agent / draft:create 建草稿 /
  draft:approve 批准草稿 / eval:read 看评测 / settings:manage 改设置."""

from collections.abc import Callable  # Callable：表示「可调用对象」（这里指依赖函数）

from fastapi import Depends, HTTPException  # Depends=FastAPI 依赖注入；HTTPException=抛 HTTP 错误
from sqlalchemy.orm import Session  # 数据库会话

from app.db.models import AuditLog, User  # 审计日志表、用户表
from app.db.session import get_db  # 获取数据库会话的依赖

# 角色权限表：键是角色名，值是该角色拥有的权限集合（set 查找快）
# 为什么用 set 而不是 list：判断「有没有某权限」时 set 的 in 操作是 O(1)，更快
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"repo:read", "repo:sync", "agent:run", "draft:create", "draft:approve", "eval:read", "settings:manage"},
    "maintainer": {"repo:read", "repo:sync", "agent:run", "draft:create", "draft:approve", "eval:read"},
    "developer": {"repo:read", "agent:run", "draft:create", "eval:read"},
    "viewer": {"repo:read", "eval:read"},
}


def ensure_demo_user(db: Session) -> User:
    # 保证库里至少有一个用户。演示环境没有注册登录，所以建一个默认 owner 当「当前用户」
    # order_by(created_at.asc()).first()：取最早创建的那条用户（演示里通常就是它）
    user = db.query(User).order_by(User.created_at.asc()).first()
    if user is None:
        # 库里一个用户都没有，就新建一个叫 demo-owner 的 owner
        user = User(name="demo-owner", role="owner")
        db.add(user)  # 加入会话
        db.commit()  # 提交到数据库
        db.refresh(user)  # 刷新，拿到数据库生成的 id 等字段
    if not getattr(user, "role", None):
        # 防御：万一这条用户没角色，补成 owner，避免后面权限判断出错
        user.role = "owner"
        db.commit()
        db.refresh(user)
    return user


def has_permission(user: User, permission: str) -> bool:
    # 查表判断：把用户角色转小写去查，查不到就当 viewer（最小权限）；看权限在不在集合里
    # 为什么默认 viewer：遵循「最小权限原则」，未知角色只能读，不会 accidentally 获得高权限
    return permission in ROLE_PERMISSIONS.get((user.role or "viewer").lower(), set())


def require_permission(permission: str) -> Callable:
    # 返回一个「FastAPI 依赖函数」。用法：在某个接口参数里写 `user: User = Depends(require_permission("agent:run"))`
    # 请求进来时 FastAPI 会自动调用这个依赖：先确保有用户，再查权限，不够就直接返回 403
    def dependency(db: Session = Depends(get_db)) -> User:
        user = ensure_demo_user(db)  # 拿到（或建出）当前演示用户
        if not has_permission(user, permission):
            # 权限不足，抛 403 Forbidden；detail 里用中文说明缺哪个权限，方便前端提示
            raise HTTPException(status_code=403, detail=f"需要权限：{permission}")
        return user  # 通过校验，把用户对象交给接口函数用

    return dependency


def write_audit_log(
    db: Session,
    *,
    user: User | None,
    repo_id=None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    status: str = "success",
    request_json: dict | None = None,
    result_json: dict | None = None,
) -> AuditLog:
    # 记录一条审计日志：谁（user）、在哪个仓库（repo_id）、做了什么（action）、对什么对象（target）、成没成（status）
    # * 后面的参数是「仅限关键字传参」，避免调用时把位置参数顺序搞错
    row = AuditLog(
        user_id=user.id if user else None,  # 没用户就记 None
        repo_id=repo_id,
        action=action,
        target_type=target_type,  # 比如 "draft" / "repo"
        target_id=target_id,  # 具体对象 id
        status=status,  # "success" 或 "failed"
        request_json=request_json or {},  # 请求内容快照；或空字典
        result_json=result_json or {},  # 结果快照
    )
    db.add(row)  # 加入会话
    db.flush()  # flush 让数据库分配 id，但还不 commit（由外层事务统一提交）
    return row
