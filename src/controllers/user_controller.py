from __future__ import annotations

from ..services.auth_service import delete_user_account, get_user_profile, list_users


def list_users_controller(query: str, limit: int, current_user_id: str | None) -> tuple[int, dict]:
    """List users and shape the HTTP response payload."""
    users = list_users(query=query, limit=limit, current_user_id=current_user_id)
    return 200, {"message": "Users retrieved successfully", "users": users}


def get_user_profile_controller(user_id: str, current_user_id: str | None) -> tuple[int, dict]:
    """Fetch one user profile and shape the HTTP response payload."""
    user = get_user_profile(user_id, current_user_id)
    return 200, {"message": "User profile retrieved successfully", "user": user}


def delete_own_account_controller(target_user_id: str, current_user_id: str) -> tuple[int, dict]:
    """Delete caller-owned account and return success response payload."""
    delete_user_account(current_user_id, target_user_id)
    return 200, {"message": "Account deleted successfully"}

