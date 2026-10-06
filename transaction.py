# C:\Offline Server\transaction.py
from datetime import datetime, timezone
import uuid
from db.manager import get_conn

def create_transaction(operator_id: int, action_type: str, details: str | None = None) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    ref_id = f"{uuid.uuid4().hex[:8]}-{action_type}"  # короткий уникальный референс

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO transactions (ts, operator_id, action_type, details, ref_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, operator_id, action_type, details, ref_id)
        )
        conn.commit()
        new_id = c.lastrowid

    return {
        "id": new_id,
        "ts": ts,
        "ref_id": ref_id,
        "operator_id": operator_id,
        "action_type": action_type,
        "details": details
    }
