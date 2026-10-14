"""
A PBS select statement's chunks. Chunks are joined by '+', and so are the instance type
alternatives inside a chunk (instance_type=c5.large+c5.xlarge), so a split on every '+'
breaks a chunk apart: a chunk boundary is a '+' followed by a chunk count or a resource name.
"""

import re

_CHUNK_BOUNDARY = re.compile(r'\+(?=\d+(?=:|$)|[A-Za-z_]\w*=)')


def select_chunks(select: str) -> list:
    return _CHUNK_BOUNDARY.split(select)
