"""User service for business logic (skeleton)."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)


class UserService:
    """
    User-related business logic (skeleton).

    TODO: Implement user operations:
    - User registration
    - User authentication (if needed)
    - User profile management
    - GitHub account linking
    """

    def __init__(self, db: AsyncSession) -> None:
        """
        Initialize user service.

        Args:
            db: Database session
        """
        self.db = db

    async def create_user(self, email: str, username: str) -> dict[str, Any]:
        """
        Create a new user (skeleton).

        Args:
            email: User email
            username: User username (GitHub username)

        Returns:
            Created user data
        """
        logger.info("create_user", email=email, username=username)

        # TODO: Implement user creation
        # 1. Validate email and username
        # 2. Check if user already exists
        # 3. Create User model instance
        # 4. Save to database
        # 5. Return user data

        raise NotImplementedError("create_user not yet implemented")

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """
        Get user by ID (skeleton).

        Args:
            user_id: User ID

        Returns:
            User data or None if not found
        """
        logger.info("get_user_by_id", user_id=user_id)

        # TODO: Implement user retrieval
        # from sqlalchemy import select
        # from submissions_checker.db.models.user import User
        # result = await self.db.execute(select(User).where(User.id == user_id))
        # user = result.scalar_one_or_none()

        raise NotImplementedError("get_user_by_id not yet implemented")

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """
        Get user by username (skeleton).

        Args:
            username: GitHub username

        Returns:
            User data or None if not found
        """
        logger.info("get_user_by_username", username=username)

        # TODO: Implement user retrieval by username

        raise NotImplementedError("get_user_by_username not yet implemented")
