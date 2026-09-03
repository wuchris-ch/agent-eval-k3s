from auth import can_delete

assert can_delete(user_id=7, owner_id=9, is_admin=False) is False
