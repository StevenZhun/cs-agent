"""为已存在的业务客户创建本地登录主体和对象级授权。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from services.bff.infrastructure.database import create_bff_engine
from services.bff.infrastructure.models import (
    SubjectCustomerBindingModel,
    SubjectModel,
    SubjectRoleModel,
)
from services.bff.security import ScryptPasswordService
from shared.web.settings import ServiceSettings


def parse_args() -> argparse.Namespace:
    """读取身份和授权参数；密码只通过交互输入，不进入命令历史。"""

    parser = argparse.ArgumentParser(description="创建或更新本地登录身份")
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=("CUSTOMER", "AGENT", "ADMIN"))
    parser.add_argument(
        "--customer-id",
        action="append",
        default=[],
        help="允许访问的业务客户编号；可重复传入",
    )
    return parser.parse_args()


async def create_identity(args: argparse.Namespace, password: str) -> None:
    """幂等保存登录主体、角色以及显式传入的客户授权关系。"""

    subject_type = "CUSTOMER" if args.role == "CUSTOMER" else "STAFF"
    relationship = "SELF" if args.role == "CUSTOMER" else "ASSIGNED_AGENT"
    if args.role == "CUSTOMER" and len(args.customer_id) != 1:
        raise ValueError("CUSTOMER 角色必须且只能传入一个 --customer-id")

    engine = create_bff_engine(ServiceSettings().database_url)
    try:
        async with engine.begin() as connection:
            subject_insert = insert(SubjectModel).values(
                subject_id=args.subject_id,
                subject_type=subject_type,
                username=args.username,
                password_hash=ScryptPasswordService().hash_password(password),
                status="ACTIVE",
            )
            await connection.execute(
                subject_insert.on_conflict_do_update(
                    index_elements=("subject_id",),
                    set_={
                        "subject_type": subject_insert.excluded.subject_type,
                        "username": subject_insert.excluded.username,
                        "password_hash": subject_insert.excluded.password_hash,
                        "status": subject_insert.excluded.status,
                        "updated_at": func.now(),
                    },
                )
            )
            await connection.execute(
                insert(SubjectRoleModel)
                .values(subject_id=args.subject_id, role_code=args.role)
                .on_conflict_do_nothing(index_elements=("subject_id", "role_code"))
            )
            for customer_id in args.customer_id:
                await connection.execute(
                    insert(SubjectCustomerBindingModel)
                    .values(
                        binding_id=f"binding_{uuid4().hex}",
                        subject_id=args.subject_id,
                        customer_id=customer_id,
                        relationship=relationship,
                        active=True,
                    )
                    .on_conflict_do_nothing(
                        index_elements=("subject_id", "customer_id", "relationship")
                    )
                )
    finally:
        await engine.dispose()


def main() -> None:
    """确认两次密码后创建身份；数据库外键会拒绝不存在的客户编号。"""

    args = parse_args()
    password = getpass.getpass("登录密码：")
    if not password or password != getpass.getpass("再次输入密码："):
        raise ValueError("两次密码不一致或密码为空")
    asyncio.run(create_identity(args, password))
    print(f"身份 {args.subject_id} 已保存。")


if __name__ == "__main__":
    main()
