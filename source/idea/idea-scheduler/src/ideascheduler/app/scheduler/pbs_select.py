"""Keep PBS chunks separate from the '+' in instance_type alternatives."""

from ideadatamodel.pbs_select import select_chunks  # noqa: F401  (shared with reporting)


def chunk_resources(chunk):
    return dict(token.split('=', 1) for token in chunk.split(':') if '=' in token)


def select_compute_node(select, compute_node):
    return '+'.join(
        ':'.join(
            token for token in chunk.split(':') if not token.startswith('compute_node=')
        )
        + f':compute_node={compute_node}'
        for chunk in select_chunks(select)
    )
