"""FAQ 内容治理脚本使用的候选问法生成提示词。"""

FAQ_VARIANT_SYSTEM_PROMPT = (
    "你是智能客服知识库的内容编辑。给定一条 FAQ 的标准问和答案，"
    "请生成客户在聊天窗口里可能真实输入的、语义相同的不同问法。要求："
    "1) 只改表达，不改问题焦点、否定词、动作和对象；"
    "2) 口语化，长短不一，可带错别字习惯用语但不要生造业务术语；"
    "3) 不要复述标准问，不要互相重复，不要加编号；"
    "4) 不要包含答案内容；"
    '5) 只输出 JSON：{"variants": ["...", ...]}。'
)


def faq_variant_user_prompt(item: dict[str, object], count: int) -> str:
    """把标准问、答案和已有变体组装成候选生成请求。"""

    return (
        f"标准问：{item['standard_question']}\n答案：{item['answer']}\n"
        f"已有问法（不要重复）：{item['existing'] or '无'}\n"
        f"请生成 {count} 条候选。"
    )
