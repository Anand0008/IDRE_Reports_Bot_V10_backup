"""Normalize IDRE API response shapes into {rows, meta}."""


def normalize(response_body: dict) -> dict:
    """Best-effort flattening. Returns {"rows": [...], "meta": {...}}."""
    data = response_body.get("data", response_body)
    rows: list = []
    meta: dict = {}
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # Find the obvious row array
        for key in ("cases", "payments", "payouts", "disputes", "activities", "rows", "items"):
            if key in data and isinstance(data[key], list):
                rows = data[key]
                meta = {k: v for k, v in data.items() if k != key}
                break
        else:
            # No row array; treat the whole dict as a single-row aggregate
            rows = [data]
            meta = {}
    return {"rows": rows, "meta": meta}
