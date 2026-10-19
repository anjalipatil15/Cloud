# Typical web-API dependencies, imported at module level so they load during INIT.
import jinja2  # noqa: F401
import pydantic  # noqa: F401
import requests  # noqa: F401


def handler(event, context):
    return {"ok": True}
