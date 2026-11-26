"""Frozen comments for tables introduced through migration 0004.

Keep this catalog unchanged after migration 0005. Add later column comments in
their model and additive migration, so old migrations remain replayable.
"""

from sqlalchemy import MetaData

# Full PostgreSQL table name -> (table comment, column comments).
TABLE_COMMENTS: dict[str, tuple[str, dict[str, str]]] = {
    "app.subject": (
        "本地登录主体；只保存身份和认证信息，客户业务事实见 business.customer。",
        {
            "subject_id": "应用主体唯一标识；与客户业务 ID 不同。",
            "subject_type": "主体类型：CUSTOMER=客户；STAFF=内部人员。",
            "username": "唯一登录名，不作为客户业务主键。",
            "password_hash": "本地登录密码的不可逆摘要；空值表示此主体暂不使用本地密码登录。",
            "status": "主体状态：ACTIVE=可用；DISABLED=停用。",
            "created_at": "主体记录创建时间，带时区。",
            "updated_at": "主体记录最近更新时间，带时区。",
        },
    ),
    "app.role": (
        "应用 RBAC 角色目录。",
        {
            "role_code": "角色代码：CUSTOMER=客户；AGENT=坐席；ADMIN=管理员。",
            "display_name": "角色的中文展示名称。",
        },
    ),
    "app.subject_role": (
        "登录主体与角色的多对多分配关系。",
        {
            "subject_id": "被授予角色的应用主体 ID，引用 app.subject。",
            "role_code": "授予的角色代码，引用 app.role。",
            "granted_at": "角色授予时间，带时区。",
        },
    ),
    "app.subject_customer_binding": (
        "应用主体访问业务客户的授权绑定；实际访问仍需对象级授权检查。",
        {
            "binding_id": "授权绑定记录唯一标识。",
            "subject_id": "被授权的应用主体 ID，引用 app.subject。",
            "customer_id": "授权可访问的业务客户 ID，引用 business.customer。",
            "relationship": (
                "绑定关系：SELF=客户本人；ASSIGNED_AGENT=获分配的坐席；" + "SUPERVISOR=主管。"
            ),
            "active": "绑定是否启用：true=启用；false=停用。",
            "created_at": "绑定创建时间，带时区。",
            "expires_at": "授权失效时间；空值表示没有预设到期时间，仍受 active 控制。",
        },
    ),
    "app.idempotency_record": (
        "BFF 写操作的幂等请求记录，用于安全重试和原响应复用。",
        {
            "subject_id": "发起写操作的主体 ID；与操作和幂等键共同构成主键。",
            "operation": "幂等保护的业务操作名称；与主体和幂等键共同构成主键。",
            "idempotency_key": "客户端为同一写请求提供的重试键；与主体和操作共同构成主键。",
            "request_hash": "请求关键内容的哈希；同键重试时用于检测请求内容是否变化。",
            "status": "处理状态：PROCESSING=处理中；COMPLETED=已完成；FAILED=失败。",
            "response_status": "可复用的原 HTTP 响应状态码；尚无最终响应时为空。",
            "response_body": "可复用的原响应 JSON；尚无最终响应时为空。",
            "locked_until": "处理中记录的锁定截止时间；空值表示没有临时锁。",
            "expires_at": "幂等记录有效期截止时间，带时区。",
            "created_at": "记录创建时间，带时区。",
            "updated_at": "记录最近更新时间，带时区。",
        },
    ),
    "app.conversation": (
        "面向客户和坐席的业务会话；不同于 LangGraph 物理线程。",
        {
            "conversation_id": "业务会话唯一标识，不等于 thread_id 或对外咨询编号。",
            "customer_id": "本会话服务的业务客户 ID，引用 business.customer。",
            "created_by_subject_id": "创建会话的应用主体 ID，引用 app.subject。",
            "assigned_agent_subject_id": "当前受理坐席的主体 ID；未分配时为空。",
            "channel": "会话接入渠道；当前使用 WEB，尚无数据库枚举约束。",
            "status": "会话状态：ACTIVE=进行中；WRAP_UP=收尾处理中；FINISHED=已结束。",
            "created_at": "会话创建时间，带时区。",
            "updated_at": "会话最近更新时间，带时区。",
        },
    ),
    "app.conversation_thread_mapping": (
        "业务会话与 LangGraph checkpoint 线程的一对一稳定映射。",
        {
            "conversation_id": "业务会话 ID，引用 app.conversation。",
            "thread_id": "LangGraph 物理线程 ID；用于定位该会话的 checkpoint。",
            "created_at": "映射创建时间，带时区。",
            "updated_at": "映射最近更新时间，带时区。",
        },
    ),
    "app.turn_record": (
        "一条客户输入对应的 Turn 生命周期记录；一个 Turn 可包含多次图运行。",
        {
            "turn_id": "本次客户交互 Turn 的唯一标识。",
            "conversation_id": "所属业务会话 ID，引用 app.conversation。",
            "thread_id": "所属 LangGraph 线程 ID，引用会话线程映射。",
            "request_id": "入口请求关联 ID，用于幂等排查和请求级日志定位。",
            "trace_id": "跨服务追踪 ID，用于串联本 Turn 的调用链。",
            "user_message_id": "触发本 Turn 的客户原文消息 ID，唯一。",
            "status": (
                "Turn 状态：RUNNING=执行中；INTERRUPTED=等待外部输入；"
                "COMPLETED=已完成；FAILED=失败。"
            ),
            "routing_decision": "本 Turn 的路由决策 JSON 快照；尚未形成决策时为空。",
            "active_run_id": "当前或最后关联的图运行 ID；尚无图运行时为空。",
            "started_at": "Turn 开始时间，带时区。",
            "completed_at": "Turn 完成时间；未完成时为空。",
        },
    ),
    "app.graph_run_record": (
        "一次 LangGraph 执行或恢复的运行记录；一个 Turn 可关联多个 Run。",
        {
            "run_id": "一次图执行或恢复的唯一运行 ID。",
            "turn_id": "所属客户交互 Turn ID，引用 app.turn_record。",
            "thread_id": "执行所在的 LangGraph 线程 ID，引用会话线程映射。",
            "status": (
                "运行状态：RUNNING=执行中；INTERRUPTED=等待恢复；"
                + "COMPLETED=已完成；FAILED=失败。"
            ),
            "decision_profile": "决策链配置：BASELINE=基线方案；JEV=启用 Jev 决策层。",
            "decision_ids": "本次运行关联的决策 ID 列表，JSON 数组；可为空数组。",
            "resumed_from_run_id": "本次恢复所承接的上一次运行 ID；非恢复运行时为空。",
            "started_at": "运行开始时间，带时区。",
            "finished_at": "运行结束时间；未结束时为空。",
            "error_code": "失败时的机器可读错误代码；无错误时为空。",
        },
    ),
    "app.chat_history": (
        "追加写的完整原始会话消息；不是 LangGraph checkpoint，也不替代 Graph State。",
        {
            "message_id": "消息唯一标识。",
            "conversation_id": "消息所属业务会话 ID，引用 app.conversation。",
            "thread_id": "消息所属 LangGraph 线程 ID，引用会话线程映射。",
            "turn_id": "消息所属客户交互 Turn ID，引用 app.turn_record。",
            "role": "消息来源：CUSTOMER=客户；AGENT=坐席；ASSISTANT=AI；SYSTEM=系统；TOOL=工具。",
            "content": "原始消息正文；不能为空白内容。",
            "tool_call_id": "工具消息对应的工具调用 ID；role=TOOL 时必填。",
            "request_id": "写入该消息所关联的入口请求 ID。",
            "trace_id": "写入该消息所关联的跨服务追踪 ID。",
            "created_at": "消息创建时间；与 message_id 一起提供稳定分页顺序。",
        },
    ),
    "business.customer": (
        "与登录身份分离的脱敏客户服务资料；不保存密码或应用角色。",
        {
            "customer_id": "业务客户唯一标识；与 app.subject.subject_id 不同。",
            "display_name": "客户展示名称。",
            "contact_last4": "联系号码末四位；不是完整联系方式。",
            "service_level": "部署方定义的客户服务等级。",
            "status": "客户服务状态：ACTIVE=可服务；INACTIVE=停用。",
            "tags": "用于界面展示或授权过滤的非敏感标签。",
            "active": "客户业务记录是否有效：true=有效；false=停用。",
            "created_at": "客户记录创建时间，带时区。",
            "updated_at": "客户记录最近更新时间，带时区。",
        },
    ),
    "business.service_record": (
        "通用客户服务记录；部署方可通过适配器映射自己的业务对象。",
        {
            "record_id": "服务记录唯一标识。",
            "customer_id": "记录归属客户 ID，引用 business.customer。",
            "category": "部署方定义的服务记录类别。",
            "title": "适合在客户页和坐席页展示的标题。",
            "status": "状态：OPEN、PENDING、RESOLVED 或 CLOSED。",
            "reference_code": "可选的外部系统参考编号。",
            "details": "由部署方定义的非敏感扩展字段，JSON 对象。",
            "updated_at": "服务记录最近更新时间，带时区。",
        },
    ),
    "app.alembic_version": (
        "Alembic 结构迁移版本表；由迁移工具维护，不是业务数据。",
        {"version_num": "当前已应用的 Alembic 修订号；请勿手工修改。"},
    ),
}


def apply_model_comments(metadata: MetaData, schema: str) -> None:
    """Attach the frozen catalog to ORM metadata for schema comparison."""

    for table_name, (table_comment, columns) in TABLE_COMMENTS.items():
        if not table_name.startswith(f"{schema}.") or table_name == "app.alembic_version":
            continue
        table = metadata.tables[table_name]
        if not set(columns) <= set(table.c.keys()):
            raise ValueError(f"Comment catalog names missing columns in {table_name}")
        table.comment = table_comment
        for column_name, comment in columns.items():
            table.c[column_name].comment = comment
