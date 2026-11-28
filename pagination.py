"""Small guards for paginated APIs that can repeat a nextPageToken."""


def next_page_token(response: dict, current: str | None, seen: set[str], operation: str) -> str | None:
    token = response.get("nextPageToken")
    if not token:
        return None
    if token == current or token in seen:
        raise RuntimeError(f"{operation} repeated a page token")
    seen.add(token)
    return token
