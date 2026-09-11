"""手动创建/重置初始管理员账号。

    python -m scripts.init_admin
    python -m scripts.init_admin --username admin --password 'YourPass123'
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import check_password_strength, hash_password  # noqa: E402
from app.db.session import SessionLocal, init_models  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.user import User  # noqa: E402


async def main(username: str, password: str, display_name: str, reset: bool) -> int:
    await init_models()
    problem = check_password_strength(password)
    if problem:
        print(f"[错误] {problem}")
        return 1

    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

        if user is None:
            user = User(
                username=username,
                password_hash=hash_password(password),
                display_name=display_name,
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(user)
            await db.commit()
            print(f"[完成] 已创建管理员：{username}")
            return 0

        if reset:
            user.password_hash = hash_password(password)
            user.role = UserRole.ADMIN
            user.is_active = True
            user.failed_login_count = 0
            user.locked_until = None
            user.must_change_password = False
            await db.commit()
            print(f"[完成] 已重置管理员密码：{username}")
            return 0

        print(f"[跳过] 管理员 {username} 已存在。如需重置密码请加 --reset")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="创建或重置极睿知识库管理员账号")
    parser.add_argument("--username", default=settings.ADMIN_USERNAME)
    parser.add_argument("--password", default=settings.ADMIN_INIT_PASSWORD)
    parser.add_argument("--display-name", default=settings.ADMIN_DISPLAY_NAME)
    parser.add_argument("--reset", action="store_true", help="已存在时重置密码")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(args.username, args.password, args.display_name, args.reset))
    )
