"""公开版默认服务请求流程的可审核配置。"""

from shared.sop_config import SopConfig, SopFieldConfig

SERVICE_REQUEST = SopConfig(
    sop_id="service_request",
    version="sop-v1",
    intent_id="SERVICE_REQUEST_CREATE",
    intent_name="创建服务请求",
    name="服务请求登记",
    entry_phrases=(
        "我要提交服务请求",
        "帮我登记一个问题",
        "我需要人工处理这个问题",
        "我要反馈一个问题",
    ),
    negative_phrases=("只是了解", "不用登记", "暂时不提交"),
    fields=(
        SopFieldConfig(
            name="request_category",
            label="请求类型",
            kind="CHOICE",
            prompt="请选择请求类型：账号问题、产品咨询、投诉建议或其他问题。",
            choices={
                "ACCOUNT": "账号问题",
                "PRODUCT": "产品咨询",
                "COMPLAINT": "投诉建议",
                "OTHER": "其他问题",
            },
            aliases={
                "账号问题": "ACCOUNT",
                "登录问题": "ACCOUNT",
                "产品咨询": "PRODUCT",
                "投诉": "COMPLAINT",
                "投诉建议": "COMPLAINT",
                "其他": "OTHER",
                "其他问题": "OTHER",
            },
        ),
        SopFieldConfig(
            name="request_summary",
            label="问题描述",
            kind="TEXT",
            prompt="请简要说明需要处理的问题，不要发送密码、验证码或访问令牌。",
            depends_on=("request_category",),
        ),
        SopFieldConfig(
            name="preferred_contact",
            label="联系偏好",
            kind="CHOICE",
            prompt="希望后续通过在线消息、电话还是电子邮件联系？",
            choices={"CHAT": "在线消息", "PHONE": "电话", "EMAIL": "电子邮件"},
            aliases={
                "在线": "CHAT",
                "在线消息": "CHAT",
                "聊天": "CHAT",
                "电话": "PHONE",
                "打电话": "PHONE",
                "电子邮件": "EMAIL",
                "邮件": "EMAIL",
            },
        ),
    ),
    cancel_phrases=("取消", "不用了", "撤销请求"),
    resume_phrases=("继续", "继续登记"),
    confirm_phrases=("确认", "信息无误", "提交"),
    human_phrases=("转人工", "人工客服", "我要人工处理"),
    switch_phrases=("换个问题", "先问其他问题"),
    messages={
        "confirm": (
            "请核对以下信息：\n{details}\n\n"
            "确认无误后回复“确认”，需要修改请直接说明字段和值。"
        ),
        "invalid": "这条信息还不能用于当前字段，请按提示重新说明。",
        "cancelled": "本次服务请求登记已取消。",
        "human": "流程已暂停，并转交人工坐席继续处理。",
        "ready": "信息已收集完成。",
        "manual_review": "信息已确认，正在等待人工坐席复核和受理。",
        "sensitive": "检测到可能包含敏感信息，流程已暂停，请通过受保护渠道处理。",
        "unsupported": "当前类型暂不支持自动登记，已转交人工处理。",
        "prerequisite": "当前请求缺少必要前提，已转交人工处理。",
    },
    collection_notice="为了保护账号安全，请勿发送密码、验证码或访问令牌。",
)

SOP_DEFINITIONS = (SERVICE_REQUEST,)
