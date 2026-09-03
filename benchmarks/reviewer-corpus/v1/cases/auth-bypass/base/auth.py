def can_delete(user_id: int, owner_id: int, is_admin: bool) -> bool:
    return is_admin or user_id == owner_id
