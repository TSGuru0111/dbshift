from ..db import in_binds, sha256_text

NAME = "plsql"


def collect(s, owners):
    frag, binds = in_binds("o", owners)
    lines = s.fetch(
        "plsql.source",
        f"""SELECT owner, type, name, line, text
            FROM dba_source
            WHERE owner IN ({frag})
            ORDER BY owner, type, name, line""",
        binds,
    )

    # DBA_SOURCE stores one row per line, newline included. Reassembling with ''
    # reproduces the stored text byte for byte -- no trimming, no case folding.
    # Normalising here would make the hash stable across changes that are real.
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for row in lines:
        key = (row["owner"], row["type"], row["name"])
        grouped.setdefault(key, []).append(row["text"] or "")

    sources = []
    for (owner, obj_type, name), parts in grouped.items():
        text = "".join(parts)
        sources.append(
            {
                "owner": owner,
                "object_type": obj_type,
                "object_name": name,
                "line_count": len(parts),
                "char_length": len(text),
                "source_sha256": sha256_text(text),
                "source_text": text,
            }
        )
    sources.sort(key=lambda r: (r["owner"], r["object_type"], r["object_name"]))

    errors = s.fetch(
        "plsql.errors",
        f"""SELECT owner, name, type, sequence, line, position, text, attribute, message_number
            FROM dba_errors
            WHERE owner IN ({frag})
            ORDER BY owner, type, name, sequence""",
        binds,
    )
    invalid = s.fetch(
        "plsql.invalid_objects",
        f"""SELECT owner, object_name, object_type, status, last_ddl_time
            FROM dba_objects
            WHERE owner IN ({frag}) AND status <> 'VALID'
            ORDER BY owner, object_type, object_name""",
        binds,
    )
    return {
        "source_inventory.plsql_source": sources,
        "source_inventory.plsql_errors": errors,
        "source_inventory.invalid_objects": invalid,
    }
