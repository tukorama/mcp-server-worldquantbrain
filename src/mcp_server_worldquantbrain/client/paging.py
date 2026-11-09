from urllib.parse import quote

MAX_OFFSET = 10_000


def segment(value: str) -> str:
    return quote(value, safe="")


def page_info(count: int | None, offset: int, received: int, limit: int) -> tuple[int | None, bool]:
    """Return (next_offset, truncated_by_api). BRAIN rejects offsets at or past 10,000."""
    reached = offset + received
    if received == 0:
        return None, False
    # BRAIN caps `count` at 10,000, so reaching a capped count does not mean the list ended.
    capped = count is not None and count >= MAX_OFFSET
    more = capped or (reached < count if count is not None else received == limit)
    if not more:
        return None, False
    if reached + limit > MAX_OFFSET:
        return None, True
    return reached, False
